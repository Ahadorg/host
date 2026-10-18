"""Extra admin views for the /admin chat panel: runners, jobs, audit log,
abuse reports, and a security-clusters summary.

WHY A SEPARATE FILE
--------------------
telegram_link.py already holds the user-management admin functions
(admin_overview_stats, set_admin, set_zip_permission, ...). This file adds
the REST of what routes/admin.py exposes on the website, kept apart so
telegram_link.py stays about identity/linking rather than growing into a
second admin.py. Every function here is read-mostly and mirrors an existing
website query — see the matching route in routes/admin.py in each
docstring — so behaviour stays consistent between the two surfaces.
"""

import ipaddress
import logging
import socket
import time
from urllib.parse import urlparse

from database import get_db_connection
from services import runner_client
from services import bot_ops
from routes.deps import now_utc_str

ADMIN_HEALTH_MAX_AGE_S = 30

logger = logging.getLogger("codenest-telegram-admin-ext")


# ── Telegram-level ban — new, not on the website ────────────────────────
# is_suspended on the website always needs a users row to attach to; this
# blocks a raw Telegram id before any account/link exists at all, e.g. for
# someone spamming /start or /code without ever going through the site.

def is_banned(telegram_id: int) -> bool:
    if not telegram_id:
        return False
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT 1 FROM banned_telegram_ids WHERE telegram_id = ?",
                            (telegram_id,)).fetchone()
        return bool(row)
    finally:
        conn.close()


def ban_telegram_id(telegram_id: int, banned_by: int, reason: str = "") -> None:
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO banned_telegram_ids (telegram_id, banned_by, reason, created_at) "
            "VALUES (?,?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET reason=excluded.reason",
            (telegram_id, banned_by, reason, now_utc_str()))
        conn.commit()
    finally:
        conn.close()


def unban_telegram_id(telegram_id: int) -> bool:
    conn = get_db_connection()
    try:
        cur = conn.execute("DELETE FROM banned_telegram_ids WHERE telegram_id = ?", (telegram_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_banned(limit: int = 30) -> list:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT telegram_id, reason, created_at FROM banned_telegram_ids "
            "ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Per-user job limit override — new, not on the website ──────────────
# MAX_JOBS_PER_USER is currently one fixed number for every account. This
# lets an admin raise (or lower) the ceiling for one specific person
# without touching the global constant.

def set_job_limit_override(user_id: int, limit) -> None:
    """limit=None clears the override, back to the global default."""
    conn = get_db_connection()
    try:
        conn.execute("UPDATE users SET job_limit_override = ?, updated_at = ? WHERE id = ?",
                     (limit, now_utc_str(), user_id))
        conn.commit()
    finally:
        conn.close()


# ── Broadcast — legacy, main-branch implementation ─────────────────────
# Sending happens in pingbot.py (it owns the Telegram API call); this returns
# the active, linked recipients. Kept unchanged while the health fix is merged.

def all_linked_telegram_ids() -> list:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT telegram_id FROM users WHERE telegram_id IS NOT NULL AND is_suspended = 0"
        ).fetchall()
        return [r["telegram_id"] for r in rows]
    finally:
        conn.close()


# ── Admin-level job control — new, not on the website ───────────────────
# routes/admin.py's job views are read-only (list + detail). An admin
# could not restart, stop, or delete someone ELSE's job without going
# into their own account — bot_ops' restart/stop/delete are deliberately
# owner-scoped (see bot_ops.find_app's docstring) and stay that way; these
# are separate, admin-only entry points into the SAME runner calls.

def admin_find_job(job_id: int) -> dict:
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def admin_restart_job(job_id: int) -> dict:
    row = admin_find_job(job_id)
    if not row:
        return {"ok": False, "error": "No such job."}
    return bot_ops._act(row["user_id"], str(row["id"]), "restart")


def admin_stop_job(job_id: int) -> dict:
    row = admin_find_job(job_id)
    if not row:
        return {"ok": False, "error": "No such job."}
    return bot_ops._act(row["user_id"], str(row["id"]), "stop")


def admin_delete_job(job_id: int) -> dict:
    row = admin_find_job(job_id)
    if not row:
        return {"ok": False, "error": "No such job."}
    return bot_ops.delete(row["user_id"], str(row["id"]))


def jobs_for_user(user_id: int) -> list:
    """Every job this account owns, with live status + runner placement.

    Used by the admin user card so a tap on a person shows their apps as
    buttons, not a paragraph to retype into /see.
    """
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, name, language, runner_job_id, worker_url, desired_state, "
            "repo_url, repo_entry, repo_commit, created_at, updated_at, "
            "telegram_bot_username "
            "FROM jobs WHERE user_id = ? ORDER BY id DESC", (user_id,)
        ).fetchall()
    finally:
        conn.close()
    live = runner_client.fleet_jobs()
    out = []
    for r in rows:
        d = dict(r)
        info = live.get(d.get("runner_job_id")) or {}
        d["live_status"] = info.get("status") or (
            "stopped" if d.get("desired_state") == "stopped" else "unknown")
        d["mem_mb"] = info.get("mem_mb")
        d["uptime_s"] = info.get("uptime_s")
        d["restarts"] = info.get("restarts")
        d["last_exit_reason"] = info.get("last_exit_reason")
        d["web"] = bool(info.get("web"))
        d["port"] = info.get("port")
        d["runner_url"] = info.get("worker_url") or d.get("worker_url") or ""
        out.append(d)
    return out


