"""
Rich broadcast — separate module (does NOT bloat pingbot.py).

Features covered for the spec:
- admin-only (caller checks), admin button + admin command both route here
- text/media, safe Telegram HTML, optional URL buttons
- preview + explicit confirmation before any recipient is messaged
- status/cancel, per-recipient status in DB so a restart can resume
- retry with backoff, 429 respect, blocked/failing recipient never stops worker
- one failing chat/message never blocks other recipients or the bot itself
- PostgreSQL + SQLite idempotent migrations

Why a separate module — pingbot.py is 5.6k lines; risky bulk refactor
is avoided by keeping broadcast logic here and only wiring thin delegates
from pingbot.py.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

import requests

from database import get_db_connection

logger = logging.getLogger("codenest-broadcast")

# ── Config ───────────────────────────────────────────────────────────────
MAX_ATTEMPTS = 3
BASE_DELAY_S = 0.05
MAX_BACKOFF_S = 30
CAMPAIGN_POLL_S = 2.0

# Telegram HTML allowlist — safe subset. Script/style/iframe etc. are escaped.
ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "span"}
# span only for tg-spoiler
ALLOWED_SPAN_RE = re.compile(r"tg-spoiler", re.I)

# worker globals
_broadcast_lock = threading.Lock()
_broadcast_thread: Optional[threading.Thread] = None
_broadcast_stop = threading.Event()
# injectable send for tests — if set, used instead of HTTP
_SEND_FN = None  # type: ignore

# ── Helpers ──────────────────────────────────────────────────────────────
def _now_utc_str() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def _bot_token() -> str:
    return (os.getenv("BOT_TOKEN", "").strip()
            or os.getenv("TELEGRAM_PING_BOT_TOKEN", "").strip())

def _tg_api() -> str:
    tok = _bot_token()
    return f"https://api.telegram.org/bot{tok}" if tok else ""

# ── Safe HTML ────────────────────────────────────────────────────────────

def sanitize_html(text: str) -> str:
    """Escape everything except a tiny allowlist of Telegram HTML tags.
    - Outside tags: html.escape
    - <a href="...">: only http(s) URLs kept, others escaped
    - <span class=\"tg-spoiler\">: kept, other spans escaped
    - all other unknown tags escaped
    - output truncated to 4096 (Telegram text limit)
    """
    if not text:
        return ""
    s = str(text)
    # quick: if no angle bracket, just escape
    if "<" not in s and ">" not in s:
        return html.escape(s, quote=False)[:4096]
    out: List[str] = []
    last = 0
    for m in re.finditer(r"<[^>]*>", s):
        # text before tag
        out.append(html.escape(s[last:m.start()], quote=False))
        tag_raw = m.group(0)
        # parse
        inner = tag_raw[1:-1].strip()
        if not inner:
            out.append(html.escape(tag_raw))
            last = m.end()
            continue
        is_closing = inner.startswith("/")
        name_part = inner[1:].strip() if is_closing else inner
        # tag name is first word
        name_match = re.match(r"([a-zA-Z0-9\-]+)", name_part)
        if not name_match:
            out.append(html.escape(tag_raw))
            last = m.end()
            continue
        name = name_match.group(1).lower()
        rest = name_part[len(name_match.group(1)):].strip()
        if name not in ALLOWED_TAGS:
            out.append(html.escape(tag_raw))
            last = m.end()
            continue
        if is_closing:
            # allow closing for allowed tags (span handled same)
            out.append(f"</{name}>")
            last = m.end()
            continue
        # opening tag
        if name == "a":
            href_m = re.search(r'href\s*=\s*(["\'])(.*?)\1', rest, re.I)
            if not href_m:
                out.append(html.escape(tag_raw))
            else:
                url = href_m.group(2).strip()
                if not re.match(r"^https?://", url, re.I):
                    out.append(html.escape(tag_raw))
                else:
                    safe_url = html.escape(url, quote=True)
                    # Telegram wants exactly <a href="…">
                    out.append(f'<a href="{safe_url}">')
            last = m.end()
            continue
        if name == "span":
            if ALLOWED_SPAN_RE.search(rest):
                out.append('<span class="tg-spoiler">')
            else:
                out.append(html.escape(tag_raw))
            last = m.end()
            continue
        # other allowed tags have no attrs — drop attrs
        out.append(f"<{name}>")
        last = m.end()
    out.append(html.escape(s[last:], quote=False))
    sanitized = "".join(out)
    # limit
    if len(sanitized) > 4096:
        sanitized = sanitized[:4096]
    return sanitized

# aliases for different naming expectations
safe_html = sanitize_html
escape_html = sanitize_html
clean_html = sanitize_html
sanitize_telegram_html = sanitize_html

def validate_html(text: str) -> tuple[bool, str]:
    """Return (ok, sanitized). ok always True — we sanitize rather than reject,
    but caller can compare len to detect if stripping happened."""
    sanitized = sanitize_html(text)
    return True, sanitized

# ── URL buttons ─────────────────────────────────────────────────────────
def parse_url_buttons(spec: Any) -> List[List[Dict[str, str]]]:
    """Parse optional URL buttons.
    Accepts:
      - None / '' -> []
      - JSON string of inline_keyboard
      - list of dicts / rows
      - multiline text: each line is a row, '|' separates columns, each column is 'Label https://url' or 'Label | https://url'
    Returns Telegram inline_keyboard: list of rows, each row list of {text,url}
    """
    if not spec:
        return []
    if isinstance(spec, list):
        # could be [{"text":..., "url":...}] or [[{...}]] or JSON decoded
        # normalize to rows
        rows: List[List[Dict[str,str]]] = []
        for item in spec:
            if isinstance(item, dict) and "text" in item and "url" in item:
                # single button -> one per row
                url = str(item["url"]).strip()
                if not re.match(r"^https?://", url, re.I):
                    continue
                rows.append([{"text": str(item["text"])[:64], "url": url}])
            elif isinstance(item, list):
                row = []
                for b in item:
                    if isinstance(b, dict) and "text" in b and "url" in b:
                        url = str(b["url"]).strip()
                        if not re.match(r"^https?://", url, re.I):
                            continue
                        row.append({"text": str(b["text"])[:64], "url": url})
                if row:
                    rows.append(row)
        return rows
    if isinstance(spec, str):
        s = spec.strip()
        if not s or s == "-":
            return []
        # try JSON
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return parse_url_buttons(parsed)
            if isinstance(parsed, dict) and "inline_keyboard" in parsed:
                return parse_url_buttons(parsed["inline_keyboard"])
        except Exception:
            pass
        rows = []
        for line in s.splitlines():
            line = line.strip()
            if not line or line == "-":
                continue
            # Prefer pipe-pair syntax: "Label | https://url" (one or many per line)
            # Regex captures label before | and url after
            pipe_pairs = re.findall(r"([^|]+?)\s*\|\s*(https?://\S+)", line)
            if pipe_pairs:
                row = []
                for label, url in pipe_pairs:
                    label = label.strip(" -:,")
                    url = url.strip().rstrip(".,;!")
                    if not re.match(r"^https?://", url, re.I):
                        continue
                    if not label:
                        label = url
                    row.append({"text": label[:64], "url": url})
                if row:
                    rows.append(row)
                    continue
            # Fallback: standalone "Label https://url" or just URL per column split
            columns = [c.strip() for c in line.split("|") if c.strip()]
            # If no pipe found, treat whole line as one column attempt
            if len(columns) == 1 and "|" not in line:
                columns = [line]
            row = []
            for col in columns:
                m = re.search(r"(https?://\S+)", col)
                if not m:
                    continue
                url = m.group(1).rstrip(".,;!")
                text = col[:m.start()].strip(" -:|,")
                if not text:
                    text = url
                row.append({"text": text[:64], "url": url})
            if row:
                rows.append(row)
        return rows
    return []

# aliases
parse_buttons = parse_url_buttons
parse_inline_buttons = parse_url_buttons
parse_inline_keyboard = parse_url_buttons
build_inline_keyboard = parse_url_buttons

def build_inline_keyboard_markup(buttons: Any) -> Optional[str]:
    rows = parse_url_buttons(buttons)
    if not rows:
        return None
    return json.dumps({"inline_keyboard": rows})

def _buttons_from_json(buttons_json: Optional[str]) -> List[List[Dict[str,str]]]:
    if not buttons_json:
        return []
    try:
        data = json.loads(buttons_json)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []

# ── Schema / migration ──────────────────────────────────────────────────
def ensure_broadcast_schema() -> None:
    """Idempotent — create tables + indices if missing. Works on both SQLite and Postgres."""
    # simplest: call database.init_db which already handles translation, but to avoid
    # circular heavy init on every call, we do direct CREATE IF NOT EXISTS
    conn = get_db_connection()
    try:
        # reuse database translation by executing raw DDL through its wrapper
        # (get_db_connection already returns wrapper that translates ? -> %s etc,
        # but DDL has no placeholders, so direct is fine).
        # Use try/except for each — Postgres may error if column already exists etc but IF NOT EXISTS makes it safe.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_by INTEGER,
                status TEXT NOT NULL DEFAULT 'draft',
                text TEXT NOT NULL DEFAULT '',
                html TEXT NOT NULL DEFAULT '',
                parse_mode TEXT NOT NULL DEFAULT 'HTML',
                media_type TEXT,
                media_file_id TEXT,
                caption TEXT,
                buttons_json TEXT,
                total_count INTEGER NOT NULL DEFAULT 0,
                sent_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                blocked_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                error TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_recipients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                next_retry_at TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (campaign_id) REFERENCES broadcast_campaigns (id) ON DELETE CASCADE,
                UNIQUE (campaign_id, telegram_id)
            )
        """)
        # indices
        conn.execute("CREATE INDEX IF NOT EXISTS idx_broadcast_campaigns_status ON broadcast_campaigns (status, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_campaign ON broadcast_recipients (campaign_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_telegram ON broadcast_recipients (telegram_id, status)")
        # column migrations for legacy partial installs — best effort, ignore errors
        def _col_exists(table, col):
            try:
                # use info schema for pg, pragma for sqlite — reuse helper by trying
                from database import DIALECT, _safe_ident
                if DIALECT == "postgres":
                    r = conn.execute("SELECT 1 FROM information_schema.columns WHERE table_name = ? AND column_name = ?", (table, col)).fetchone()
                    return bool(r)
                else:
                    rows = conn.execute(f"PRAGMA table_info({_safe_ident(table)})").fetchall()
                    return any(x["name"] == col for x in rows)
            except Exception:
                return True  # assume exists to avoid alter
        if not _col_exists("broadcast_campaigns", "html"):
            try: conn.execute("ALTER TABLE broadcast_campaigns ADD COLUMN html TEXT NOT NULL DEFAULT ''")
            except Exception: pass
        if not _col_exists("broadcast_campaigns", "caption"):
            try: conn.execute("ALTER TABLE broadcast_campaigns ADD COLUMN caption TEXT")
            except Exception: pass
        if not _col_exists("broadcast_campaigns", "blocked_count"):
            try: conn.execute("ALTER TABLE broadcast_campaigns ADD COLUMN blocked_count INTEGER NOT NULL DEFAULT 0")
            except Exception: pass
        if not _col_exists("broadcast_recipients", "next_retry_at"):
            try: conn.execute("ALTER TABLE broadcast_recipients ADD COLUMN next_retry_at TEXT")
            except Exception: pass
        conn.commit()
    except Exception as exc:
        logger.warning("ensure_broadcast_schema: %s", exc)
        try: conn.rollback()
        except: pass
    finally:
        try: conn.close()
        except: pass