def job_full_detail_with_code(user_id: int, job_ref: str) -> dict:
    """Unlike job_detail() above, this INCLUDES the actual source code —
    an explicit investigation tool for /see, not the general admin job
    view. Scoped to the given user_id so /see <them> <job> can only ever
    return code that user actually owns, never an arbitrary job id."""
    conn = get_db_connection()
    try:
        if job_ref.isdigit():
            row = conn.execute(
                "SELECT * FROM jobs WHERE user_id = ? AND id = ?",
                (user_id, int(job_ref))).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM jobs WHERE user_id = ? AND LOWER(name) = LOWER(?)",
                (user_id, job_ref)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    d = dict(row)
    live = runner_client.fleet_jobs()
    info = live.get(d.get("runner_job_id")) or {}
    d.update(live_status=info.get("status"), uptime_s=info.get("uptime_s"),
              mem_mb=info.get("mem_mb"), restarts=info.get("restarts"))
    return d


# ── Search — new, not on the website (which only paginates, no search box) ──

def search_users(query: str, limit: int = 15) -> list:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, username, telegram_id, is_admin, can_upload_zip, is_suspended "
            "FROM users WHERE username LIKE ? OR email LIKE ? ORDER BY id DESC LIMIT ?",
            (f"%{query}%", f"%{query}%", limit)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def search_jobs(query: str, limit: int = 15) -> list:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT j.id, j.name, j.language, u.username AS owner FROM jobs j "
            "JOIN users u ON u.id = j.user_id WHERE j.name LIKE ? "
            "ORDER BY j.id DESC LIMIT ?", (f"%{query}%", limit)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Recent signups — new, not on the website ────────────────────────────

def recent_signups(hours: int = 24, limit: int = 20) -> list:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, username, email, telegram_id, created_at FROM users "
            "WHERE created_at >= datetime('now', ?) ORDER BY id DESC LIMIT ?",
            (f"-{hours} hours", limit)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── CSV export — new, not on the website ────────────────────────────────

def export_users_csv() -> str:
    import csv, io as _io
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, username, email, telegram_id, is_admin, can_upload_zip, "
            "is_suspended, job_limit_override, created_at FROM users ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    buf = _io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "username", "email", "telegram_id", "is_admin", "can_upload_zip",
                "is_suspended", "job_limit_override", "created_at"])
    for r in rows:
        w.writerow([r[k] for k in ("id", "username", "email", "telegram_id", "is_admin",
                                    "can_upload_zip", "is_suspended", "job_limit_override", "created_at")])
    return buf.getvalue()


def export_jobs_csv() -> str:
    import csv, io as _io
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT j.id, j.name, j.language, u.username AS owner, j.runner_job_id, j.created_at "
            "FROM jobs j JOIN users u ON u.id = j.user_id ORDER BY j.id"
        ).fetchall()
    finally:
        conn.close()
    buf = _io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "name", "language", "owner", "runner_job_id", "created_at"])
    for r in rows:
        w.writerow([r[k] for k in ("id", "name", "language", "owner", "runner_job_id", "created_at")])
    return buf.getvalue()


# ── Delete runner — new, not on the website (which only enables/disables) ──

def delete_runner(runner_id: int) -> dict:
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT label FROM runner_nodes WHERE id=?", (runner_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "No such runner."}
        conn.execute("DELETE FROM runner_nodes WHERE id=?", (runner_id,))
        conn.commit()
        runner_client.invalidate_runner_registry()
        return {"ok": True, "label": row["label"]}
    finally:
        conn.close()


# ── New-signup / abuse-report notifier — new, not on the website ───────────
# A periodic watcher (same shape as services/bot_notify.py's crash watcher)
# rather than a hook inside routes/auth.py or the abuse-report endpoint —
# keeps this feature from ever touching signup/report code paths directly.
# State (highest id already notified) lives in a tiny one-row table so a
# restart doesn't re-announce everything.

def _notify_state_get(key: str) -> int:
    conn = get_db_connection()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS tg_notify_state (key TEXT PRIMARY KEY, last_id INTEGER)")
        conn.commit()  # Postgres rolls DDL back on close() without this — the
        # table was being created and then immediately un-created every call,
        # which is why every INSERT after it failed with "relation ... does
        # not exist".
        row = conn.execute("SELECT last_id FROM tg_notify_state WHERE key=?", (key,)).fetchone()
        return row["last_id"] if row else 0
    finally:
        conn.close()


def _notify_state_set(key: str, last_id: int) -> None:
    conn = get_db_connection()
    try:
        conn.execute("INSERT INTO tg_notify_state (key, last_id) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET last_id=excluded.last_id", (key, last_id))
        conn.commit()
    finally:
        conn.close()


def check_new_signups_and_reports() -> dict:
    """Call this every ~60s from a background loop. Returns what's new
    since the last call (and marks it seen), for the caller to message
    admins with."""
    conn = get_db_connection()
    try:
        last_user = _notify_state_get("users")
        new_users = conn.execute(
            "SELECT id, username, email FROM users WHERE id > ? ORDER BY id", (last_user,)
        ).fetchall()
        last_report = _notify_state_get("abuse_reports")
        new_reports = conn.execute(
            "SELECT id, url, reason FROM abuse_reports WHERE id > ? ORDER BY id", (last_report,)
        ).fetchall()
    finally:
        conn.close()
    if new_users:
        _notify_state_set("users", max(r["id"] for r in new_users))
    if new_reports:
        _notify_state_set("abuse_reports", max(r["id"] for r in new_reports))
    return {"users": [dict(r) for r in new_users], "reports": [dict(r) for r in new_reports]}


# ── Runners / worker pool — mirrors GET /admin/runners ─────────────────


# ── Maintenance mode — new, not on the website ──────────────────────────
# A single flag other routes can check (app.py would need one line added
# to actually refuse traffic on it — this gives admins the on/off switch
# and the state; wiring every route to respect it is a separate, larger
# change to app.py itself, flagged rather than done silently here).

def get_maintenance_mode() -> bool:
    conn = get_db_connection()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS tg_notify_state (key TEXT PRIMARY KEY, last_id INTEGER)")
        conn.commit()  # same missing-commit bug as _notify_state_get above
        row = conn.execute("SELECT last_id FROM tg_notify_state WHERE key='maintenance'").fetchone()
        return bool(row and row["last_id"])
    finally:
        conn.close()


def set_maintenance_mode(on: bool) -> None:
    _notify_state_set("maintenance", 1 if on else 0)


# ── Terms-agreement status — new, not on the website ────────────────────