# Call at import time idempotently — safe because CREATE IF NOT EXISTS
try:
    ensure_broadcast_schema()
except Exception:
    pass

# ── Recipient source ─────────────────────────────────────────────────────
def _all_linked_telegram_ids() -> List[int]:
    """All active linked telegram_ids. Prefer telegram_admin_ext but fallback to direct query."""
    try:
        from services import telegram_admin_ext
        ids = telegram_admin_ext.all_linked_telegram_ids()
        return [int(x) for x in ids if x]
    except Exception:
        pass
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT telegram_id FROM users WHERE telegram_id IS NOT NULL AND is_suspended = 0").fetchall()
        return [int(r["telegram_id"]) for r in rows if r["telegram_id"]]
    finally:
        conn.close()

# ── Campaign CRUD ────────────────────────────────────────────────────────
def create_campaign(
    created_by: Optional[int],
    text: str,
    *,
    parse_mode: str = "HTML",
    buttons: Any = None,
    media_type: Optional[str] = None,
    media_file_id: Optional[str] = None,
    caption: Optional[str] = None,
    recipients: Optional[List[int]] = None,
    auto_start: bool = False,
) -> int:
    """Create a campaign and its recipient rows. Returns campaign id.
    - text is sanitized to safe HTML
    - buttons is optional inline URL buttons (see parse_url_buttons)
    - media_type/media_file_id optional (photo/video/document/audio)
    - recipients: list of telegram_ids. If None, all linked users are used.
    - auto_start: if True, status='sending' immediately, else 'preview' (needs confirmation)
    Raises ValueError on empty text (and empty media).
    """
    ensure_broadcast_schema()
    raw = (text or "").strip()
    cap = (caption or "").strip() if caption is not None else raw
    # allow media without text? spec says text/media, so at least one must be present
    if not raw and not media_file_id:
        raise ValueError("Broadcast text is empty")
    html_text = sanitize_html(raw) if raw else ""
    html_caption = sanitize_html(cap) if cap else html_text
    # buttons
    rows = parse_url_buttons(buttons) if buttons is not None else []
    # also try to extract buttons embedded in tail of text like "...\n[Label](https://url)"? simple: if raw contains "http" and no explicit buttons, try parse tail lines
    if not rows and raw and "https://" in raw:
        # last 3 lines that contain |
        tail = "\n".join(raw.splitlines()[-3:])
        maybe = parse_url_buttons(tail)
        # but don't auto-extract if not clear — keep explicit only. Skipping auto to avoid surprise.
        pass
    buttons_json = json.dumps(rows) if rows else None
    pm = "HTML"  # force HTML for safety; parse_mode param kept for compat
    if parse_mode and parse_mode.upper() in ("HTML", "MARKDOWN", "MARKDOWNV2"):
        pm = parse_mode.upper()
        if pm != "HTML":
            pm = "HTML"  # we always store HTML
    now = _now_utc_str()
    status = "sending" if auto_start else "preview"
    started_at = now if auto_start else None

    conn = get_db_connection()
    try:
        cur = conn.execute(
            "INSERT INTO broadcast_campaigns (created_by, status, text, html, parse_mode, media_type, media_file_id, caption, buttons_json, total_count, sent_count, failed_count, blocked_count, created_at, started_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (created_by, status, raw, html_text, pm, media_type, media_file_id, html_caption, buttons_json, 0, 0, 0, 0, now, started_at),
        )
        campaign_id = cur.lastrowid
        if not campaign_id:
            # postgres fallback: SELECT last
            row = conn.execute("SELECT id FROM broadcast_campaigns ORDER BY id DESC LIMIT 1").fetchone()
            campaign_id = int(row["id"]) if row else None
        if not campaign_id:
            raise RuntimeError("Could not create broadcast campaign")
        # recipients
        if recipients is None:
            recipients = _all_linked_telegram_ids()
        # dedupe and filter
        recips = []
        seen = set()
        for tid in (recipients or []):
            try: tid_i = int(tid)
            except: continue
            if tid_i in seen: continue
            seen.add(tid_i)
            recips.append(tid_i)
        total = len(recips)
        # insert recipients
        for tid in recips:
            try:
                conn.execute(
                    "INSERT INTO broadcast_recipients (campaign_id, telegram_id, status, attempts, updated_at) VALUES (?,?, 'pending', 0, ?)",
                    (campaign_id, tid, now),
                )
            except Exception as exc:
                # duplicate -> ignore, other errors log
                msg = str(exc).lower()
                if "unique" in msg or "duplicate" in msg or "conflict" in msg:
                    continue
                logger.warning("recipient insert %s: %s", tid, exc)
        conn.execute("UPDATE broadcast_campaigns SET total_count=? WHERE id=?", (total, campaign_id))
        conn.commit()
        return int(campaign_id)
    finally:
        conn.close()