def terms_status_summary() -> dict:
    conn = get_db_connection()
    try:
        total = dict(conn.execute("SELECT COUNT(*) AS c FROM users").fetchone())["c"]
        agreed = dict(conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE agreed_terms_at IS NOT NULL").fetchone())["c"]
        return {"total": total, "agreed": agreed, "not_agreed": total - agreed}
    finally:
        conn.close()


def users_without_terms(limit: int = 15) -> list:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, username, telegram_id FROM users WHERE agreed_terms_at IS NULL "
            "ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Last-seen — new, not on the website ─────────────────────────────────

def last_seen_for_user(user_id: int):
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT MAX(last_seen) AS ls, ip_address FROM sessions WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row and row["ls"] else None
    finally:
        conn.close()


# ── Bulk suspend — new, not on the website ──────────────────────────────

def bulk_suspend(user_ids: list, value: bool) -> dict:
    from services import telegram_link as _tl
    ok, failed = [], []
    for uid in user_ids:
        try:
            _tl.set_suspended(int(uid), value)
            ok.append(uid)
        except Exception:
            failed.append(uid)
    return {"ok": ok, "failed": failed}


# ── Store moderation queue — new, not on the website's admin panel ─────
# (the website has a separate store-review surface; this brings the same
# pending queue into the chat panel)

def store_pending(limit: int = 10) -> list:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, title, author_name, created_at FROM store_items "
            "WHERE status = 'pending' ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def store_set_status(item_id: int, status: str, note: str = "") -> bool:
    conn = get_db_connection()
    try:
        now = now_utc_str()
        cur = conn.execute(
            "UPDATE store_items SET status=?, review_note=?, updated_at=?, "
            "published_at=CASE WHEN ?='approved' THEN ? ELSE published_at END WHERE id=?",
            (status, note, now, status, now, item_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── Bot revision history + rollback — new, not on the website ──────────

def job_revisions(job_id: int, limit: int = 10) -> list:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, version, action, status, created_at FROM bot_revisions "
            "WHERE job_id = ? ORDER BY version DESC LIMIT ?", (job_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def rollback_job(job_id: int, revision_id: int) -> dict:
    """Redeploys the code stored in a past revision — same update_code path
    everything else uses, so it goes through the runner properly instead of
    just rewriting the jobs row."""
    conn = get_db_connection()
    try:
        rev = conn.execute("SELECT * FROM bot_revisions WHERE id=? AND job_id=?",
                            (revision_id, job_id)).fetchone()
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    finally:
        conn.close()
    if not rev or not job:
        return {"ok": False, "error": "Revision or job not found."}
    return bot_ops.update_code(job["user_id"], str(job["id"]), rev["code"], rev["language"])


# ── IP / fingerprint cluster drill-down — new, not on the website's
# summary-only view; this lists the actual clustered accounts. ─────────

def fingerprint_clusters(limit: int = 10) -> list:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT fingerprint, GROUP_CONCAT(DISTINCT user_id) AS uids, COUNT(DISTINCT user_id) AS n "
            "FROM sessions WHERE fingerprint IS NOT NULL "
            "GROUP BY fingerprint HAVING n > 1 ORDER BY n DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            uids = [int(x) for x in (r["uids"] or "").split(",") if x]
            unames = conn.execute(
                f"SELECT username FROM users WHERE id IN ({','.join('?'*len(uids))})", uids
            ).fetchall() if uids else []
            out.append({"fingerprint": r["fingerprint"][:16], "count": r["n"],
                        "usernames": [u["username"] for u in unames]})
        return out
    finally:
        conn.close()


# ── Runner secret rotate — new, not on the website ──────────────────────

def rotate_runner_secret(runner_id: int, new_secret: str) -> dict:
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT label, url FROM runner_nodes WHERE id=?", (runner_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return {"ok": False, "error": "No such runner."}
    return add_runner(row["label"], row["url"], new_secret, None)  # re-validates + re-saves


# ── Admin-filtered audit log — new, not on the website ──────────────────

def audit_log_by_admin(admin_username: str, limit: int = 10) -> list:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT a.id, a.action, a.target, a.created_at FROM admin_audit_log a "
            "JOIN users u ON u.id = a.admin_id WHERE u.username = ? "
            "ORDER BY a.id DESC LIMIT ?", (admin_username, limit)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_admins() -> list:
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT id, username FROM users WHERE is_admin=1").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Fresh health check — new, not on the website (which only shows the
# last cached health poll) ──────────────────────────────────────────────

def runners_health_now() -> dict:
    return runners_overview(refresh=True)


def runners_overview(refresh: bool = False) -> dict:
    conn = get_db_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id,label,url,enabled FROM runner_nodes ORDER BY id"
        ).fetchall()]
    finally:
        conn.close()
    health = runner_client.worker_health(refresh=refresh, max_age_s=ADMIN_HEALTH_MAX_AGE_S) or {}
    # annotate exclusive queen pins so admin sees why a runner is avoided for normal users
    try:
        excl = runner_client.list_exclusive_runners()
    except Exception:
        excl = {}
    for row in rows:
        h = health.get(row["url"]) or {}
        row.update(online=bool(h.get("online")), jobs=h.get("jobs", 0),
                   capacity=h.get("capacity", 0), mem_mb=h.get("mem_mb", 0),
                   free_mb=h.get("free_mb", 0), total_mb=h.get("total_mb", 0),
                   exclusive_owner=excl.get(row["url"].rstrip("/")) )
    embedded = None
    if runner_client.embedded_mode() or runner_client._has_embedded_assignments():
        try:
            h = runner_client._runner_http("GET", "/health", worker="embedded").json()
            embedded = {"online": True, "jobs": h.get("jobs", 0), "capacity": h.get("capacity", 0)}
        except Exception:
            embedded = {"online": False, "jobs": 0, "capacity": 0}
    return {"runners": rows, "embedded": embedded}


_TRANSIENT_STATUSES = (408, 425, 429, 500, 502, 503, 504)


def _validate_runner_url(url: str) -> str:
    """Same URL rules as the website's 'Add runner'. Returns '' when fine,
    otherwise a human-readable error."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        return "Paste the runner's public https:// URL (e.g. https://my-runner.onrender.com)."
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, 443)}
        if not addresses or any(ipaddress.ip_address(ip).is_private
                                or ipaddress.ip_address(ip).is_loopback
                                or ipaddress.ip_address(ip).is_link_local
                                for ip in addresses):
            raise ValueError("private address")
    except (OSError, ValueError):
        return "Runner URL must resolve to a public service."
    return ""


def _fallback_admin_id(conn):
    """created_by is NOT NULL (and a foreign key to users). The chat's own
    account is used when it exists; the owner of the Telegram super-admin id
    often has no linked site account, so fall back to a real admin user."""
    row = conn.execute("SELECT id FROM users WHERE is_admin=1 ORDER BY id LIMIT 1").fetchone()
    if not row:
        row = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    return row["id"] if row else None


def add_runner(label: str, url: str, secret: str, created_by) -> dict:
    """Same validation and same writes as the website's 'Add runner'
    (routes/admin.py admin_add_runner):
    1) URL must be a public https:// address.
    2) GET /health must return 200 (retried — a sleeping Render free
       service answers 502/503 while it wakes up).
    3) An authenticated call must succeed with the given secret.
    Only then is it written to runner_nodes. Never raises: every failure
    comes back as {"ok": False, "error": ...} so the chat always answers."""
    import requests as _requests
    from services import secrets_store

    label = (label or "").strip()[:60] or "Runner"
    url = (url or "").strip().rstrip("/")
    secret = (secret or "").strip()
    if not secret:
        return {"ok": False, "error": "The runner secret is empty."}
    bad_url = _validate_runner_url(url)
    if bad_url:
        return {"ok": False, "error": bad_url}

    health = None
    for attempt in range(4):
        try:
            health = _requests.get(url + "/health", timeout=12)
            if health.status_code == 200:
                break
            if health.status_code not in _TRANSIENT_STATUSES:
                break
        except _requests.RequestException:
            pass
        if attempt < 3:
            time.sleep(2)
    if health is None:
        return {"ok": False, "error": "Runner is unreachable after four attempts. Confirm the "
                                       "Render service is Live, then open its /health URL."}
    if health.status_code != 200:
        if health.status_code == 404:
            detail = ("The /health endpoint returned 404. Deploy the repository with Root "
                      "Directory set to runner, not the main website.")
        elif health.status_code in (502, 503, 504):
            detail = ("Render is still starting the runner. Open /health until it returns "
                      "status 200, then try again.")
        else:
            detail = f"{url}/health returned HTTP {health.status_code}, expected 200."
        return {"ok": False, "error": detail}
    try:
        auth_check = _requests.get(url + "/internal/jobs",
                                   headers={"Authorization": "Bearer " + secret}, timeout=12)
    except _requests.RequestException:
        return {"ok": False, "error": "Health works, but the authenticated endpoint did not respond."}
    if auth_check.status_code in (401, 403):
        return {"ok": False, "error": "Runner is healthy, but that secret doesn't match "
                                       "RUNNER_SERVICE_SECRET on that Render service."}
    if auth_check.status_code == 503:
        try:
            remote_detail = str((auth_check.json() or {}).get("detail") or "")
        except Exception:
            remote_detail = ""
        if "secret not configured" in remote_detail.lower():
            return {"ok": False, "error": "Runner is healthy, but RUNNER_SERVICE_SECRET is missing in "
                                           "the runner's Render Environment. Add it there and redeploy."}
    if auth_check.status_code != 200:
        return {"ok": False, "error": f"Auth check returned HTTP {auth_check.status_code}, expected 200."}

    try:
        had_remote_pool = bool(runner_client.runner_pool())
    except Exception:
        had_remote_pool = True
    conn = get_db_connection()
    try:
        owner_id = created_by or _fallback_admin_id(conn)
        if not owner_id:
            return {"ok": False, "error": "No site account exists yet to own this runner. "
                                           "Sign up on the website once, then try again."}
        existing = conn.execute("SELECT id FROM runner_nodes WHERE url=?", (url,)).fetchone()
        now = now_utc_str()
        stored_secret = secrets_store.pack_env({"secret": secret})
        if existing:
            conn.execute("UPDATE runner_nodes SET label=?,encrypted_secret=?,enabled=1,updated_at=? WHERE id=?",
                         (label, stored_secret, now, existing["id"]))
            node_id = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO runner_nodes (label,url,encrypted_secret,enabled,created_by,created_at,updated_at) "
                "VALUES (?,?,?,1,?,?,?)", (label, url, stored_secret, owner_id, now, now))
            node_id = cur.lastrowid
        if not had_remote_pool:
            # Keep jobs already running in the in-process engine addressable.
            conn.execute("UPDATE jobs SET worker_url='embedded' "
                         "WHERE worker_url IS NULL AND runner_job_id IS NOT NULL")
        conn.execute(
            "INSERT INTO admin_audit_log (admin_id, action, target, details, created_at) "
            "VALUES (?,?,?,?,?)", (owner_id, "runner_add", url, label, now))
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("add_runner: saving to the database failed")
        return {"ok": False, "error": f"Runner checked OK, but saving it failed: "
                                       f"{type(exc).__name__}: {str(exc)[:200]}"}
    finally:
        conn.close()
    runner_client.invalidate_runner_registry()
    return {"ok": True, "id": node_id, "label": label}


def toggle_runner(runner_id: int) -> dict:
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT id,label,enabled FROM runner_nodes WHERE id=?",
                            (runner_id,)).fetchone()
        if not row:
            return None
        new_val = 0 if row["enabled"] else 1
        conn.execute("UPDATE runner_nodes SET enabled=?,updated_at=? WHERE id=?",
                     (new_val, now_utc_str(), runner_id))
        conn.commit()
        runner_client.invalidate_runner_registry()
        return {"id": row["id"], "label": row["label"], "enabled": new_val}
    finally:
        conn.close()


# ── Jobs — mirrors GET /admin/jobs and /admin/jobs/{id}, metadata only ──
# Never the code — same privacy rule the website route follows.

def jobs_recent(limit: int = 8, offset: int = 0) -> list:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT j.id, j.name, j.language, j.runner_job_id, "
            "u.username AS owner, u.id AS owner_id "
            "FROM jobs j JOIN users u ON u.id = j.user_id "
            "ORDER BY j.id DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        total = dict(conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone())["c"]
    finally:
        conn.close()
    live = runner_client.fleet_jobs()
    out = []
    for r in rows:
        d = dict(r)
        info = live.get(d.get("runner_job_id")) or {}
        d["live_status"] = info.get("status") or "unknown"
        out.append(d)
    return out, total


def job_detail(job_id: int) -> dict:
    """Full admin view of one job — everything the card needs, NO source body.

    Source is fetched on demand (admin:jobsource) so a list of ten jobs does not
    pull ten copies of multi-megabyte code into one chat message. Env VALUES are
    never returned — only the key names — because a bot token in an admin chat
    is a permanent leak into Telegram's cloud history.
    """
    import re as _re
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT j.id, j.name, j.language, j.created_at, j.updated_at, "
            "j.runner_job_id, j.worker_url, j.desired_state, "
            "j.telegram_bot_username, j.telegram_check_status, j.telegram_bot_id, "
            "j.repo_url, j.repo_entry, j.repo_commit, j.auto_deploy, j.env, "
            "j.code, "
            "u.username AS owner, u.id AS owner_id, u.is_suspended AS owner_suspended, "
            "u.telegram_id AS owner_telegram_id "
            "FROM jobs j JOIN users u ON u.id = j.user_id WHERE j.id = ?", (job_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    d = dict(row)
    try:
        from services import secrets_store
        env_vals, _ok = secrets_store.read_env(d.get("env"))
        d["env_keys"] = sorted((env_vals or {}).keys())
    except Exception:
        d["env_keys"] = []
    code = d.get("code") or ""
    d["has_code"] = bool(code.strip())
    d["code_bytes"] = len(code.encode("utf-8")) if code else 0
    d["requirements"] = ""
    for line in code.splitlines()[:30]:
        m = _re.match(r"^#\s*requirements:\s*(.+)$", line, _re.I)
        if m:
            d["requirements"] = m.group(1).strip()
            break
    d.pop("code", None)
    d.pop("env", None)

    live = runner_client.fleet_jobs()
    info = live.get(d.get("runner_job_id")) or {}
    d.update(
        live_status=info.get("status") or (
            "stopped" if d.get("desired_state") == "stopped" else "unknown"),
        uptime_s=info.get("uptime_s"),
        mem_mb=info.get("mem_mb"),
        peak_mem_mb=info.get("peak_mem_mb"),
        restarts=info.get("restarts"),
        last_exit_reason=info.get("last_exit_reason"),
        last_exit_code=info.get("last_exit_code"),
        libs=info.get("libs") or [],
        web=bool(info.get("web")),
        port=info.get("port"),
        web_slug=info.get("web_slug"),
        mem_limit_mb=info.get("mem_limit_mb"),
        runner_url=(info.get("worker_url") or d.get("worker_url") or ""),
        live_repo_commit=info.get("repo_commit"),
    )
    return d


def job_source(job_id: int) -> dict:
    """The stored source for one job, for admin download. Body included."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT id, name, language, code, user_id, repo_url, repo_entry "
            "FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def admin_update_code(job_id: int, code: str, language: str = None) -> dict:
    """Admin rewrite of someone else's source — same rails as owner /update."""
    row = admin_find_job(job_id)
    if not row:
        return {"ok": False, "error": "No such job."}
    return bot_ops.update_code(row["user_id"], str(row["id"]), code, language)


def admin_set_env(job_id: int, key: str, value) -> dict:
    row = admin_find_job(job_id)
    if not row:
        return {"ok": False, "error": "No such job."}
    return bot_ops.set_env(row["user_id"], str(row["id"]), key, value)


def admin_logs(job_id: int, lines: int = 40) -> dict:
    row = admin_find_job(job_id)
    if not row:
        return {"ok": False, "error": "No such job."}
    return bot_ops.logs(row["user_id"], str(row["id"]), lines=lines)


# ── Audit log — mirrors GET /admin/audit-log ────────────────────────────

def audit_log_recent(limit: int = 10) -> list:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT a.id, a.action, a.target, a.created_at, u.username AS admin_name "
            "FROM admin_audit_log a LEFT JOIN users u ON u.id = a.admin_id "
            "ORDER BY a.id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Abuse reports — mirrors GET /admin/abuse-reports ────────────────────

def abuse_reports_open(limit: int = 10) -> list:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, url, reason, status, created_at FROM abuse_reports "
            "WHERE status != 'resolved' ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def resolve_abuse_report(report_id: int) -> bool:
    conn = get_db_connection()
    try:
        cur = conn.execute("UPDATE abuse_reports SET status='resolved' WHERE id=?", (report_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── Security clusters — summary only, not a full drill-down. The website's
# fingerprint/IP clustering views are investigative tools meant for a wide
# screen and cross-referencing; a chat message just surfaces the COUNT so
# an admin knows whether it's worth opening the website. ───────────────

def security_clusters_summary() -> dict:
    conn = get_db_connection()
    try:
        fp = dict(conn.execute(
            "SELECT COUNT(*) AS c FROM (SELECT fingerprint FROM users "
            "WHERE fingerprint IS NOT NULL GROUP BY fingerprint HAVING COUNT(*) > 1)"
        ).fetchone())["c"] if _table_has_column(conn, "users", "fingerprint") else 0
        return {"fingerprint_clusters": fp}
    finally:
        conn.close()


def _table_has_column(conn, table, col) -> bool:
    try:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        return col in cols
    except Exception:
        return False