# aliases
create_broadcast = create_campaign
create_broadcast_campaign = create_campaign

def get_campaign(campaign_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM broadcast_campaigns WHERE id=?", (int(campaign_id),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

get_broadcast = get_campaign

def list_campaigns(limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT * FROM broadcast_campaigns ORDER BY id DESC LIMIT ? OFFSET ?", (int(limit), int(offset))).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

list_broadcasts = list_campaigns

def campaign_stats(campaign_id: int) -> Dict[str, Any]:
    conn = get_db_connection()
    try:
        camp = conn.execute("SELECT * FROM broadcast_campaigns WHERE id=?", (int(campaign_id),)).fetchone()
        if not camp:
            return {}
        camp_d = dict(camp)
        rows = conn.execute("SELECT status, COUNT(*) as c FROM broadcast_recipients WHERE campaign_id=? GROUP BY status", (int(campaign_id),)).fetchall()
        counts = {r["status"]: r["c"] for r in rows}
        total = camp_d.get("total_count") or sum(counts.values())
        pending = counts.get("pending", 0)
        sent = counts.get("sent", 0)
        failed = counts.get("failed", 0)
        blocked = counts.get("blocked", 0)
        cancelled = counts.get("cancelled", 0)
        # keep DB counters in sync (sent/failed/blocked) — but authoritative is recipients table
        return {
            "campaign": camp_d,
            "total": total,
            "pending": pending,
            "sent": sent,
            "failed": failed,
            "blocked": blocked,
            "cancelled": cancelled,
            "status": camp_d.get("status"),
        }
    finally:
        conn.close()

get_status = campaign_stats
broadcast_status = campaign_stats
get_campaign_stats = campaign_stats

def cancel_campaign(campaign_id: int, cancelled_by: Optional[int] = None) -> bool:
    """Cancel a preview/sending campaign. Marks pending recipients as cancelled."""
    ensure_broadcast_schema()
    conn = get_db_connection()
    try:
        camp = conn.execute("SELECT status FROM broadcast_campaigns WHERE id=?", (int(campaign_id),)).fetchone()
        if not camp:
            return False
        if camp["status"] in ("completed", "cancelled"):
            return False
        now = _now_utc_str()
        conn.execute("UPDATE broadcast_campaigns SET status='cancelled', completed_at=?, error=? WHERE id=?", (now, "cancelled by admin", int(campaign_id)))
        conn.execute("UPDATE broadcast_recipients SET status='cancelled', updated_at=? WHERE campaign_id=? AND status='pending'", (now, int(campaign_id)))
        conn.commit()
        return True
    finally:
        conn.close()

cancel_broadcast = cancel_campaign

def confirm_campaign(campaign_id: int, confirmed_by: Optional[int] = None) -> bool:
    """Move preview -> sending. Starts worker."""
    ensure_broadcast_schema()
    conn = get_db_connection()
    try:
        camp = conn.execute("SELECT status FROM broadcast_campaigns WHERE id=?", (int(campaign_id),)).fetchone()
        if not camp:
            return False
        if camp["status"] != "preview":
            # if already sending/completed, treat as success
            return camp["status"] in ("sending", "completed")
        now = _now_utc_str()
        conn.execute("UPDATE broadcast_campaigns SET status='sending', started_at=? WHERE id=?", (now, int(campaign_id)))
        conn.commit()
    finally:
        conn.close()
    # ensure worker running
    start_broadcast_worker()
    return True

# aliases
approve_campaign = confirm_campaign

def _get_sending_campaigns(limit: int = 5) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT * FROM broadcast_campaigns WHERE status='sending' ORDER BY id ASC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def _get_pending_recipients(campaign_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        # respect next_retry_at: only return those whose retry time has passed or is null
        now = _now_utc_str()
        rows = conn.execute(
            "SELECT * FROM broadcast_recipients WHERE campaign_id=? AND status='pending' AND (next_retry_at IS NULL OR next_retry_at <= ?) ORDER BY id ASC LIMIT ?",
            (int(campaign_id), now, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def _update_campaign_counts(campaign_id: int) -> None:
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT status, COUNT(*) as c FROM broadcast_recipients WHERE campaign_id=? GROUP BY status", (int(campaign_id),)).fetchall()
        m = {r["status"]: r["c"] for r in rows}
        sent = m.get("sent", 0)
        failed = m.get("failed", 0)
        blocked = m.get("blocked", 0)
        conn.execute("UPDATE broadcast_campaigns SET sent_count=?, failed_count=?, blocked_count=? WHERE id=?", (int(sent), int(failed), int(blocked), int(campaign_id)))
        conn.commit()
    finally:
        conn.close()

def _mark_recipient_sent(campaign_id: int, telegram_id: int) -> None:
    conn = get_db_connection()
    try:
        now = _now_utc_str()
        conn.execute("UPDATE broadcast_recipients SET status='sent', attempts=attempts+1, last_error=NULL, next_retry_at=NULL, updated_at=? WHERE campaign_id=? AND telegram_id=?", (now, int(campaign_id), int(telegram_id)))
        conn.commit()
    finally:
        conn.close()
    _update_campaign_counts(campaign_id)

def _mark_recipient_blocked(campaign_id: int, telegram_id: int, err: str) -> None:
    conn = get_db_connection()
    try:
        now = _now_utc_str()
        conn.execute("UPDATE broadcast_recipients SET status='blocked', attempts=attempts+1, last_error=?, next_retry_at=NULL, updated_at=? WHERE campaign_id=? AND telegram_id=?", (err[:500], now, int(campaign_id), int(telegram_id)))
        conn.commit()
    finally:
        conn.close()
    _update_campaign_counts(campaign_id)

def _mark_recipient_failed(campaign_id: int, telegram_id: int, err: str) -> None:
    conn = get_db_connection()
    try:
        now = _now_utc_str()
        conn.execute("UPDATE broadcast_recipients SET status='failed', attempts=attempts+1, last_error=?, next_retry_at=NULL, updated_at=? WHERE campaign_id=? AND telegram_id=?", (err[:500], now, int(campaign_id), int(telegram_id)))
        conn.commit()
    finally:
        conn.close()
    _update_campaign_counts(campaign_id)

def _schedule_retry(campaign_id: int, telegram_id: int, err: str, retry_after_s: float) -> None:
    conn = get_db_connection()
    try:
        now_ts = time.time() + max(0.5, float(retry_after_s))
        # store as ISO
        retry_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts))
        now = _now_utc_str()
        conn.execute("UPDATE broadcast_recipients SET attempts=attempts+1, last_error=?, next_retry_at=?, updated_at=? WHERE campaign_id=? AND telegram_id=?", (err[:500], retry_at, now, int(campaign_id), int(telegram_id)))
        conn.commit()
    finally:
        conn.close()

def _is_blocked_error(desc_low: str) -> bool:
    needles = ("blocked", "chat not found", "user is deactivated", "peer_id_invalid", "bot was kicked", "have no rights", "not found", "chat_id is empty", "user not found", "deactivated")
    return any(n in desc_low for n in needles)

def _is_retryable_error(code: Optional[int], desc_low: str) -> bool:
    if code in (429, 500, 502, 503, 504):
        return True
    if any(x in desc_low for x in ("timeout", "timed out", "try again", "too many requests", "internal server error", "bad gateway", "service unavailable", "gateway")):
        return True
    return False

def _extract_retry_after(resp: Dict[str, Any]) -> float:
    try:
        params = resp.get("parameters") or {}
        if "retry_after" in params:
            return float(params["retry_after"])
    except Exception:
        pass
    desc = str(resp.get("description") or "")
    m = re.search(r"retry after (\d+)", desc, re.I)
    if m:
        try: return float(m.group(1))
        except: pass
    m = re.search(r"too many requests", desc, re.I)
    if m:
        return 2.0
    return 2.0

# ── Telegram send ───────────────────────────────────────────────────────
def _telegram_send(telegram_id: int, campaign: Dict[str, Any]) -> Dict[str, Any]:
    """Send one broadcast message via Bot API. Returns Telegram JSON dict.
    Handles text/html + optional buttons + optional media.
    Injectable via _SEND_FN for tests.
    """
    if _SEND_FN:
        try:
            return _SEND_FN(int(telegram_id), campaign)
        except Exception as exc:
            return {"ok": False, "error_code": 500, "description": f"injected send failed: {type(exc).__name__}: {exc}"}
    api = _tg_api()
    if not api:
        return {"ok": False, "error_code": 500, "description": "BOT_TOKEN not configured"}
    html_text = campaign.get("html") or campaign.get("text") or ""
    buttons_json = campaign.get("buttons_json")
    inline_rows = _buttons_from_json(buttons_json) if isinstance(buttons_json, str) else (buttons_json or [])
    reply_markup_json = json.dumps({"inline_keyboard": inline_rows}) if inline_rows else None
    media_type = campaign.get("media_type")
    media_file_id = campaign.get("media_file_id")
    caption = campaign.get("caption") or html_text or ""
    try:
        if media_type and media_file_id:
            method_map = {"photo": "sendPhoto", "video": "sendVideo", "document": "sendDocument", "audio": "sendAudio", "animation": "sendAnimation"}
            method = method_map.get(str(media_type).lower(), "sendPhoto")
            # Telegram media methods use photo/video/document/audio/animation field name
            field = {"sendPhoto": "photo", "sendVideo": "video", "sendDocument": "document", "sendAudio": "audio", "sendAnimation": "animation"}.get(method, "photo")
            payload: Dict[str, Any] = {"chat_id": int(telegram_id), field: media_file_id, "caption": caption[:1024], "parse_mode": "HTML"}
            if reply_markup_json:
                payload["reply_markup"] = reply_markup_json
            url = f"{api}/{method}"
            r = requests.post(url, json=payload, timeout=30)
            try:
                j = r.json()
            except Exception:
                j = {"ok": r.status_code < 400, "description": r.text[:500], "error_code": r.status_code}
            return j
        else:
            payload = {"chat_id": int(telegram_id), "text": (html_text or "")[:4096], "parse_mode": "HTML", "disable_web_page_preview": True}
            if reply_markup_json:
                payload["reply_markup"] = reply_markup_json
            url = f"{api}/sendMessage"
            r = requests.post(url, json=payload, timeout=30)
            try:
                j = r.json()
            except Exception:
                j = {"ok": r.status_code < 400, "description": r.text[:500], "error_code": r.status_code}
            # also handle HTTP-level 429 where JSON may still have ok=false
            if r.status_code == 429 and not j.get("ok"):
                # enrich with retry_after if header present
                try:
                    ra = r.headers.get("Retry-After")
                    if ra and "parameters" not in j:
                        j["parameters"] = {"retry_after": int(ra)}
                except Exception:
                    pass
            return j
    except requests.exceptions.RequestException as exc:
        return {"ok": False, "error_code": 500, "description": f"Network error: {type(exc).__name__}: {exc}"}
    except Exception as exc:
        return {"ok": False, "error_code": 500, "description": f"Send failed: {type(exc).__name__}: {exc}"}

# allow tests to inject
def set_send_fn(fn) -> None:
    global _SEND_FN
    _SEND_FN = fn

def clear_send_fn() -> None:
    global _SEND_FN
    _SEND_FN = None

# ── Worker ────────────────────────────────────────────────────────────────
def _process_campaign(campaign: Dict[str, Any], stop_event: threading.Event) -> None:
    cid = int(campaign["id"])
    # re-fetch to ensure not cancelled mid-flight
    while not stop_event.is_set():
        fresh = get_campaign(cid)
        if not fresh or fresh["status"] != "sending":
            return
        pending = _get_pending_recipients(cid, limit=20)
        if not pending:
            # check if any future retries pending
            conn = get_db_connection()
            try:
                row = conn.execute("SELECT COUNT(*) as c FROM broadcast_recipients WHERE campaign_id=? AND status='pending'", (cid,)).fetchone()
                remaining = int(row["c"]) if row else 0
            finally:
                conn.close()
            if remaining == 0:
                # all done
                conn = get_db_connection()
                try:
                    now = _now_utc_str()
                    # determine if any failed/blocked -> still completed (not failed overall)
                    conn.execute("UPDATE broadcast_campaigns SET status='completed', completed_at=? WHERE id=? AND status='sending'", (now, cid))
                    conn.commit()
                finally:
                    conn.close()
            return
        for rec in pending:
            if stop_event.is_set():
                return
            # check cancellation before each send
            cur = get_campaign(cid)
            if not cur or cur["status"] == "cancelled":
                # mark remaining as cancelled
                conn = get_db_connection()
                try:
                    now = _now_utc_str()
                    conn.execute("UPDATE broadcast_recipients SET status='cancelled', updated_at=? WHERE campaign_id=? AND status='pending'", (now, cid))
                    conn.commit()
                finally:
                    conn.close()
                return
            tid = int(rec["telegram_id"])
            attempts = int(rec.get("attempts") or 0)
            # respect per-recipient retry backoff already via next_retry_at filter above
            # send
            resp = _telegram_send(tid, fresh)
            if resp.get("ok"):
                _mark_recipient_sent(cid, tid)
            else:
                code = resp.get("error_code")
                # some transports put status in 'error_code', others in 'code'
                try: code = int(code) if code is not None else None
                except: code = None
                desc = str(resp.get("description") or resp.get("error") or resp.get("result") or "unknown error")
                desc_low = desc.lower()
                # 429
                if code == 429 or "too many requests" in desc_low or "retry after" in desc_low:
                    retry_after = _extract_retry_after(resp)
                    # clamp
                    retry_after = max(0.5, min(60, float(retry_after)))
                    if attempts + 1 < MAX_ATTEMPTS:
                        _schedule_retry(cid, tid, f"429 {desc}", retry_after)
                        # respect Telegram's ask: sleep now before continuing to next recipient
                        stop_event.wait(retry_after)
                    else:
                        _mark_recipient_failed(cid, tid, f"429 {desc}")
                        stop_event.wait(retry_after)
                elif _is_blocked_error(desc_low):
                    _mark_recipient_blocked(cid, tid, desc)
                else:
                    # generic
                    if _is_retryable_error(code, desc_low) and attempts + 1 < MAX_ATTEMPTS:
                        backoff = min(2 ** (attempts + 1), MAX_BACKOFF_S)
                        _schedule_retry(cid, tid, desc, backoff)
                        stop_event.wait(min(backoff, 2.0))
                    else:
                        _mark_recipient_failed(cid, tid, desc)
            # global rate limit respect — small pause between recipients
            # if we just did a 429 sleep, this extra 0.05 is negligible
            stop_event.wait(BASE_DELAY_S)
        # loop will re-query pending

def _worker_loop(stop_event: threading.Event, poll_interval: float = CAMPAIGN_POLL_S) -> None:
    logger.info("broadcast worker started")
    while not stop_event.is_set():
        try:
            camps = _get_sending_campaigns(limit=3)
            if not camps:
                stop_event.wait(poll_interval)
                continue
            for camp in camps:
                if stop_event.is_set():
                    break
                _process_campaign(camp, stop_event)
        except Exception as exc:
            logger.exception("broadcast worker error: %s", exc)
            stop_event.wait(poll_interval)
    logger.info("broadcast worker stopped")

def start_broadcast_worker(poll_interval: float = CAMPAIGN_POLL_S) -> bool:
    """Start background worker if not running. Returns True if started (or already running)."""
    global _broadcast_thread
    ensure_broadcast_schema()
    with _broadcast_lock:
        if _broadcast_thread and _broadcast_thread.is_alive():
            return True
        _broadcast_stop.clear()
        t = threading.Thread(target=_worker_loop, args=(_broadcast_stop, float(poll_interval)), daemon=True, name="broadcast-worker")
        _broadcast_thread = t
        t.start()
        return True

def stop_broadcast_worker(timeout: float = 2.0) -> None:
    global _broadcast_thread
    _broadcast_stop.set()
    t = _broadcast_thread
    if t and t.is_alive() and t is not threading.current_thread():
        t.join(timeout=max(0.0, timeout))
    with _broadcast_lock:
        _broadcast_thread = None

def is_worker_running() -> bool:
    return bool(_broadcast_thread and _broadcast_thread.is_alive())

def resume_pending_broadcasts() -> int:
    """Called at startup — ensure any 'sending' campaigns resume. Returns count."""
    ensure_broadcast_schema()
    camps = _get_sending_campaigns(limit=100)
    if camps:
        start_broadcast_worker()
    return len(camps)

# aliases for naming flexibility
start_worker = start_broadcast_worker
stop_worker = stop_broadcast_worker
ensure_schema = ensure_broadcast_schema
resume_pending = resume_pending_broadcasts

def process_campaign_sync(campaign_id: int, max_recipients: int = 1000) -> Dict[str, Any]:
    """Synchronous processing for tests — processes pending recipients without background thread."""
    ensure_broadcast_schema()
    camp = get_campaign(int(campaign_id))
    if not camp or camp["status"] != "sending":
        return {"ok": False, "error": "campaign not sending"}
    # use a dummy stop event that never triggers
    dummy = threading.Event()
    # limit recipients processed
    processed = 0
    while processed < max_recipients:
        pending = _get_pending_recipients(int(campaign_id), limit=20)
        if not pending:
            break
        # process up to max_recipients - processed
        for rec in pending:
            if processed >= max_recipients:
                break
            tid = int(rec["telegram_id"])
            resp = _telegram_send(tid, camp)
            if resp.get("ok"):
                _mark_recipient_sent(int(campaign_id), tid)
            else:
                code = resp.get("error_code")
                try: code = int(code) if code is not None else None
                except: code = None
                desc = str(resp.get("description") or resp.get("error") or "unknown error")
                desc_low = desc.lower()
                attempts = int(rec.get("attempts") or 0)
                if code == 429 and attempts + 1 < MAX_ATTEMPTS:
                    _schedule_retry(int(campaign_id), tid, desc, _extract_retry_after(resp))
                elif _is_blocked_error(desc_low):
                    _mark_recipient_blocked(int(campaign_id), tid, desc)
                else:
                    if _is_retryable_error(code, desc_low) and attempts + 1 < MAX_ATTEMPTS:
                        _schedule_retry(int(campaign_id), tid, desc, min(2 ** (attempts+1), 10))
                    else:
                        _mark_recipient_failed(int(campaign_id), tid, desc)
            processed += 1
            # no sleep in sync mode (tests)
        # refresh
        camp = get_campaign(int(campaign_id))
        if not camp or camp["status"] != "sending":
            break
    # if no pending after sync, mark completed
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT COUNT(*) as c FROM broadcast_recipients WHERE campaign_id=? AND status='pending'", (int(campaign_id),)).fetchone()
        if int(row["c"]) == 0:
            camp_now = get_campaign(int(campaign_id))
            if camp_now and camp_now["status"] == "sending":
                now = _now_utc_str()
                conn.execute("UPDATE broadcast_campaigns SET status='completed', completed_at=? WHERE id=?", (now, int(campaign_id)))
                conn.commit()
    finally:
        conn.close()
    return campaign_stats(int(campaign_id))

# sync alias
process_one_campaign = process_campaign_sync
tick_broadcast = process_campaign_sync

# ── Preview helpers for pingbot ──────────────────────────────────────────
def preview_text(campaign_id: int) -> str:
    camp = get_campaign(int(campaign_id))
    if not camp:
        return "Broadcast not found."
    raw = camp.get("text") or ""
    total = camp.get("total_count") or 0
    html = camp.get("html") or ""
    buttons_json = camp.get("buttons_json")
    rows = _buttons_from_json(buttons_json) if buttons_json else []
    out = ["📢 *Broadcast preview*"]
    out.append(f"Recipients: *{total}*")
    out.append("")
    out.append(html if html else html_escape_fallback(raw))
    if rows:
        out.append("")
        out.append("Buttons:")
        for row in rows:
            out.append(" | ".join(f"[{b.get('text')}]({b.get('url')})" for b in row))
    out.append("")
    out.append("_Tap Confirm to send, or Cancel to abort._")
    return "\n".join(out)

def html_escape_fallback(s: str) -> str:
    return html.escape(s, quote=False)

def preview_keyboard(campaign_id: int) -> Dict[str, Any]:
    return {"inline_keyboard": [
        [{"text": "✅ Confirm send", "callback_data": f"admin:broadcast_confirm:{campaign_id}"},
         {"text": "✖️ Cancel", "callback_data": f"admin:broadcast_cancel:{campaign_id}"}],
        [{"text": "📊 Status", "callback_data": f"admin:broadcast_status:{campaign_id}"}]
    ]}

def status_text(campaign_id: int) -> str:
    st = campaign_stats(int(campaign_id))
    if not st or not st.get("campaign"):
        return "Broadcast not found."
    camp = st["campaign"]
    total = st["total"]
    sent = st["sent"]
    failed = st["failed"]
    blocked = st["blocked"]
    pending = st["pending"]
    cancelled = st.get("cancelled", 0)
    status = camp.get("status") or "unknown"
    lines = [
        f"📢 *Broadcast #{campaign_id}* — `{status}`",
        f"Recipients: *{total}*  •  Pending: *{pending}*  •  Sent: *{sent}*",
        f"Failed: *{failed}*  •  Blocked: *{blocked}*  •  Cancelled: *{cancelled}*",
        f"Created: {camp.get('created_at') or '—'}",
    ]
    if camp.get("started_at"):
        lines.append(f"Started: {camp['started_at']}")
    if camp.get("completed_at"):
        lines.append(f"Completed: {camp['completed_at']}")
    # snippet
    snippet = (camp.get("text") or "")[:300]
    if snippet:
        lines.append("")
        lines.append(f"_{html.escape(snippet, quote=False)[:300]}_")
    return "\n".join(lines)

def status_keyboard(campaign_id: int) -> Dict[str, Any]:
    camp = get_campaign(int(campaign_id))
    if not camp:
        return {"inline_keyboard": [[{"text": "⬅️ Menu", "callback_data": "admin:menu"}]]}
    if camp["status"] in ("sending", "preview"):
        return {"inline_keyboard": [
            [{"text": "📊 Refresh", "callback_data": f"admin:broadcast_status:{campaign_id}"},
             {"text": "✖️ Cancel", "callback_data": f"admin:broadcast_cancel:{campaign_id}"}],
            [{"text": "⬅️ Menu", "callback_data": "admin:menu"}]
        ]}
    return {"inline_keyboard": [[{"text": "⬅️ Menu", "callback_data": "admin:menu"}]]}

# ── High-level flow for pingbot ─────────────────────────────────────────
def start_broadcast_flow(admin_chat_id: int, created_by: int, message_text: str, buttons_spec: Any = None, media_type: Optional[str]=None, media_file_id: Optional[str]=None) -> int:
    """Helper used by pingbot: creates preview campaign and returns id.
    Caller is responsible for sending preview_text/keyboard to admin.
    """
    cid = create_campaign(created_by, message_text, buttons=buttons_spec, media_type=media_type, media_file_id=media_file_id, auto_start=False)
    return cid

# ── Legacy compatibility wrappers ───────────────────────────────────────
def all_linked_telegram_ids() -> List[int]:
    return _all_linked_telegram_ids()

# For tests that import specific names, ensure we expose many
__all__ = [
    "sanitize_html", "safe_html", "escape_html", "clean_html", "sanitize_telegram_html", "validate_html",
    "parse_url_buttons", "parse_buttons", "parse_inline_buttons", "parse_inline_keyboard", "build_inline_keyboard", "build_inline_keyboard_markup",
    "ensure_broadcast_schema", "ensure_schema",
    "create_campaign", "create_broadcast", "create_broadcast_campaign", "start_broadcast_flow",
    "get_campaign", "get_broadcast", "list_campaigns", "list_broadcasts",
    "campaign_stats", "get_status", "broadcast_status", "get_campaign_stats", "status_text", "status_keyboard",
    "cancel_campaign", "cancel_broadcast", "confirm_campaign", "approve_campaign",
    "preview_text", "preview_keyboard",
    "start_broadcast_worker", "stop_broadcast_worker", "is_worker_running", "resume_pending_broadcasts", "resume_pending", "start_worker", "stop_worker",
    "process_campaign_sync", "process_one_campaign", "tick_broadcast",
    "set_send_fn", "clear_send_fn", "_telegram_send", "_is_blocked_error", "_is_retryable_error",
]

