"""
Telegram Bot - Advanced RunSpace Controller (Pure requests)
Features:
- /code <name> → create a new app (send code as text or a file next)
- /update <name> → redeploy an existing app in place (text or a file next)
- Inline buttons after deploy
- Real logs, Uptime, Download DB
"""
import io
import ipaddress
import socket
import tempfile
import json
import os
import re
import threading
import time
import zipfile
import requests
from collections import defaultdict
from urllib.parse import quote, urljoin, urlparse

BOT_TOKEN = (os.getenv("BOT_TOKEN", "").strip()
             or os.getenv("TELEGRAM_PING_BOT_TOKEN", "").strip())
# Every command that DOES something is gated on this: the chat must be bound
# to a CodeNest account. Before it existed, an unknown chat could deploy code
# — reproduced, a stranger's os.system('whoami') ran on the server.
from services import telegram_link  # noqa: E402
from services import bot_ops  # noqa: E402
from services import runner_client  # noqa: E402
from services import bot_analytics
from services import telegram_admin_ext  # noqa: E402
from services import github_repo  # noqa: E402
from services import pingbot_broadcast  # noqa: E402

import logging
logger = logging.getLogger("codenest-app")

# Hardcoded, not an env var (by request) — this Telegram account always has
# full admin rights over the bot regardless of the users.is_admin DB flag,
# so there's never a chicken-and-egg problem bootstrapping the very first
# admin. It can grant/revoke admin and zip-upload permission for anyone
# else via /admin — see cmd_admin below.
SUPER_ADMIN_TG_ID = 8768764605


# Soft per-chat command debounce: NOT a punishment, not a lock on any
# action — just "please wait a sec" for someone mashing a button fast
# (double-tapping /restart, spamming /logs). Admins (/queen) are exempt,
# since they legitimately act across many bots back to back.
_CMD_RATE_WINDOW_S = float(os.getenv("CMD_RATE_LIMIT_WINDOW_S", "4"))
_CMD_RATE_N = int(os.getenv("CMD_RATE_LIMIT_N", "3"))
_cmd_times: dict = {}


def _rate_limited(chat_id) -> bool:
    now = time.time()
    hist = [t for t in _cmd_times.get(chat_id, []) if now - t < _CMD_RATE_WINDOW_S]
    hist.append(now)
    _cmd_times[chat_id] = hist
    return len(hist) > _CMD_RATE_N


def _is_admin(user, telegram_user_id=None) -> bool:
    if telegram_user_id == SUPER_ADMIN_TG_ID:
        return True
    return bool(user and user.get("is_admin"))


def _admin_notify_targets():
    """Everyone who should hear about new signups/abuse reports: the
    hardcoded super-admin plus anyone with is_admin=1 who's linked."""
    ids = set()
    if SUPER_ADMIN_TG_ID:
        ids.add(SUPER_ADMIN_TG_ID)
    for r in telegram_link.list_admin_overview(limit=200):
        if r.get("is_admin") and r.get("telegram_id"):
            ids.add(r["telegram_id"])
    return ids


def _admin_notify_loop():
    while True:
        try:
            time.sleep(60)
            found = telegram_admin_ext.check_new_signups_and_reports()
            if not found["users"] and not found["reports"]:
                continue
            targets = _admin_notify_targets()
            for u in found["users"]:
                text = f"🆕 New signup: *{u.get('username') or '(no username)'}* (#{u['id']})"
                for tid in targets:
                    try:
                        _send(tid, text)
                    except Exception:
                        pass
            for r in found["reports"]:
                text = f"🚩 New abuse report #{r['id']}: {r.get('reason') or 'no reason'}\n{r.get('url','')[:80]}"
                for tid in targets:
                    try:
                        _send(tid, text)
                    except Exception:
                        pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("admin notify loop error: %s", exc)


def _admin_menu_kb():
    """Neat, grouped admin menu — core health first, bulk actions last."""
    return {"inline_keyboard": [
        # Top row: the two things an admin checks first at night when bots seem slow
        [{"text": "🩺 Health", "callback_data": "admin:health"},
         {"text": "📊 Overview", "callback_data": "admin:overview"}],
        [{"text": "👥 Users", "callback_data": "admin:users:0"},
         {"text": "👑 Queens", "callback_data": "admin:queens"}],
        [{"text": "🖥 Runners", "callback_data": "admin:runners"},
         {"text": "📦 Jobs", "callback_data": "admin:jobs:0"}],
        [{"text": "📢 Broadcast", "callback_data": "admin:broadcast"},
         {"text": "♻️ Recover now", "callback_data": "admin:recovernow"}],
        [{"text": "🚩 Reports", "callback_data": "admin:abuse"},
         {"text": "📝 Audit", "callback_data": "admin:audit"}],
        [{"text": "🔎 Search", "callback_data": "admin:searchflow"},
         {"text": "🆕 Signups", "callback_data": "admin:signups"}],
        [{"text": "⛔ Bans", "callback_data": "admin:bans"},
         {"text": "🔗 Clusters", "callback_data": "admin:clusters"}],
        [{"text": "🔍 Security", "callback_data": "admin:security"},
         {"text": "📜 Terms", "callback_data": "admin:terms"}],
        [{"text": "📤 Export", "callback_data": "admin:exportmenu"},
         {"text": "🏪 Store", "callback_data": "admin:store"}],
        [{"text": "🧑‍⚖️ By admin", "callback_data": "admin:auditadmins"},
         {"text": _maintenance_label(), "callback_data": "admin:togmaint"}],
    ]}


def _maintenance_label():
    return ("🔴 Maintenance: ON (tap to turn off)" if telegram_admin_ext.get_maintenance_mode()
            else "🟢 Maintenance: OFF (tap to turn on)")


def _admin_user_row_kb(target: dict, jobs: list = None):
    """User card buttons: flags + every job as a tappable row."""
    uid = target["id"]
    admin_lbl = "➖ Revoke admin" if target.get("is_admin") else "➕ Grant admin"
    zip_lbl = "🚫 Deny zip" if target.get("can_upload_zip") else "📦 Allow zip"
    susp_lbl = "✅ Unsuspend" if target.get("is_suspended") else "⛔ Suspend"
    queen_lbl = "🚫 Remove 👑" if target.get("mem_unlimited") else "👑 Make queen"
    rows = [
        [{"text": admin_lbl, "callback_data": f"admin:togadmin:{uid}"},
         {"text": zip_lbl, "callback_data": f"admin:togzip:{uid}"}],
        [{"text": susp_lbl, "callback_data": f"admin:togsuspend:{uid}"},
         {"text": queen_lbl, "callback_data": f"admin:togqueen:{uid}"}],
        [{"text": "🚦 Set job limit", "callback_data": f"admin:limituser:{uid}"},
         {"text": "🔄 Refresh", "callback_data": f"admin:user:{uid}"}],
    ]
    jobs = jobs if jobs is not None else telegram_admin_ext.jobs_for_user(uid)
    icon = {"running": "🟢", "crashed": "🔴", "stopped": "⏹",
            "installing": "🟡", "offline": "⚪"}
    for j in (jobs or [])[:30]:
        st = j.get("live_status") or "?"
        mark = icon.get(st, "⚪")
        label = f"{mark} {j['name']} · {st}"[:60]
        rows.append([{"text": label, "callback_data": f"admin:job:{j['id']}"}])
    if jobs and len(jobs) > 30:
        rows.append([{"text": f"… +{len(jobs) - 30} more",
                      "callback_data": f"admin:user:{uid}"}])
    rows.append([{"text": "⬅️ Users", "callback_data": "admin:users:0"},
                 {"text": "🛠 Menu", "callback_data": "admin:menu"}])
    return {"inline_keyboard": rows}


def _admin_user_detail_text(target: dict, jobs: list = None) -> str:
    """Everything about one account; jobs also become buttons on the keyboard."""
    flags = []
    if target.get("is_admin"): flags.append("admin")
    if target.get("can_upload_zip"): flags.append("zip-allowed")
    if target.get("is_suspended"): flags.append("suspended")
    if target.get("mem_unlimited"): flags.append("👑 unlimited memory")
    if target.get("job_limit_override") is not None:
        flags.append(f"limit={target['job_limit_override']}")
    tag = ", ".join(flags) or "no special flags"
    tid = target.get("telegram_id") or "not linked"
    seen = telegram_admin_ext.last_seen_for_user(target["id"])
    seen_line = (f"\nLast seen: {seen['ls']} from `{seen.get('ip_address') or '—'}`"
                 if seen else "\nLast seen: never")
    jobs = jobs if jobs is not None else telegram_admin_ext.jobs_for_user(target["id"])
    try:
        priv = bot_ops.account_privileges(target["id"])
        running = bot_ops.active_count(target["id"])
        slots = f"{running}/{priv.get('job_limit')}"
        mem = priv.get("mem_limit_mb")
        mem_s = "no ceiling" if not mem else f"{mem}MB/app"
    except Exception:
        slots, mem_s = "—", "—"
    lines = [
        f"👤 *{target.get('username') or '(no username)'}* (#{target['id']})",
        f"Telegram: `{tid}`",
        f"Email: `{target.get('email') or '—'}`",
        f"Flags: {tag}{seen_line}",
        f"Slots: {slots} · Memory: {mem_s}",
        f"Created: {target.get('created_at') or '—'}",
        "",
    ]
    if not jobs:
        lines.append("📦 No jobs yet.")
    else:
        lines.append(f"📦 *{len(jobs)} job(s)* — tap a button below for full control:")
        for j in jobs[:12]:
            st = j.get("live_status") or "?"
            extra = []
            if j.get("runner_url"):
                extra.append("runner set")
            if j.get("repo_url"):
                extra.append("from repo")
            if j.get("telegram_bot_username"):
                extra.append(f"@{j['telegram_bot_username']}")
            tail = (" · " + ", ".join(extra)) if extra else ""
            lines.append(f"· #{j['id']} *{j['name']}* · {j.get('language') or '?'} · {st}{tail}")
        if len(jobs) > 12:
            lines.append(f"· … and {len(jobs) - 12} more")
    return "\n".join(lines)


def _admin_job_detail_text(j: dict) -> str:
    """One job, every fact an admin needs before they press a button."""
    st = j.get("live_status") or "unknown"
    icon = {"running": "🟢", "crashed": "🔴", "stopped": "⏹",
            "installing": "🟡", "offline": "⚪"}.get(st, "⚪")
    lines = [
        f"{icon} *{j['name']}* (#{j['id']})",
        f"Owner: {j.get('owner') or '—'} (#{j.get('owner_id') or '?'})"
        + (" ⛔ suspended" if j.get("owner_suspended") else ""),
        f"Language: `{j.get('language') or '—'}` · Status: `{st}`",
        f"Desired: `{j.get('desired_state') or '—'}`",
    ]
    if j.get("telegram_bot_username"):
        lines.append(f"Bot: @{j['telegram_bot_username']}"
                     + (f" · check `{j.get('telegram_check_status')}`"
                        if j.get("telegram_check_status") else ""))
    mem_now = j.get("mem_mb") or 0
    mem_peak = j.get("peak_mem_mb") or 0
    mem_cap = j.get("mem_limit_mb")
    cap_s = ("unlimited" if mem_cap == 0 else
             (f"{mem_cap}MB" if mem_cap else "runner default"))
    lines.append(f"Memory: {round(float(mem_now))}MB now · {round(float(mem_peak))}MB peak · cap {cap_s}")
    lines.append(f"Uptime: {_fmt_uptime(j.get('uptime_s'))} · Restarts: {j.get('restarts') or 0}")
    if j.get("last_exit_reason"):
        lines.append(f"Last exit: `{j['last_exit_reason']}`"
                     + (f" (code {j['last_exit_code']})"
                        if j.get("last_exit_code") not in (None, "") else ""))
    rid = j.get("runner_job_id") or "—"
    rurl = j.get("runner_url") or "(embedded / default pool)"
    lines += ["", f"Runner job id: `{rid}`", f"Runner URL: `{rurl}`"]
    if j.get("web") or j.get("web_slug") or j.get("port"):
        lines.append(f"Web: slug `{j.get('web_slug') or '—'}` · port `{j.get('port') or '—'}`")
    if j.get("repo_url"):
        lines.append(f"Repo: `{j['repo_url']}`")
        if j.get("repo_entry"):
            lines.append(f"Entry: `{j['repo_entry']}`")
        commit = j.get("live_repo_commit") or j.get("repo_commit") or "—"
        lines.append(f"Commit: `{str(commit)[:12]}`"
                     + (" · auto-deploy on" if j.get("auto_deploy") else ""))
    if j.get("requirements"):
        lines.append(f"Requirements: `{j['requirements'][:180]}`")
    if j.get("libs"):
        libs = j["libs"]
        lines.append(f"Installed libs: `{', '.join(libs[:12])}`"
                     + ("…" if len(libs) > 12 else ""))
    if j.get("env_keys"):
        lines.append(f"Env keys: `{', '.join(j['env_keys'])}`  _(values hidden)_")
    else:
        lines.append("Env keys: _(none)_")
    if j.get("has_code"):
        kb = j.get("code_bytes") or 0
        lines.append(f"Source: stored inline ({kb} bytes) — tap 📄 to download")
    else:
        lines.append("Source: no inline body (repo/zip deploy) — files live on the runner")
    lines.append(f"Created: {j.get('created_at') or '—'} · Updated: {j.get('updated_at') or '—'}")
    return "\n".join(lines)


def _admin_job_kb(j: dict) -> dict:
    jid = j["id"]
    uid = j.get("owner_id")
    rows = [
        [{"text": "🔄 Restart", "callback_data": f"admin:jobrestart:{jid}"},
         {"text": "⏹ Stop", "callback_data": f"admin:jobstop:{jid}"}],
        [{"text": "📜 Logs", "callback_data": f"admin:joblogs:{jid}"},
         {"text": "📄 Source", "callback_data": f"admin:jobsource:{jid}"}],
        [{"text": "✏️ Edit code", "callback_data": f"admin:jobedit:{jid}"},
         {"text": "🔑 Set env", "callback_data": f"admin:jobenv:{jid}"}],
        [{"text": "📜 Revisions", "callback_data": f"admin:revisions:{jid}"},
         {"text": "🗑 Delete…", "callback_data": f"admin:jobdelconfirm:{jid}"}],
        [{"text": "🔄 Refresh", "callback_data": f"admin:job:{jid}"}],
    ]
    if j.get("telegram_bot_username"):
        rows.append([{"text": "🤖 Open bot", "url": f"https://t.me/{j['telegram_bot_username']}"}])
    nav = []
    if uid:
        nav.append({"text": "👤 Owner", "callback_data": f"admin:user:{uid}"})
    nav.append({"text": "⬅️ Jobs", "callback_data": "admin:jobs:0"})
    rows.append(nav)
    return {"inline_keyboard": rows}


def cmd_admin_short_toggle(chat_id, telegram_user_id, arg, sub):
    """Backs /zip <userid> and /unzip <userid> — short text fallbacks for
    exactly what the Users → Allow/Deny zip button does, for when an inline
    button misbehaves (Telegram-side hiccups happen) and retyping /admin
    to navigate back to the same user isn't worth it for one toggle."""
    caller = telegram_link.user_for_chat(telegram_user_id)
    if not _is_admin(caller, telegram_user_id):
        return  # silent, same as /admin for a non-admin
    ref = (arg or "").strip()
    if not ref:
        _send(chat_id, f"Usage: `/{'zip' if sub == 'allowzip' else 'unzip'} name`")
        return
    target = telegram_link.resolve_user_ref(ref)
    if not target:
        _send(chat_id, f"No user found for “{ref}”.")
        return
    telegram_link.set_zip_permission(target["id"], sub == "allowzip")
    verb = "can now upload" if sub == "allowzip" else "can no longer upload"
    _send(chat_id, f"✅ {target.get('username') or ref} {verb} .zip bundles.")


def _safe_reapply_mem(user_id: int) -> int:
    """Push a fresh 👑 decision to the bots that are running RIGHT NOW, and
    report how many were restarted.

    Best-effort on purpose: the flag is already written to users.mem_unlimited
    before this is called, so a runner that is asleep or mid-deploy only means
    "the next deploy applies it" — never "the grant failed". Raising here would
    turn a cosmetic follow-up into a lost admin command."""
    try:
        return bot_ops.reapply_mem_limit(user_id)
    except Exception:
        logger.exception("Could not re-apply the memory limit for user %s", user_id)
        return 0


def _queens_text() -> str:
    rows = telegram_link.list_queens()
    if not rows:
        return ("👑 *Queens* — nobody has unlimited memory yet.\n"
                "Grant it with `/queen username` (or a telegram id).")
    lines = [f"👑 *Queens* ({len(rows)}) — no per-bot memory ceiling:"]
    for r in rows:
        extra = f" · jobs:`{r['job_limit_override']}`" if r.get("job_limit_override") else ""
        lines.append(f"`{r['id']}` · {r.get('username') or '(no username)'} · "
                     f"tg:`{r.get('telegram_id') or '—'}`{extra}")
    lines.append("\nRevoke with `/queen off name`.")
    return "\n".join(lines)


def cmd_queen(chat_id, telegram_user_id, arg):
    """👑 /queen — the unlimited-memory flag, back by request.

      /queen                 list everyone who has it
      /queen <user>          grant  (users.mem_unlimited=1 → the runner is told
                             mem_limit_mb=0, which skips the per-job RLIMIT_AS)
      /queen off <user>      revoke (back to the runner's default cap)
      /unqueen <user>        the same revoke, one word shorter

    <user> is a CodeNest username, email or linked Telegram id — resolved by
    telegram_link.resolve_user_ref, the same lookup /admin limit and /see use,
    because an admin usually knows one of those and never the internal id.

    Admin-only and SILENT for everyone else, the same posture as /admin and
    /see: a non-admin gets nothing back, not even proof the command exists.

    The flag alone used to be the whole feature, and that was the complaint:
    the runner stores a job's RLIMIT when the job is created, so a bot that was
    already being OOM-killed kept its old ceiling until someone redeployed it.
    reapply_mem_limit() below closes that gap for bots that are running now.
    """
    caller = telegram_link.user_for_chat(telegram_user_id)
    if not _is_admin(caller, telegram_user_id):
        # Silent for everyone else — same posture as /admin and /see. The one
        # exception is an account that ALREADY holds 👑: showing them their own
        # panel reveals nothing they don't have, and "I typed /queen and nothing
        # happened" is indistinguishable from a broken bot.
        if _user_is_queen(caller):
            _send(chat_id, _queen_panel_text(caller), reply_markup=_queen_panel_kb())
        return

    parts = (arg or "").split(None, 1)
    if not parts:
        _send(chat_id, _queens_text())
        return

    off = parts[0].lower() in ("off", "remove", "revoke", "no", "unqueen")
    # "/queen off" on its own is a real thing an admin types mid-thought —
    # indexing parts[1] unconditionally raised IndexError, which the dispatcher
    # logged as an error and answered with nothing at all.
    if off:
        ref = parts[1].strip() if len(parts) > 1 else ""
    else:
        ref = parts[0].strip()
    if not ref:
        _send(chat_id, "Usage:\n"
                       "`/queen username` — grant 👑\n"
                       "`/queen off name` — revoke it\n"
                       "`/queen` — list everyone who has it")
        return

    target = telegram_link.resolve_user_ref(ref)
    if not target:
        _send(chat_id, f"No user found for “{ref}”.")
        return

    name = target.get("username") or ref
    grant = not off
    if bool(target.get("mem_unlimited")) == grant:
        _send(chat_id, f"{name} is {'already 👑 — no memory ceiling' if grant else 'already on the default memory cap'}. "
                       "Nothing changed.")
        return

    telegram_link.set_unlimited_permission(target["id"], grant)
    reapplied = _safe_reapply_mem(target["id"])
    text = (f"👑 {name} now has unlimited memory — no per-bot RAM ceiling."
            if grant else
            f"✅ {name} is back on the runner's default memory cap.")
    if reapplied:
        text += f"\n🔁 Restarted {reapplied} running bot(s) so it applies now — data kept."
    else:
        text += "\nAny running bot picks it up on its next deploy or restart."
    _send(chat_id, text)


def cmd_user(chat_id, telegram_user_id, arg=""):
    """`/user` — every account as buttons; `/user name` opens one card.

    Same data the 👥 Users panel shows, reachable by typing so an admin who
    already knows the username does not have to page through the list.
    """
    caller = telegram_link.user_for_chat(telegram_user_id)
    if not _is_admin(caller, telegram_user_id):
        return
    ref = (arg or "").strip()
    if not ref:
        _send(chat_id, _admin_users_text(0), reply_markup=_admin_users_kb(0))
        return
    target = telegram_link.resolve_user_ref(ref)
    if not target:
        _send(chat_id, f"No user found for “{ref}”. Try `/user` for the full list, "
                       f"or `/admin search {ref}`.")
        return
    jobs = telegram_admin_ext.jobs_for_user(target["id"])
    _send(chat_id, _admin_user_detail_text(target, jobs),
          reply_markup=_admin_user_row_kb(target, jobs))


def cmd_see(chat_id, telegram_user_id, arg):
    """/see <username|telegram_id> — that user's account + job list.
    /see <username|telegram_id> <job id|name> — full detail on one job,
    INCLUDING the actual code, sent as a real file. This deliberately
    bypasses the no-code-shown rule the rest of the admin panel follows
    (job_detail/jobs_recent never return code) — /see exists specifically
    for investigating a reported account, so showing the code IS the
    point. Admin-only; silent for anyone else, same posture as /admin."""
    caller = telegram_link.user_for_chat(telegram_user_id)
    if not _is_admin(caller, telegram_user_id):
        return
    parts = (arg or "").split(None, 1)
    if not parts:
        _send(chat_id, "Usage:\n`/see name` — account + jobs\n"
                       "`/see name job` — full job + code")
        return
    target = telegram_link.resolve_user_ref(parts[0])
    if not target:
        _send(chat_id, f"No user found for “{parts[0]}”.")
        return

    if len(parts) == 1:
        jobs = telegram_admin_ext.jobs_for_user(target["id"])
        flags = []
        if target.get("is_admin"): flags.append("admin")
        if target.get("can_upload_zip"): flags.append("zip")
        if target.get("is_suspended"): flags.append("suspended")
        tag = f" [{', '.join(flags)}]" if flags else ""
        lines = [f"👤 *{target.get('username') or '(no username)'}* (#{target['id']}){tag}",
                 f"Telegram: `{target.get('telegram_id') or 'not linked'}`", ""]
        if not jobs:
            lines.append("No jobs.")
        else:
            lines.append(f"*{len(jobs)} job(s):*")
            for j in jobs:
                lines.append(f"· #{j['id']} {j['name']} · {j['language']} · {j['live_status']}")
            lines.append(f"\nFor full detail + code: `/see {parts[0]} job`")
        _send(chat_id, "\n".join(lines))
        return

    job_ref = parts[1].strip()
    j = telegram_admin_ext.job_full_detail_with_code(target["id"], job_ref)
    if not j:
        _send(chat_id, f"No job “{job_ref}” for {target.get('username') or parts[0]}.")
        return

    bot_line = f"\nBot: @{j['telegram_bot_username']}" if j.get("telegram_bot_username") else ""
    text = (f"*{j['name']}* (#{j['id']}) — owned by {target.get('username') or parts[0]}\n"
            f"Language: {j['language']} · Status: {j.get('live_status') or 'unknown'}\n"
            f"Created: {j.get('created_at')}\n"
            f"Uptime: {j.get('uptime_s') or 0}s · Mem: {j.get('mem_mb') or 0}MB · "
            f"Restarts: {j.get('restarts') or 0}{bot_line}")
    _send(chat_id, text)

    code = j.get("code") or ""
    if not code.strip():
        _send(chat_id, "(No inline code stored for this job — it may be a repo/zip import; "
                       "check the runner's own copy on disk if you need the actual files.)")
        return
    ext = {"python": "py", "node": "js", "bash": "sh", "ruby": "rb", "php": "php"}.get(j["language"], "txt")
    fname = f"{j['name']}.{ext}"
    with tempfile.NamedTemporaryFile(mode="w", suffix=f"_{fname}", delete=False, encoding="utf-8") as f:
        f.write(code)
        tmp_path = f.name
    try:
        _send_document(chat_id, tmp_path, caption=f"{j['name']} — source as stored")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


_ADMIN_RECOVERY_COMMAND_LOCK = threading.Lock()


def _admin_recovery_worker(chat_id):
    try:
        from services import job_recovery
        unresolved = job_recovery.recover_once()
        _send(chat_id,
              f"✅ Recovery pass done. Unresolved (still missing token/source): *{unresolved}*.",
              reply_markup={"inline_keyboard": [
                  [{"text": "🩺 Health", "callback_data": "admin:health"},
                   {"text": "🖥 Runners", "callback_data": "admin:runners"}],
                  [{"text": "⬅️ Menu", "callback_data": "admin:menu"}],
              ]})
    except Exception as exc:  # noqa: BLE001
        logger.exception("manual admin recovery failed")
        try:
            _send(chat_id, f"❌ Recovery failed: {type(exc).__name__}: {exc}")
        except Exception:
            logger.exception("could not report manual recovery failure")
    finally:
        _ADMIN_RECOVERY_COMMAND_LOCK.release()


def _start_admin_recovery(chat_id, message_id=None):
    """Queue exactly one admin-requested sweep without blocking update handling."""
    if not _ADMIN_RECOVERY_COMMAND_LOCK.acquire(blocking=False):
        _edit_or_send(chat_id, message_id,
                      "♻️ A recovery pass is already running. I won't start another copy.")
        return False
    try:
        _edit_or_send(chat_id, message_id,
                      "♻️ Recovery started once. I'll send one result when it finishes.")
        thread = threading.Thread(target=_admin_recovery_worker, args=(chat_id,),
                                  daemon=True, name="admin-recovery")
        thread.start()
        return True
    except Exception:
        _ADMIN_RECOVERY_COMMAND_LOCK.release()
        raise


def cmd_admin(chat_id, telegram_user_id, arg):
    """/admin — inline-button panel for most things; a few actions also have
    typed shortcuts (all support a bare, no-args form that starts a
    step-by-step Q&A instead):
      /admin ban <telegram_id> [reason]
      /admin unban <telegram_id>
      /admin broadcast <message>
      /admin limit <username|telegram_id> <number|clear>
      /admin queen <username|telegram_id> — 👑 unlimited memory (see /queen)
      /admin unqueen <username|telegram_id> / /admin queens — list them
      /admin grant|revoke|allowzip|denyzip <username|telegram_id>
      /admin addrunner <label> <url> <secret>
      /admin deleterunner <id>
      /admin search <query> / /admin searchjobs <query>
      /admin signups [hours] — default 24h
      /admin export users|jobs — sends a CSV file
      /user  — every account as buttons; /user <name|id> opens one card
              (jobs, source, runner URL, restart/stop/delete/edit — all from chat)
    The hardcoded SUPER_ADMIN_TG_ID or any user with is_admin=1 can use all
    of this; every action re-checks admin status on its own, since
    callback_data is attacker-suppliable in principle."""
    caller = telegram_link.user_for_chat(telegram_user_id)
    if not _is_admin(caller, telegram_user_id):
        return  # silent — a non-admin gets nothing, not even confirmation the command exists

    parts = (arg or "").split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub == "ban":
        if not rest:
            _start_admin_flow(chat_id, "ban")
            return
        bits = rest.split(None, 1)
        if not bits or not bits[0].isdigit():
            _send(chat_id, "Usage: `/admin ban 123456789 [reason]` — or just `/admin ban` "
                           "and I'll ask for each piece.")
            return
        reason = bits[1] if len(bits) > 1 else ""
        telegram_admin_ext.ban_telegram_id(int(bits[0]), telegram_user_id, reason)
        _send(chat_id, f"⛔ Banned `{bits[0]}`" + (f" — {reason}" if reason else "") + ".")
        return

    if sub == "unban":
        if not rest.isdigit():
            _send(chat_id, "Usage: `/admin unban 123456789`")
            return
        ok = telegram_admin_ext.unban_telegram_id(int(rest))
        _send(chat_id, "✅ Unbanned." if ok else "That id wasn't banned.")
        return

    if sub == "broadcast":
        # Rich broadcast — supports: /admin broadcast status, /admin broadcast cancel <id>, /admin broadcast <message>
        low_rest = (rest or "").strip().lower()
        if low_rest in ("status", "list", "queue"):
            try:
                from services import pingbot_broadcast as _pb
                cands = _pb.list_campaigns(limit=5)
                if not cands:
                    _send(chat_id, "📢 No broadcasts yet.")
                    return
                lines = ["📢 *Recent broadcasts:*"]
                for c in cands:
                    lines.append(f"#{c['id']} · `{c['status']}` · {c.get('total_count',0)} users · {c.get('created_at','')[:10]}")
                    lines.append(f"_{ (c.get('text') or '')[:80].replace(chr(10),' ') }_")
                kb = {"inline_keyboard": [[{"text": f"📊 #{c['id']} status", "callback_data": f"admin:broadcast_status:{c['id']}" }] for c in cands[:4]] + [[{"text": "⬅️ Menu", "callback_data": "admin:menu"}]]}
                _send(chat_id, "\n".join(lines), reply_markup=kb)
            except Exception as exc:
                logger.exception("broadcast status failed")
                _send(chat_id, f"❌ Status failed: {type(exc).__name__}")
            return
        if low_rest.startswith("cancel"):
            bits = (rest or "").split(None, 1)
            cid = bits[1].strip() if len(bits) > 1 else ""
            if not cid.isdigit():
                _send(chat_id, "Usage: `/admin broadcast cancel <id>` — or `status` to list.")
                return
            try:
                from services import pingbot_broadcast as _pb
                ok = _pb.cancel_campaign(int(cid))
                _send(chat_id, f"✅ Broadcast #{cid} cancelled." if ok else f"❌ Could not cancel #{cid} (not found or already done).")
                if ok:
                    _send(chat_id, _pb.status_text(int(cid)), reply_markup=_pb.status_keyboard(int(cid)))
            except Exception as exc:
                _send(chat_id, f"❌ Cancel failed: {exc}")
            return
        if not rest:
            _start_admin_flow(chat_id, "broadcast")
            return
        # One-line broadcast with explicit preview + confirmation (new rich path).
        # Keep legacy immediate-send as fallback if broadcast module unavailable.
        try:
            from services import pingbot_broadcast as _pb
            cid = _pb.create_campaign(telegram_user_id or chat_id, rest, auto_start=False)
            st = _pb.campaign_stats(cid)
            # preview to admin with confirm/cancel buttons
            _send(chat_id, _pb.preview_text(cid), reply_markup=_pb.preview_keyboard(cid))
            _send(chat_id, _pb.status_text(cid), reply_markup=_pb.status_keyboard(cid))
            return
        except Exception as exc:
            logger.exception("rich broadcast create failed, falling back to legacy")
            # fallback legacy immediate
            ids = telegram_admin_ext.all_linked_telegram_ids()
            _send(chat_id, f"📢 Sending to {len(ids)} user(s)…")
            sent = 0
            for tid in ids:
                try:
                    _send(tid, rest)
                    sent += 1
                except Exception:
                    pass
                time.sleep(0.05)
            _send(chat_id, f"✅ Broadcast sent to {sent}/{len(ids)} user(s).")
            return

    if sub in ("health", "doctor", "diag", "diagnose"):
        # The same screen the 🩺 button shows: webhook, runners, jobs, the two
        # loops that keep bots alive, and how a deploy works now.
        _send(chat_id, _admin_health_text(), reply_markup=_admin_health_kb())
        return

    if sub in ("runners", "runner", "fleet"):
        # Command twin of the 🖥 Runners button — same screen, no tap required.
        handle_admin_callback(chat_id, telegram_user_id, "runners", "", None)
        return

    if sub in ("menu", "panel", "home"):
        _send(chat_id, "🛠 *Admin panel* — every row is also a command "
                       "(`/admin runners`, `/admin health`, `/user`, …).",
              reply_markup=_admin_menu_kb())
        return

    if sub in ("recover", "rescue", "reconcile"):
        # One recovery only; code updates are a separate, explicit action.
        # Queue the work so the Telegram poller/webhook remains responsive.
        _start_admin_recovery(chat_id)
        return

    if sub in ("overview", "stats", "dash"):
        handle_admin_callback(chat_id, telegram_user_id, "overview", "", None)
        return

    if sub in ("bans", "banlist"):
        handle_admin_callback(chat_id, telegram_user_id, "bans", "", None)
        return

    if sub in ("audit", "log"):
        handle_admin_callback(chat_id, telegram_user_id, "audit", "", None)
        return

    if sub in ("jobs", "joblist"):
        handle_admin_callback(chat_id, telegram_user_id, "jobs", "0", None)
        return

    if sub in ("users", "userlist"):
        handle_admin_callback(chat_id, telegram_user_id, "users", "0", None)
        return

    if sub == "limit":
        if not rest:
            _start_admin_flow(chat_id, "limit")
            return
        bits = rest.split(None, 1)
        if len(bits) < 2:
            _send(chat_id, "Usage: `/admin limit username 10` (or `clear`) — "
                           "or just `/admin limit` and I'll ask.")
            return
        target = telegram_link.resolve_user_ref(bits[0])
        if not target:
            _send(chat_id, f"No user found for “{bits[0]}”.")
            return
        val = None if bits[1].lower() == "clear" else bits[1]
        if val is not None and not val.isdigit():
            _send(chat_id, "The limit must be a number, or `clear` to remove the override.")
            return
        telegram_admin_ext.set_job_limit_override(target["id"], int(val) if val else None)
        _send(chat_id, f"✅ Job limit for {target.get('username') or bits[0]} "
                       + (f"set to {val}." if val else "cleared (back to default)."))
        return

    if sub in ("queen", "unqueen", "queens"):
        # One implementation, two spellings: /admin queen and the top-level
        # /queen are the same command, so there is only one place that decides
        # what the flag does or how it is reported.
        if sub == "queens":
            _send(chat_id, _queens_text())
        elif sub == "queen":
            cmd_queen(chat_id, telegram_user_id, rest)
        else:
            cmd_queen(chat_id, telegram_user_id, f"off {rest}".strip())
        return

    if sub == "addrunner":
        if not rest:
            _start_admin_flow(chat_id, "addrunner")
            return
        bits = rest.split(None, 2)
        if len(bits) < 3:
            _send(chat_id, "Usage: `/admin addrunner label https://runner.example secret` — "
                           "or just `/admin addrunner` and I'll ask for each one.\n"
                           "⚠️ Delete this message after sending — the secret sits in chat history otherwise.")
            return
        label, url, secret = bits[0], bits[1], bits[2]
        _send(chat_id, f"Checking {url}…")
        res = telegram_admin_ext.add_runner(label, url, secret, caller["id"] if caller else None)
        if not res.get("ok"):
            _send(chat_id, f"❌ {res['error']}")
            return
        _send(chat_id, f"✅ Runner “{res['label']}” registered (#{res['id']}) and enabled.")
        return

    if sub in ("grant", "revoke", "allowzip", "denyzip"):
        if not rest:
            _send(chat_id, f"Usage: `/admin {sub} username`")
            return
        target = telegram_link.resolve_user_ref(rest)
        if not target:
            _send(chat_id, f"No user found for “{rest}”.")
            return
        if sub == "grant":
            telegram_link.set_admin(target["id"], True)
            _send(chat_id, f"✅ {target.get('username') or rest} is now an admin.")
        elif sub == "revoke":
            if target.get("telegram_id") == SUPER_ADMIN_TG_ID:
                _send(chat_id, "Can't revoke the built-in super-admin.")
                return
            telegram_link.set_admin(target["id"], False)
            _send(chat_id, f"✅ {target.get('username') or rest} is no longer an admin.")
        elif sub == "allowzip":
            telegram_link.set_zip_permission(target["id"], True)
            _send(chat_id, f"✅ {target.get('username') or rest} can now upload .zip bundles.")
        else:
            telegram_link.set_zip_permission(target["id"], False)
            _send(chat_id, f"✅ {target.get('username') or rest} can no longer upload .zip bundles.")
        return

    if sub == "search":
        if not rest:
            _send(chat_id, "Usage: `/admin search part-of-name`")
            return
        rows = telegram_admin_ext.search_users(rest)
        if not rows:
            _send(chat_id, f"No users matching “{rest}”.")
            return
        lines = [f"🔎 *{len(rows)} match(es) for “{rest}”:*"]
        for r in rows:
            flags = []
            if r.get("is_admin"): flags.append("admin")
            if r.get("can_upload_zip"): flags.append("zip")
            if r.get("is_suspended"): flags.append("suspended")
            tag = f" _{', '.join(flags)}_" if flags else ""
            lines.append(f"`{r['id']}` · {r.get('username') or '(no username)'} · "
                         f"tg:`{r.get('telegram_id') or '—'}`{tag}")
        _send(chat_id, "\n".join(lines))
        return

    if sub == "searchjobs":
        if not rest:
            _send(chat_id, "Usage: `/admin searchjobs part-of-name`")
            return
        rows = telegram_admin_ext.search_jobs(rest)
        if not rows:
            _send(chat_id, f"No jobs matching “{rest}”.")
            return
        lines = [f"🔎 *{len(rows)} match(es) for “{rest}”:*"]
        for r in rows:
            lines.append(f"`{r['id']}` · {r['name']} · {r['owner']} · {r['language']}")
        _send(chat_id, "\n".join(lines))
        return

    if sub == "signups":
        hours = int(rest) if rest.isdigit() else 24
        rows = telegram_admin_ext.recent_signups(hours=hours)
        if not rows:
            _send(chat_id, f"No signups in the last {hours}h.")
            return
        lines = [f"🆕 *Signups, last {hours}h ({len(rows)}):*"]
        for r in rows:
            lines.append(f"`{r['id']}` · {r.get('username') or '(no username)'} · "
                         f"tg:`{r.get('telegram_id') or '—'}` · {r['created_at']}")
        _send(chat_id, "\n".join(lines))
        return

    if sub == "export":
        which = rest.strip().lower()
        if which not in ("users", "jobs"):
            _send(chat_id, "Usage: `/admin export users` or `/admin export jobs`")
            return
        csv_text = (telegram_admin_ext.export_users_csv() if which == "users"
                    else telegram_admin_ext.export_jobs_csv())
        with tempfile.NamedTemporaryFile(mode="w", suffix=f"_{which}.csv",
                                          delete=False, encoding="utf-8") as f:
            f.write(csv_text)
            tmp_path = f.name
        try:
            _send_document(chat_id, tmp_path, caption=f"{which} export")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return

    if sub == "deleterunner":
        if not rest.isdigit():
            _send(chat_id, "Usage: `/admin deleterunner 3` — get the id from the Runners view.")
            return
        res = telegram_admin_ext.delete_runner(int(rest))
        _send(chat_id, f"🗑 Deleted runner “{res['label']}”." if res.get("ok") else f"❌ {res['error']}")
        return

    if sub and sub not in ("help", "menu"):
        # An unrecognized subcommand used to fall straight through to the
        # main menu with zero feedback — indistinguishable from success.
        # Anyone who mistyped, or followed a stale instruction, had no way
        # to know their command did nothing.
        _send(chat_id, f"Unknown admin subcommand “{sub}”. Showing the menu instead:")

    _send(chat_id, "🛠 *Admin panel*", reply_markup=_admin_menu_kb())


def cmd_details(chat_id, telegram_user_id, arg):
    """/details — admin-only shortcut straight into the job fleet list: one
    line + one hidden-link 🔗 button per job (id, name, owner's user id,
    🔒 if restart-locked). Tapping the button opens that job's full card —
    status, bot, memory, repo/commit, requirements, env key names (never
    values), source availability, logs/restart/stop/edit/delete — same as
    /admin → 📦 Jobs, just reachable in one command."""
    caller = telegram_link.user_for_chat(telegram_user_id)
    if not _is_admin(caller, telegram_user_id):
        return  # silent — same as every other admin-only command
    page = int(arg) if (arg or "").strip().isdigit() else 0
    handle_admin_callback(chat_id, telegram_user_id, "jobs", str(page), None)


def _admin_users_text(page: int) -> str:
    rows = telegram_link.list_admin_overview(limit=200)
    per_page = 8
    start = page * per_page
    page_rows = rows[start:start + per_page]
    lines = ["👥 *Users* — tap a button below to manage one, or copy an id here:"]
    for r in page_rows:
        flags = []
        if r.get("is_admin"): flags.append("admin")
        if r.get("can_upload_zip"): flags.append("zip")
        if r.get("is_suspended"): flags.append("suspended")
        if r.get("mem_unlimited"): flags.append("👑")
        tag = f" _{', '.join(flags)}_" if flags else ""
        tid = r.get("telegram_id")
        lines.append(f"`{r['id']}` · {r.get('username') or '(no username)'} · "
                     f"tg:`{tid if tid else '—'}`{tag}")
    return "\n".join(lines)


def _admin_users_kb(page: int):
    rows = telegram_link.list_admin_overview(limit=200)
    per_page = 8
    start = page * per_page
    page_rows = rows[start:start + per_page]
    kb = []
    for r in page_rows:
        flags = []
        if r.get("is_admin"): flags.append("A")
        if r.get("can_upload_zip"): flags.append("Z")
        if r.get("is_suspended"): flags.append("S")
        if r.get("mem_unlimited"): flags.append("👑")
        label = r.get("username") or f"#{r['id']}"
        if flags:
            label += " [" + "".join(flags) + "]"
        kb.append([{"text": label, "callback_data": f"admin:user:{r['id']}"}])
    nav = []
    if start > 0:
        nav.append({"text": "◀️ Prev", "callback_data": f"admin:users:{page-1}"})
    if start + per_page < len(rows):
        nav.append({"text": "Next ▶️", "callback_data": f"admin:users:{page+1}"})
    if nav:
        kb.append(nav)
    kb.append([{"text": "⛔ Bulk suspend", "callback_data": "admin:bulksuspendflow"}])
    kb.append([{"text": "⬅️ Menu", "callback_data": "admin:menu"}])
    return {"inline_keyboard": kb}


def _admin_health_text() -> str:
    """🩺 One screen that answers "is the machinery actually working?".

    Every line is read from the thing itself — the webhook Telegram reports, the
    runners' own `/health`, the jobs table — instead of from what the code
    INTENDS to do. That distinction is the point: "the inline buttons don't
    work", "my bot stopped when the runner restarted" and "my push didn't
    deploy" all look identical from the outside and have three different causes,
    and each is answered here with evidence rather than a guess.
    """
    lines = ["🩺 *Runner & bot health*"]

    # ---- Telegram delivery: why a button press arrives at all -------------
    info = (_tg("getWebhookInfo") or {}).get("result") or {}
    hook = str(info.get("url") or "")
    allowed = info.get("allowed_updates") or []
    err = str(info.get("last_error_message") or "")
    if not hook:
        lines.append("\n*Telegram*\n⚠️ No webhook registered — this service is "
                     "long-polling. On a host that sleeps (Render free tier) the "
                     "poller dies with it and button presses queue up on "
                     "Telegram's side until something else wakes the box. Set "
                     "`SITE_BASE` to the public URL and restart, or tap "
                     "🔁 below.")
    else:
        lines.append(f"\n*Telegram*\n✅ Webhook `{hook}`")
        lines.append(f"   Pending updates: {info.get('pending_update_count', 0)}"
                     f" · max connections: {info.get('max_connections', '-')}")
        # THE check for "inline buttons do nothing": a webhook registered before
        # callback_query was in allowed_updates keeps receiving typed commands
        # and silently drops every single button press.
        if allowed and "callback_query" not in allowed:
            lines.append("❌ *`callback_query` is missing from allowed_updates* — "
                         "Telegram is dropping every button press. Tap "
                         "🔁 *Re-register webhook* to fix it now.")
        elif allowed:
            lines.append(f"   allowed_updates: {', '.join(str(a) for a in allowed)}")
        else:
            lines.append("   allowed_updates: all types (none restricted)")
        if err:
            when = info.get("last_error_date")
            stamp = ""
            try:
                stamp = time.strftime(" at %Y-%m-%d %H:%M UTC", time.gmtime(int(when)))
            except Exception:                                        # noqa: BLE001
                pass
            lines.append(f"⚠️ Last delivery error: {err}{stamp}")
        else:
            lines.append("   Last delivery error: none")

    # ---- runners ----------------------------------------------------------
    pool = runner_client.runner_pool()
    if not pool:
        lines.append("\n*Runners*\nEmbedded mode — the runner lives inside this "
                     "service (no `RUNNER_SERVICE_URL`).")
    else:
        health = runner_client.worker_health(refresh=False, max_age_s=20)
        lines.append(f"\n*Runners* ({len(pool)})")
        for u in pool:
            h = health.get(u) or {}
            if h.get("online"):
                lines.append(f"✅ `{u}`\n   {h.get('jobs', 0)} job(s) · "
                             f"{h.get('free', 0)} free slot(s) · "
                             f"{int(h.get('free_mb') or 0)}MB free of "
                             f"{int(h.get('total_mb') or 0)}MB"
                             + (" · **FULL**" if h.get("full") else ""))
            else:
                lines.append(f"❌ `{u}` — no answer from `/health` "
                             f"(asleep, restarting, or the wrong URL)")

    # ---- jobs + the two loops that keep them alive ------------------------
    s = telegram_link.admin_overview_stats()
    try:
        from services import job_recovery
        ad = job_recovery.auto_deploy_status()
        rec = job_recovery.RECOVERY_INTERVAL_S
    except Exception as exc:                                         # noqa: BLE001
        ad, rec = {"enabled": False, "last": {}}, 0
        lines.append(f"\n⚠️ recovery module unreadable: {exc}")
    lines.append(f"\n*Jobs*\n{s.get('jobs_total', 0)} total · "
                 f"{s.get('jobs_deployed', 0)} deployed")
    if rec >= 60:
        lines.append(f"🔁 Recovery runs every {rec}s — any job the fleet lost is "
                     f"re-created from the stored code and env, then its "
                     f"snapshot (database.db, session.json, data/) is restored, "
                     f"so a runner restart no longer stops anything.")
    else:
        lines.append(f"⚠️ Recovery is OFF (`JOB_RECOVERY_INTERVAL_S={rec}`) — a "
                     f"runner redeploy will leave bots down until you restart "
                     f"them by hand.")
    if ad.get("enabled"):
        last = ad.get("last") or {}
        ago = ad.get("last_run_s_ago")
        lines.append(f"⚙️ Auto-deploy sweep every {ad.get('interval_s')}s"
                     + (f" · last {ago}s ago" if ago is not None else " · not run yet")
                     + f" · checked {last.get('checked', 0)}, updated "
                       f"{last.get('updated', 0)}, failed {last.get('failed', 0)}")
        for e in (last.get("errors") or [])[:3]:
            lines.append(f"   • {e}")
    else:
        lines.append("⚙️ Auto-deploy sweep is OFF "
                     "(`AUTO_DEPLOY_INTERVAL_S=0`).")

    # ---- GitHub reachability, which is what /import and the picker need ----
    tok = bool(os.getenv("GITHUB_TOKEN", "").strip())
    q_owner, q_repo = _queen_repo_parts()
    q_branch = QUEEN_PROJECTS_BRANCH or ""
    lines.append("\n*GitHub*")
    lines.append(("✅ `GITHUB_TOKEN` set — 5000 req/hour"
                  if tok else
                  "⚠️ No `GITHUB_TOKEN` — anonymous calls are 60/hour per IP; "
                  "a shared host can hit that and `/import` will look empty."))
    if q_owner:
        try:
            head = github_repo.head_commit(q_owner, q_repo, q_branch or "")
            gh = f"Starter pack `{q_owner}/{q_repo}`" + (f" @ `{q_branch}`" if q_branch else "") \
                 + f" → `{(head or '')[:7] or 'no answer'}`"
        except Exception as exc:                                     # noqa: BLE001
            gh = f"Starter pack `{q_owner}/{q_repo}` — {type(exc).__name__}: {exc}"
        lines.append(gh)
    else:
        lines.append("No starter pack configured (optional `QUEEN_PROJECTS_REPO`).")

    # ---- what the runner now does, in the admin's own words ---------------
    lines.append("\n*How a deploy works now*\n"
                 "1. The repo is scanned, and each runnable thing in it becomes a "
                 "button — no filename to guess.\n"
                 "2. The runner clones the exact branch (`/tree/branch` in the "
                 "URL, or `#<branch>`) and records the commit it built.\n"
                 "3. It installs the root manifest AND each chosen sub-project's "
                 "own requirements (up to 4 paths).\n"
                 "4. `/latest` or ⬆️ redeploys IN PLACE: same id, same folder — so "
                 "the bot's database and sessions survive — same address, same "
                 "env.\n"
                 "5. On boot the runner respawns every job it still wants running "
                 "before this site even asks, and the recovery sweep above "
                 "re-creates anything that is gone entirely.\n"
                 "6. 👑 `/autodeploy x on` makes step 4 happen by itself.")
    return "\n".join(lines)


def _admin_health_kb():
    return {"inline_keyboard": [
        [{"text": "🔁 Re-register webhook", "callback_data": "admin:fixwebhook"},
         {"text": "🩺 Refresh", "callback_data": "admin:health"}],
        [{"text": "♻️ Recover bots now", "callback_data": "admin:recovernow"},
         {"text": "⚙️ Auto-deploy now", "callback_data": "admin:autodepnow"}],
        [{"text": "🚦 Set a job limit", "callback_data": "admin:limitflow"}],
        [{"text": "🖥 Runners", "callback_data": "admin:runners"},
         {"text": "⬅️ Menu", "callback_data": "admin:menu"}],
    ]}


def handle_admin_callback(chat_id, telegram_user_id, action, ref, message_id=None):
    """Every admin: callback lands here. Re-checks admin status on every
    single press — a button label is not a permission, whoever crafted the
    tap is.

    NEVER raises to the webhook/poller: a raised error becomes Telegram's
    alert popup "Something went wrong" which looks like the whole admin
    panel is broken. Failures stay as a normal chat reply instead.
    """
    try:
        _handle_admin_callback_inner(chat_id, telegram_user_id, action, ref, message_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("admin callback %s/%s failed", action, ref)
        try:
            _edit_or_send(
                chat_id, message_id,
                f"⚠️ Could not complete that admin action "
                f"({type(exc).__name__}). Try `/admin` again, or 🩺 Health.",
                reply_markup={"inline_keyboard": [
                    [{"text": "🛠 Menu", "callback_data": "admin:menu"},
                     {"text": "🩺 Health", "callback_data": "admin:health"}]]})
        except Exception:
            try:
                _send(chat_id, "⚠️ Admin action failed — send `/admin` again.")
            except Exception:
                pass


def _handle_admin_callback_inner(chat_id, telegram_user_id, action, ref, message_id=None):
    caller = telegram_link.user_for_chat(telegram_user_id)
    if not _is_admin(caller, telegram_user_id):
        # Silent for non-admins — do not confirm the panel exists.
        return

    if action == "menu":
        _edit_or_send(chat_id, message_id, "🛠 *Admin panel*", reply_markup=_admin_menu_kb())
        return

    if action == "health":
        # Read live: webhook, runners, jobs, the two recovery loops, GitHub.
        _edit_or_send(chat_id, message_id, _admin_health_text(),
                      reply_markup=_admin_health_kb())
        return

    if action == "fixwebhook":
        # The repair for "inline buttons don't work" when the cause is on
        # Telegram's side: a webhook registered without callback_query drops
        # every button press while typed commands keep working.
        #
        # It only re-registers when a webhook ALREADY exists, because that is
        # what proves this service is in webhook mode and no poller thread is
        # running. Registering one over a live poller would start a fight the
        # poller loses silently: getUpdates comes back 409, poll_loop deletes
        # the webhook once to self-heal, and after that it just logs 409 forever
        # while the bot answers nothing. A mode change needs a restart, and the
        # message says so instead of pretending a button can do it.
        before = (_tg("getWebhookInfo") or {}).get("result") or {}
        if not str(before.get("url") or ""):
            _send(chat_id, "⚠️ No webhook is registered, so this service is "
                           "long-polling — and switching modes is a restart, not "
                           "a button: registering one now would fight the running "
                           "poller and the bot would go quiet.\n\n"
                           "To move to webhook mode:\n"
                           "1. set `SITE_BASE` to this service's public URL "
                           "(e.g. `https://ahadrunspace.onrender.com`)\n"
                           "2. restart the service\n"
                           "It registers `/telegram/webhook` with "
                           "`message` + `callback_query` at boot and skips "
                           "polling. 🩺 shows which mode you are in.")
            return
        ok = enable_webhook()
        info = (_tg("getWebhookInfo") or {}).get("result") or {}
        allowed = info.get("allowed_updates") or []
        _send(chat_id, ("✅ Webhook re-registered." if ok else
                        "❌ Telegram refused setWebhook — check `SITE_BASE` and "
                        "`BOT_TOKEN`, and the service log for the reason.")
                       + f"\nurl: `{info.get('url') or '(none)'}`"
                       + f"\nallowed_updates: {', '.join(str(a) for a in allowed) or '(all)'}"
                       + f"\npending: {info.get('pending_update_count', 0)}"
                       + ("\n\nButtons should answer now. If they still don't, the "
                          "cause is inside this service, not Telegram — 🩺 shows "
                          "which part." if ok else ""))
        if message_id:
            _edit_or_send(chat_id, message_id, _admin_health_text(),
                          reply_markup=_admin_health_kb())
        return

    if action == "recovernow":
        _start_admin_recovery(chat_id, message_id)
        return

    if action == "autodepnow":
        # Run the 👑 auto-deploy sweep on demand instead of waiting for its own
        # clock — the answer to "I pushed twenty minutes ago, where is it?".
        try:
            from services import job_recovery
            res = job_recovery.auto_deploy_sweep(force=True)
        except Exception as exc:                                   # noqa: BLE001
            res = {"error": f"{type(exc).__name__}: {exc}"}
        if res.get("disabled"):
            _send(chat_id, "⚙️ The auto-deploy sweep is switched off "
                           "(`AUTO_DEPLOY_INTERVAL_S=0`). Individual apps still "
                           "update with `/latest name` or ⬆️.")
        else:
            _send(chat_id, f"⚙️ Sweep done — checked *{res.get('checked', 0)}*, "
                           f"updated *{res.get('updated', 0)}*, unchanged "
                           f"{res.get('unchanged', 0)}, failed {res.get('failed', 0)}."
                           + ("".join(f"\n• {e}" for e in (res.get("errors") or [])[:4])))
        return

    if action == "limitflow":
        # /admin limit as a button: the same two-question flow, discoverable
        # from the panel instead of only from the command list.
        _start_admin_flow(chat_id, "limit")
        return

    if action == "overview":
        s = telegram_link.admin_overview_stats()
        text = ("📊 *Overview*\n"
                f"Users: *{s['users']}* ({s['tg_linked']} linked to Telegram)\n"
                f"Admins: *{s['admins']}* · Zip-allowed: *{s['zip_allowed']}* · Suspended: *{s['suspended']}*\n"
                f"Jobs: *{s['jobs_total']}* total, *{s['jobs_deployed']}* deployed")
        _edit_or_send(chat_id, message_id, text, reply_markup={"inline_keyboard": [[{"text": "⬅️ Menu", "callback_data": "admin:menu"}]]})
        return

    if action == "users":
        page = int(ref) if ref.isdigit() else 0
        text = _admin_users_text(page)
        _edit_or_send(chat_id, message_id, text, reply_markup=_admin_users_kb(page))
        return

    if action == "user":
        target = telegram_link.get_user_by_id(int(ref)) if ref.isdigit() else None
        if not target:
            _edit_or_send(chat_id, message_id, "That user no longer exists.")
            return
        jobs = telegram_admin_ext.jobs_for_user(target["id"])
        _edit_or_send(chat_id, message_id, _admin_user_detail_text(target, jobs),
                      reply_markup=_admin_user_row_kb(target, jobs))
        return

    if action in ("togadmin", "togzip", "togsuspend", "togqueen"):
        target = telegram_link.get_user_by_id(int(ref)) if ref.isdigit() else None
        if not target:
            _edit_or_send(chat_id, message_id, "That user no longer exists.")
            return
        note = ""
        if action == "togadmin":
            if target.get("telegram_id") == SUPER_ADMIN_TG_ID and target.get("is_admin"):
                _edit_or_send(chat_id, message_id, "Can't revoke the built-in super-admin.")
            else:
                telegram_link.set_admin(target["id"], not target.get("is_admin"))
        elif action == "togzip":
            telegram_link.set_zip_permission(target["id"], not target.get("can_upload_zip"))
        elif action == "togsuspend":
            telegram_link.set_suspended(target["id"], not target.get("is_suspended"))
        else:
            # 👑 — same decision /queen makes from typed text, and the same
            # follow-up: the flag is written first, then pushed to whatever
            # this user has running so it is not a "next deploy" surprise.
            grant = not target.get("mem_unlimited")
            telegram_link.set_unlimited_permission(target["id"], grant)
            reapplied = _safe_reapply_mem(target["id"])
            note = ("👑 Granted — no per-bot memory ceiling." if grant
                    else "👑 Removed — back to the runner's default cap.")
            note += (f" Restarted {reapplied} running bot(s)." if reapplied
                     else " Running bots pick it up on their next deploy or restart.")
        target = telegram_link.get_user_by_id(target["id"])  # fresh flags
        jobs = telegram_admin_ext.jobs_for_user(target["id"])
        card = _admin_user_detail_text(target, jobs)
        if note:
            card += f"\n\n_{note}_"
        _edit_or_send(chat_id, message_id, card,
                      reply_markup=_admin_user_row_kb(target, jobs))
        return

    if action in ("queens", "unqueen"):
        if action == "unqueen":
            target = telegram_link.get_user_by_id(int(ref)) if ref.isdigit() else None
            if target:
                telegram_link.set_unlimited_permission(target["id"], False)
                _safe_reapply_mem(target["id"])
        rows = telegram_link.list_queens()
        kb = []
        if not rows:
            lines = ["👑 *Queens* — nobody has unlimited memory right now.",
                     "Grant it from a user's card (👑 Make queen) or with "
                     "`/queen username` (or a telegram id)."]
        else:
            lines = [f"👑 *Queens* ({len(rows)}) — no per-bot memory ceiling:"]
            for r in rows:
                who = r.get("username") or f"#{r['id']}"
                lines.append(f"`{r['id']}` · {who} · tg:`{r.get('telegram_id') or '—'}`")
                kb.append([{"text": f"🚫 Remove 👑 {who}"[:60],
                            "callback_data": f"admin:unqueen:{r['id']}"}])
        kb.append([{"text": "⬅️ Menu", "callback_data": "admin:menu"}])
        _edit_or_send(chat_id, message_id, "\n".join(lines), reply_markup={"inline_keyboard": kb})
        return

    if action == "runners":
        data = telegram_admin_ext.runners_overview()
        lines = ["🖥 *Runners*"]
        kb = []
        for r in data["runners"]:
            dot = "🟢" if r.get("online") else "⚪"
            # neat admin view: free MB + exclusive owner makes the "heavy project landed on loaded runner" bug visible
            free = r.get("free_mb")
            total = r.get("total_mb")
            mem_s = f" · {int(free or 0)}MB free" + (f" of {int(total)}MB" if total else "") if r.get("online") else ""
            excl = r.get("exclusive_owner")
            excl_s = f" · 🔒 queen #{excl}" if excl else ""
            lines.append(f"{dot} {r['label']} — {'enabled' if r['enabled'] else 'disabled'} "
                         f"· {r.get('jobs', 0)}/{r.get('capacity', 0)} jobs{mem_s}{excl_s}")
            kb.append([{"text": f"{'Disable' if r['enabled'] else 'Enable'} {r['label']}",
                        "callback_data": f"admin:togrunner:{r['id']}"},
                       {"text": "🔑", "callback_data": f"admin:rotatesecretflow:{r['id']}"},
                       {"text": "🗑", "callback_data": f"admin:delrunnerconfirm:{r['id']}"}])
        if data.get("embedded"):
            e = data["embedded"]
            lines.append(f"{'🟢' if e['online'] else '⚪'} embedded · {e.get('jobs',0)}/{e.get('capacity',0)} jobs")
        kb.append([{"text": "➕ Add runner", "callback_data": "admin:addrunnerflow"}])
        kb.append([{"text": "⬅️ Menu", "callback_data": "admin:menu"}])
        _edit_or_send(chat_id, message_id, "\n".join(lines), reply_markup={"inline_keyboard": kb})
        return

    if action == "togrunner":
        res = telegram_admin_ext.toggle_runner(int(ref)) if ref.isdigit() else None
        if not res:
            _edit_or_send(chat_id, message_id, "That runner no longer exists.")
            return
        _edit_or_send(chat_id, message_id, f"✅ {res['label']} is now {'enabled' if res['enabled'] else 'disabled'}.")
        handle_admin_callback(chat_id, telegram_user_id, "runners", "", message_id)
        return

    if action == "delrunnerconfirm":
        _edit_or_send(chat_id, message_id, "Delete this runner? Jobs assigned to it will need reassigning.",
              reply_markup={"inline_keyboard": [
                  [{"text": "🗑 Yes, delete", "callback_data": f"admin:delrunner:{ref}"},
                   {"text": "✖️ Cancel", "callback_data": "admin:runners"}]]})
        return

    if action == "delrunner":
        res = telegram_admin_ext.delete_runner(int(ref)) if ref.isdigit() else {"ok": False, "error": "Bad id."}
        _edit_or_send(chat_id, message_id, f"🗑 Deleted “{res['label']}”." if res.get("ok") else f"❌ {res['error']}")
        handle_admin_callback(chat_id, telegram_user_id, "runners", "", message_id)
        return

    if action == "jobs":
        page = int(ref) if ref.isdigit() else 0
        per_page = 8
        rows, total = telegram_admin_ext.jobs_recent(limit=per_page, offset=page * per_page)
        kb = []
        lines = [f"📦 *Jobs* ({total} total) — tap 🔗 to open the hidden link:"]
        for j in rows:
            lines.append(f"#{j['id']} · {j['name']} · uid `{j.get('owner_id') or '?'}`")
            kb.append([{"text": f"🔗 #{j['id']} {j['name']}"[:60],
                        "callback_data": f"admin:job:{j['id']}"}])
        nav = []
        if page > 0:
            nav.append({"text": "◀️ Prev", "callback_data": f"admin:jobs:{page-1}"})
        if (page + 1) * per_page < total:
            nav.append({"text": "Next ▶️", "callback_data": f"admin:jobs:{page+1}"})
        if nav:
            kb.append(nav)
        kb.append([{"text": "⬅️ Menu", "callback_data": "admin:menu"}])
        _edit_or_send(chat_id, message_id, "\n".join(lines), reply_markup={"inline_keyboard": kb})
        return

    if action == "job":
        j = telegram_admin_ext.job_detail(int(ref)) if ref.isdigit() else None
        if not j:
            _edit_or_send(chat_id, message_id, "That job no longer exists.")
            return
        _edit_or_send(chat_id, message_id, _admin_job_detail_text(j),
                      reply_markup=_admin_job_kb(j))
        return

    if action == "jobsource":
        # Send the stored source as a file. Repo/zip jobs may have an empty
        # body — say so instead of an empty document.
        job_id = int(ref) if ref.isdigit() else None
        src = telegram_admin_ext.job_source(job_id) if job_id else None
        if not src:
            _send(chat_id, "That job no longer exists.")
            return
        code = src.get("code") or ""
        if not code.strip():
            extra = ""
            if src.get("repo_url"):
                extra = f"\nRepo: `{src['repo_url']}`" + (
                    f" · entry `{src['repo_entry']}`" if src.get("repo_entry") else "")
            _send(chat_id, f"*{src['name']}* has no inline source stored "
                           f"(repo/zip deploy). Files live on the runner." + extra,
                  reply_markup={"inline_keyboard": [
                      [{"text": "⬅️ Job", "callback_data": f"admin:job:{job_id}"}]]})
            return
        ext = {"python": "py", "node": "js", "javascript": "js", "bash": "sh",
               "ruby": "rb", "php": "php"}.get((src.get("language") or "").lower(), "txt")
        fname = f"{src['name']}.{ext}"
        with tempfile.NamedTemporaryFile(mode="w", suffix=f"_{fname}",
                                          delete=False, encoding="utf-8") as f:
            f.write(code)
            tmp_path = f.name
        try:
            _send_document(chat_id, tmp_path,
                           caption=f"{src['name']} — source as stored "
                                   f"(owner user #{src.get('user_id')})")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return

    if action == "joblogs":
        job_id = int(ref) if ref.isdigit() else None
        res = telegram_admin_ext.admin_logs(job_id, lines=60) if job_id else {"ok": False, "error": "Bad id."}
        if not res.get("ok"):
            _send(chat_id, f"❌ {res.get('error') or 'no logs'}")
            return
        job = res.get("job") or {}
        body = res.get("logs") or "(empty)"
        header = f"📜 *{job.get('name') or job_id}* — last lines\n"
        # Plain text: log lines break Markdown freely.
        _send_plain(chat_id, header.replace("*", "") + body)
        _send(chat_id, "—", reply_markup={"inline_keyboard": [
            [{"text": "⬅️ Job", "callback_data": f"admin:job:{job_id}"},
             {"text": "🔄 Restart", "callback_data": f"admin:jobrestart:{job_id}"}]]})
        return

    if action == "jobedit":
        job_id = int(ref) if ref.isdigit() else None
        j = telegram_admin_ext.admin_find_job(job_id) if job_id else None
        if not j:
            _send(chat_id, "That job no longer exists.")
            return
        _start_admin_flow(chat_id, "jobedit", extra={"job_id": job_id, "name": j.get("name")})
        return

    if action == "jobenv":
        job_id = int(ref) if ref.isdigit() else None
        j = telegram_admin_ext.admin_find_job(job_id) if job_id else None
        if not j:
            _send(chat_id, "That job no longer exists.")
            return
        _start_admin_flow(chat_id, "jobenv", extra={"job_id": job_id, "name": j.get("name")})
        return

    if action == "limituser":
        uid = int(ref) if ref.isdigit() else None
        target = telegram_link.get_user_by_id(uid) if uid else None
        if not target:
            _send(chat_id, "That user no longer exists.")
            return
        # Pre-fill the user, ask only for the new limit value.
        _admin_flow[chat_id] = {
            "flow": "limit", "idx": 1,
            "data": {"ref": target.get("username") or str(target.get("telegram_id") or uid)},
            "extra": {}, "expires": time.time() + _ADMIN_FLOW_TTL_S,
        }
        _send(chat_id, f"🚦 Job limit for *{target.get('username') or uid}* (#{uid}).\n"
                       f"Send a number, or `clear` to remove the override. `/cancel` to stop.")
        return

    if action == "revisions":
        job_id = int(ref) if ref.isdigit() else None
        revs = telegram_admin_ext.job_revisions(job_id) if job_id else []
        kb = []
        if not revs:
            lines = ["📜 *Revisions* — none recorded for this job."]
        else:
            lines = ["📜 *Revision history* (tap to roll back):"]
            for r in revs:
                lines.append(f"v{r['version']} · {r['action']} · {r['status']} ({r['created_at']})")
                kb.append([{"text": f"⏪ Roll back to v{r['version']}",
                            "callback_data": f"admin:rollbackconfirm:{job_id}:{r['id']}"}])
        kb.append([{"text": "⬅️ Job", "callback_data": f"admin:job:{job_id}"}])
        _edit_or_send(chat_id, message_id, "\n".join(lines), reply_markup={"inline_keyboard": kb})
        return

    if action == "rollbackconfirm":
        job_id, rev_id = (ref.split(":") + ["", ""])[:2]
        _edit_or_send(chat_id, message_id, "Roll back to this revision? This redeploys that old code now.",
              reply_markup={"inline_keyboard": [
                  [{"text": "⏪ Yes, roll back", "callback_data": f"admin:rollback:{job_id}:{rev_id}"},
                   {"text": "✖️ Cancel", "callback_data": f"admin:revisions:{job_id}"}]]})
        return

    if action == "rollback":
        job_id, rev_id = (ref.split(":") + ["", ""])[:2]
        if not (job_id.isdigit() and rev_id.isdigit()):
            _edit_or_send(chat_id, message_id, "Bad reference.")
            return
        res = telegram_admin_ext.rollback_job(int(job_id), int(rev_id))
        _edit_or_send(chat_id, message_id, "✅ Rolled back and redeployed." if res.get("ok") else f"❌ {res.get('error')}")
        handle_admin_callback(chat_id, telegram_user_id, "job", job_id, message_id)
        return

    if action in ("jobrestart", "jobstop"):
        job_id = int(ref) if ref.isdigit() else None
        if not job_id:
            _edit_or_send(chat_id, message_id, "Bad job id.")
            return
        res = (telegram_admin_ext.admin_restart_job(job_id) if action == "jobrestart"
               else telegram_admin_ext.admin_stop_job(job_id))
        if not res.get("ok"):
            _edit_or_send(chat_id, message_id, f"❌ {res['error']}")
            return
        _edit_or_send(chat_id, message_id, f"✅ {'Restarted' if action == 'jobrestart' else 'Stopped'}.")
        handle_admin_callback(chat_id, telegram_user_id, "job", str(job_id), message_id)
        return

    if action == "jobdelconfirm":
        job_id = int(ref) if ref.isdigit() else None
        j = telegram_admin_ext.admin_find_job(job_id) if job_id else None
        if not j:
            _edit_or_send(chat_id, message_id, "That job no longer exists.")
            return
        _edit_or_send(chat_id, message_id, f"Delete *{j['name']}* (owned by user #{j['user_id']})? This cannot be undone.",
              reply_markup={"inline_keyboard": [
                  [{"text": "🗑 Yes, delete", "callback_data": f"admin:jobdel:{job_id}"},
                   {"text": "✖️ Cancel", "callback_data": f"admin:job:{job_id}"}]]})
        return

    if action == "jobdel":
        job_id = int(ref) if ref.isdigit() else None
        res = telegram_admin_ext.admin_delete_job(job_id) if job_id else {"ok": False, "error": "Bad job id."}
        if not res.get("ok"):
            _edit_or_send(chat_id, message_id, f"❌ {res['error']}")
            return
        _edit_or_send(chat_id, message_id, "🗑 Deleted.", reply_markup={"inline_keyboard": [[{"text": "⬅️ Jobs", "callback_data": "admin:jobs:0"}]]})
        return

    if action == "audit":
        rows = telegram_admin_ext.audit_log_recent()
        if not rows:
            lines = ["📝 *Audit log* — nothing recorded yet."]
        else:
            lines = ["📝 *Audit log* (most recent):"]
            for r in rows:
                who = r.get("admin_name") or "system"
                target = f"`{r.get('target')}`" if r.get("target") else "—"
                lines.append(f"· {who} {r['action']} → {target} ({r['created_at']})")
        _edit_or_send(chat_id, message_id, "\n".join(lines),
              reply_markup={"inline_keyboard": [[{"text": "⬅️ Menu", "callback_data": "admin:menu"}]]})
        return

    if action == "abuse":
        rows = telegram_admin_ext.abuse_reports_open()
        kb = []
        if not rows:
            lines = ["🚩 *Abuse reports* — none open."]
        else:
            lines = ["🚩 *Open abuse reports*:"]
            for r in rows:
                lines.append(f"#{r['id']} · {r.get('reason') or 'no reason given'} · {r['url'][:40]}")
                kb.append([{"text": f"✅ Resolve #{r['id']}", "callback_data": f"admin:resolveabuse:{r['id']}"}])
        kb.append([{"text": "⬅️ Menu", "callback_data": "admin:menu"}])
        _edit_or_send(chat_id, message_id, "\n".join(lines), reply_markup={"inline_keyboard": kb})
        return

    if action == "resolveabuse":
        ok = telegram_admin_ext.resolve_abuse_report(int(ref)) if ref.isdigit() else False
        _edit_or_send(chat_id, message_id, "✅ Marked resolved." if ok else "Couldn't find that report.")
        handle_admin_callback(chat_id, telegram_user_id, "abuse", "", message_id)
        return

    if action == "security":
        s = telegram_admin_ext.security_clusters_summary()
        text = (f"🔍 *Security summary*\n"
                f"Fingerprint clusters (shared device across accounts): *{s['fingerprint_clusters']}*")
        kb = [[{"text": "🔗 See the clusters", "callback_data": "admin:clusters"}],
              [{"text": "⬅️ Menu", "callback_data": "admin:menu"}]]
        _edit_or_send(chat_id, message_id, text, reply_markup={"inline_keyboard": kb})
        return

    if action == "clusters":
        rows = telegram_admin_ext.fingerprint_clusters()
        if not rows:
            lines = ["🔗 *Clusters* — no shared-device accounts found."]
        else:
            lines = ["🔗 *Accounts sharing a device:*"]
            for r in rows:
                names = ", ".join(r["usernames"]) or "(no usernames)"
                lines.append(f"`{r['fingerprint']}` ({r['count']}): {names}")
        _edit_or_send(chat_id, message_id, "\n".join(lines),
              reply_markup={"inline_keyboard": [[{"text": "⬅️ Menu", "callback_data": "admin:menu"}]]})
        return

    if action == "searchflow":
        _start_admin_flow(chat_id, "search")
        return

    if action == "signups":
        rows = telegram_admin_ext.recent_signups()
        if not rows:
            lines = ["🆕 *Signups, last 24h* — none."]
        else:
            lines = [f"🆕 *Signups, last 24h ({len(rows)}):*"]
            for r in rows:
                lines.append(f"`{r['id']}` · {r.get('username') or '(no username)'} · "
                             f"tg:`{r.get('telegram_id') or '—'}`")
        _edit_or_send(chat_id, message_id, "\n".join(lines),
              reply_markup={"inline_keyboard": [[{"text": "⬅️ Menu", "callback_data": "admin:menu"}]]})
        return

    if action == "exportmenu":
        _edit_or_send(chat_id, message_id, "📤 *Export* — pick one:", reply_markup={"inline_keyboard": [
            [{"text": "Users CSV", "callback_data": "admin:exportfile:users"},
             {"text": "Jobs CSV", "callback_data": "admin:exportfile:jobs"}],
            [{"text": "⬅️ Menu", "callback_data": "admin:menu"}]]})
        return

    if action == "exportfile":
        which = ref
        csv_text = (telegram_admin_ext.export_users_csv() if which == "users"
                    else telegram_admin_ext.export_jobs_csv())
        with tempfile.NamedTemporaryFile(mode="w", suffix=f"_{which}.csv",
                                          delete=False, encoding="utf-8") as f:
            f.write(csv_text)
            tmp_path = f.name
        try:
            _send_document(chat_id, tmp_path, caption=f"{which} export")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return

    if action == "store":
        rows = telegram_admin_ext.store_pending()
        kb = []
        if not rows:
            lines = ["🏪 *Store queue* — nothing pending."]
        else:
            lines = ["🏪 *Pending listings:*"]
            for r in rows:
                lines.append(f"`{r['id']}` · {r['title']} · by {r.get('author_name')}")
                kb.append([{"text": f"✅ Approve #{r['id']}", "callback_data": f"admin:storeapprove:{r['id']}"},
                           {"text": f"🚫 Reject #{r['id']}", "callback_data": f"admin:storereject:{r['id']}"}])
        kb.append([{"text": "⬅️ Menu", "callback_data": "admin:menu"}])
        _edit_or_send(chat_id, message_id, "\n".join(lines), reply_markup={"inline_keyboard": kb})
        return

    if action in ("storeapprove", "storereject"):
        ok = telegram_admin_ext.store_set_status(int(ref), "approved" if action == "storeapprove" else "rejected")
        _edit_or_send(chat_id, message_id, "✅ Updated." if ok else "❌ Couldn't find that listing.")
        handle_admin_callback(chat_id, telegram_user_id, "store", "", message_id)
        return

    if action == "terms":
        s = telegram_admin_ext.terms_status_summary()
        not_agreed = telegram_admin_ext.users_without_terms()
        lines = [f"📜 *Terms status*\n{s['agreed']}/{s['total']} agreed, {s['not_agreed']} have not."]
        if not_agreed:
            lines.append("\n*Not agreed (recent):*")
            for r in not_agreed:
                lines.append(f"`{r['id']}` · {r.get('username') or '(no username)'}")
        _edit_or_send(chat_id, message_id, "\n".join(lines),
              reply_markup={"inline_keyboard": [[{"text": "⬅️ Menu", "callback_data": "admin:menu"}]]})
        return

    if action == "auditadmins":
        admins = telegram_admin_ext.list_admins()
        kb = [[{"text": a["username"] or f"#{a['id']}", "callback_data": f"admin:auditby:{a['username']}"}]
              for a in admins if a.get("username")]
        kb.append([{"text": "⬅️ Menu", "callback_data": "admin:menu"}])
        _edit_or_send(chat_id, message_id, "🧑‍⚖️ *Audit by admin* — pick one:", reply_markup={"inline_keyboard": kb})
        return

    if action == "auditby":
        rows = telegram_admin_ext.audit_log_by_admin(ref)
        if not rows:
            lines = [f"📝 No audit entries for {ref}."]
        else:
            lines = [f"📝 *{ref}'s recent actions:*"]
            for r in rows:
                target = f"`{r.get('target')}`" if r.get("target") else "—"
                lines.append(f"· {r['action']} → {target} ({r['created_at']})")
        _edit_or_send(chat_id, message_id, "\n".join(lines),
              reply_markup={"inline_keyboard": [[{"text": "⬅️ Menu", "callback_data": "admin:menu"}]]})
        return

    if action == "togmaint":
        telegram_admin_ext.set_maintenance_mode(not telegram_admin_ext.get_maintenance_mode())
        _edit_or_send(chat_id, message_id, "🛠 *Admin panel*", reply_markup=_admin_menu_kb())
        return

    if action == "bulksuspendflow":
        _start_admin_flow(chat_id, "bulksuspend")
        return

    if action == "rotatesecretflow":
        if not ref.isdigit():
            _edit_or_send(chat_id, message_id, "Bad runner id.")
            return
        _start_admin_flow(chat_id, "rotatesecret", extra={"runner_id": ref})
        return

    if action == "bans":
        rows = telegram_admin_ext.list_banned()
        kb = [[{"text": "🚫 Ban someone", "callback_data": "admin:bansflow"}]]
        if not rows:
            lines = ["⛔ *Bans* — nobody is banned."]
        else:
            lines = ["⛔ *Banned Telegram ids:*"]
            for r in rows:
                lines.append(f"`{r['telegram_id']}` · {r.get('reason') or 'no reason'} ({r['created_at']})")
                kb.append([{"text": f"Unban {r['telegram_id']}", "callback_data": f"admin:unban:{r['telegram_id']}"}])
        kb.append([{"text": "⬅️ Menu", "callback_data": "admin:menu"}])
        _edit_or_send(chat_id, message_id, "\n".join(lines), reply_markup={"inline_keyboard": kb})
        return

    if action == "unban":
        ok = telegram_admin_ext.unban_telegram_id(int(ref)) if ref.isdigit() else False
        _edit_or_send(chat_id, message_id, "✅ Unbanned." if ok else "That id wasn't banned.")
        handle_admin_callback(chat_id, telegram_user_id, "bans", "", message_id)
        return

    if action == "broadcast":
        _start_admin_flow(chat_id, "broadcast")
        return

    if action in ("broadcast_confirm", "broadcast_cancel", "broadcast_status", "broadcast_preview"):
        # Rich broadcast callbacks — explicit confirmation / cancel / status
        try:
            from services import pingbot_broadcast as _pb
            # ref may contain campaign id; action already split? Some callbacks encode as admin:broadcast_confirm:123
            # In _handle_admin_callback_inner, action is first part after 'admin:', ref is second. For broadcast_confirm:123, action==broadcast_confirm and ref==123
            cid_str = (ref or "").strip()
            # also handle case where action contains colon remnants
            if not cid_str.isdigit() and ":" in action:
                # fallback parse
                parts = action.split(":",1)
                if len(parts)>1:
                    cid_str = parts[1]
                    action = parts[0]
            if not cid_str.isdigit():
                _edit_or_send(chat_id, message_id, "Bad broadcast id.")
                return
            cid = int(cid_str)
            if action == "broadcast_confirm":
                ok = _pb.confirm_campaign(cid, telegram_user_id)
                if ok:
                    _edit_or_send(chat_id, message_id, f"✅ Broadcast #{cid} is now sending.", reply_markup=_pb.status_keyboard(cid))
                    # also send status snapshot
                    try:
                        _send(chat_id, _pb.status_text(cid), reply_markup=_pb.status_keyboard(cid))
                    except Exception:
                        pass
                else:
                    _edit_or_send(chat_id, message_id, f"❌ Could not confirm #{cid} (maybe already sent or cancelled).")
                return
            if action == "broadcast_cancel":
                ok = _pb.cancel_campaign(cid, telegram_user_id)
                _edit_or_send(chat_id, message_id, f"✅ Broadcast #{cid} cancelled." if ok else f"❌ Could not cancel #{cid}.")
                try:
                    _send(chat_id, _pb.status_text(cid), reply_markup=_pb.status_keyboard(cid))
                except Exception:
                    pass
                return
            if action in ("broadcast_status", "broadcast_preview"):
                _edit_or_send(chat_id, message_id, _pb.status_text(cid), reply_markup=_pb.status_keyboard(cid))
                return
        except Exception as exc:
            logger.exception("broadcast callback %s failed", action)
            _edit_or_send(chat_id, message_id, f"⚠️ Broadcast action failed: {type(exc).__name__}")
        return

    if action == "bansflow":
        _start_admin_flow(chat_id, "ban")
        return

    if action == "addrunnerflow":
        _start_admin_flow(chat_id, "addrunner")
        return

# CODE-VIA-CHAT — READ THIS BEFORE TOUCHING /code, /update, or _pending.
#
# This used to accept a pasted snippet with no account check and deploy it
# straight to the runner, bypassing the jobs table entirely — removed after
# an unlinked chat was able to run arbitrary code on the server. /code and
# /update are back by request, rebuilt with the hole closed:
#   · Both commands are gated through _require_link() in poll_loop() below,
#     the SAME gate /restart, /stop and /delete already use. Nothing here
#     skips it.
#   · The actual create/redeploy logic lives in services/bot_ops.py
#     (create_app / update_code), which goes through the jobs table and
#     MAX_JOBS_PER_USER cap exactly like the website's editor does — see
#     that module's docstring for the full reasoning.
#   · Code arrives in the message AFTER the command, as plain text (~4096
#     char Telegram cap — fine for a quick one-line fix) or as an uploaded
#     document (up to 20MB, enough for a real app), tracked per-chat in
#     _pending{} with a 5-minute expiry so a stale "waiting for code" state
#     can never quietly capture an unrelated later message.
RUNNER_SECRET = os.getenv("RUNNER_SERVICE_SECRET", "")


def _site_base() -> str:
    """The URL the Mini App button opens — THIS deployment's own URL.

    THE HARDCODED DEFAULT WAS A TRAP, and it is the single most likely reason
    a working deployment still shows "nothing happens" on tap.

    It used to be:

        SITE_BASE = os.getenv("SITE_BASE_URL", "https://ahadorg.onrender.com")

    SITE_BASE_URL is `sync: false` in render.yaml, i.e. it is NOT set for you
    — a fresh deploy has to add it by hand. Miss that one step and the button
    is still built, still rendered, and still tappable, but it opens somebody
    else's host. What happens then depends on what lives there:

      * host gone / renamed  -> Telegram opens a webview on a dead URL and
                                closes it. Tap, flicker, nothing.
      * host alive           -> a DIFFERENT server verifies the initData with
                                a DIFFERENT bot token, so sign-in fails with
                                bad_hash and the phone shows an error naming
                                a bot the user has never heard of.

    Neither says "your SITE_BASE_URL is wrong", and the deploy is green
    throughout. Render sets RENDER_EXTERNAL_URL to the service's own public
    URL automatically, so the right default is knowable without asking: use
    it, and treat an explicit SITE_BASE_URL as an override for a custom
    domain. Falling back to a literal host that belongs to one particular
    deployment can only ever be right for that one deployment.
    """
    for name in ("SITE_BASE_URL", "PUBLIC_BASE_URL", "RENDER_EXTERNAL_URL"):
        val = os.getenv(name, "").strip().rstrip("/")
        if val:
            return val
    return ""


SITE_BASE = _site_base()

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""
TG_FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}" if BOT_TOKEN else ""

# Extension -> runtime. Matches RS_EXT_LANG in static/pro.js — RunSpace can
# only ever RUN these five languages, so anything else is rejected up front
# with a clear reason instead of failing later inside the runner.
_CODE_EXT_LANG = {
    "py": "python", "pyw": "python",
    "js": "javascript", "mjs": "javascript", "cjs": "javascript",
    "sh": "bash", "bash": "bash",
    "rb": "ruby",
    "php": "php",
}

# chat_id -> {"mode": "create"|"update", "user_id", "ref"/"name", "expires"}
# One pending slot per chat: sending a new /code or /update just overwrites
# whatever was waiting, so there is never a stale slot fighting a fresh one.
_pending = {}
_PENDING_TTL_S = 300

# chat_id -> set of guide topics already shown this process (avoid spam)
_guide_shown = {}


# Parallel to _pending, but for multi-field ADMIN actions: bot asks one
# field at a time ("URL?" -> user answers -> "Secret?" -> user answers ->
# runs) instead of requiring everything on one line. Each flow is a list of
# (field_name, prompt) steps; _run_admin_flow below executes the matching
# function once every field is collected.
_admin_flow = {}
_ADMIN_FLOW_TTL_S = 300

ADMIN_FLOWS = {
    "addrunner": [
        ("label", "Label for this runner? (a short name, e.g. `render-eu`)"),
        ("url", "Its URL? (e.g. `https://my-runner.onrender.com`)"),
        ("secret", "Its `RUNNER_SERVICE_SECRET`? ⚠️ delete this message after I confirm."),
    ],
    "ban": [
        ("telegram_id", "Telegram id to ban? (the number, not a username)"),
        ("reason", "Reason? (or send `-` for none)"),
    ],
    "limit": [
        ("ref", "Which user? (username or telegram id)"),
        ("value", "New job limit? (a number, or `clear` to remove the override)"),
    ],
    "broadcast": [
        ("message", "What should I send to every linked user? (supports <b>, <i>, <code>, <a href=\"https://...\">)"),
        ("buttons", "Optional URL buttons? One per line as \`Label | https://example.com\` — or send \`-\` to skip."),
    ],
    "search": [
        ("query", "Search for what? (part of a username or email)"),
    ],
    "bulksuspend": [
        ("ids", "User ids to suspend, comma-separated (e.g. `12,45,90`)"),
    ],
    "rotatesecret": [
        ("secret", "New `RUNNER_SERVICE_SECRET` for this runner?"),
    ],
    "jobedit": [
        ("code", "Send the new source as a message (or a single file). `/cancel` to stop."),
    ],
    "jobenv": [
        ("pair", "Send `KEY=value` to set, or `delete KEY` to remove. `/cancel` to stop."),
    ],
}


def _start_admin_flow(chat_id, flow_name, extra=None):
    steps = ADMIN_FLOWS[flow_name]
    _admin_flow[chat_id] = {"flow": flow_name, "idx": 0, "data": {}, "extra": extra or {},
                            "expires": time.time() + _ADMIN_FLOW_TTL_S}
    _send(chat_id, f"🛠 *{flow_name}* — {steps[0][1]}\n(`/cancel` to stop)")


def _advance_admin_flow(chat_id, text):
    """Called for a plain-text message while a flow is active. Stores the
    answer, asks the next question, or runs the flow once every field is
    in. Returns True if it consumed the message (caller should stop)."""
    state = _admin_flow.get(chat_id)
    if not state or state["expires"] < time.time():
        _admin_flow.pop(chat_id, None)
        return False
    steps = ADMIN_FLOWS[state["flow"]]
    field, _ = steps[state["idx"]]
    state["data"][field] = text.strip()
    state["idx"] += 1
    if state["idx"] < len(steps):
        state["expires"] = time.time() + _ADMIN_FLOW_TTL_S
        _send(chat_id, steps[state["idx"]][1])
        return True
    _admin_flow.pop(chat_id, None)
    _run_admin_flow(chat_id, state["flow"], state["data"], state.get("extra") or {})
    return True


def _run_admin_flow(chat_id, flow_name, data, extra=None):
    extra = extra or {}
    caller = telegram_link.user_for_chat(chat_id)  # private chat: chat_id == telegram user id
    if flow_name == "addrunner":
        _send(chat_id, f"Checking {data['url']}…")
        res = telegram_admin_ext.add_runner(data["label"], data["url"], data["secret"],
                                            caller["id"] if caller else None)
        _send(chat_id, f"✅ Runner “{res['label']}” registered (#{res['id']}) and enabled."
              if res.get("ok") else f"❌ {res['error']}")
    elif flow_name == "ban":
        if not data["telegram_id"].isdigit():
            _send(chat_id, "That wasn't a number — nothing banned. Try `/admin ban` again.")
            return
        reason = "" if data["reason"] == "-" else data["reason"]
        telegram_admin_ext.ban_telegram_id(int(data["telegram_id"]), chat_id, reason)
        _send(chat_id, f"⛔ Banned `{data['telegram_id']}`" + (f" — {reason}" if reason else "") + ".")
    elif flow_name == "limit":
        target = telegram_link.resolve_user_ref(data["ref"])
        if not target:
            _send(chat_id, f"No user found for “{data['ref']}”.")
            return
        val = data["value"]
        if val.lower() == "clear":
            telegram_admin_ext.set_job_limit_override(target["id"], None)
            _send(chat_id, f"✅ Job limit for {target.get('username') or data['ref']} cleared.")
        elif val.isdigit():
            telegram_admin_ext.set_job_limit_override(target["id"], int(val))
            _send(chat_id, f"✅ Job limit for {target.get('username') or data['ref']} set to {val}.")
        else:
            _send(chat_id, "That wasn't a number or `clear` — nothing changed.")
    elif flow_name == "broadcast":
        # Rich flow — create campaign with preview + explicit confirmation.
        msg = (data.get("message") or "").strip()
        buttons_raw = (data.get("buttons") or "").strip()
        if not msg:
            _send(chat_id, "Empty message — nothing sent.")
            return
        # buttons: '-' means none
        buttons = None if buttons_raw in ("", "-", "—") else buttons_raw
        # media if admin sent photo/video as message
        extra_media_type = (extra or {}).get("broadcast_media_type")
        extra_media_file_id = (extra or {}).get("broadcast_media_file_id")
        try:
            from services import pingbot_broadcast as _pb
            cid = _pb.create_campaign(chat_id, msg, buttons=buttons, media_type=extra_media_type, media_file_id=extra_media_file_id, auto_start=False)
            _send(chat_id, _pb.preview_text(cid), reply_markup=_pb.preview_keyboard(cid))
            _send(chat_id, _pb.status_text(cid), reply_markup=_pb.status_keyboard(cid))
        except Exception as exc:
            logger.exception("broadcast flow failed")
            _send(chat_id, f"❌ Broadcast failed: {type(exc).__name__}: {exc}")
    elif flow_name == "search":
        rows = telegram_admin_ext.search_users(data["query"])
        if not rows:
            _send(chat_id, f"No users matching “{data['query']}”.")
            return
        lines = [f"🔎 *{len(rows)} match(es):*"]
        for r in rows:
            lines.append(f"`{r['id']}` · {r.get('username') or '(no username)'} · "
                         f"tg:`{r.get('telegram_id') or '—'}`")
        _send(chat_id, "\n".join(lines))
    elif flow_name == "bulksuspend":
        ids = [x.strip() for x in data["ids"].split(",") if x.strip().isdigit()]
        if not ids:
            _send(chat_id, "No valid ids found — expected something like `12,45,90`.")
            return
        res = telegram_admin_ext.bulk_suspend(ids, True)
        _send(chat_id, f"✅ Suspended {len(res['ok'])}." +
              (f" Failed: {', '.join(map(str, res['failed']))}." if res['failed'] else ""))
    elif flow_name == "rotatesecret":
        runner_id = extra.get("runner_id")
        res = telegram_admin_ext.rotate_runner_secret(int(runner_id), data["secret"])
        _send(chat_id, f"✅ Secret rotated for “{res['label']}”." if res.get("ok") else f"❌ {res['error']}")
    elif flow_name == "jobedit":
        job_id = extra.get("job_id")
        code = (data.get("code") or "").strip()
        if not code:
            _send(chat_id, "Empty source — nothing changed.")
            return
        name = extra.get("name") or job_id
        with _progress(chat_id, "update", name=name) as prog:
            prog.at(18, "Reading your edit")
            prog.at(40, "Saving on the site")
            res = telegram_admin_ext.admin_update_code(int(job_id), code)
            if not res.get("ok"):
                prog.fail(res.get("error") or "update failed")
                return
            prog.at(78, "Restarting on the runner")
            prog.done(f"*{name}* updated and restarted.")
        _send(chat_id, f"✅ *{name}* updated.",
              reply_markup={"inline_keyboard": [
                  [{"text": "⬅️ Job", "callback_data": f"admin:job:{job_id}"}]]})
    elif flow_name == "jobenv":
        job_id = extra.get("job_id")
        pair = (data.get("pair") or "").strip()
        name = extra.get("name") or job_id
        if pair.lower().startswith("delete "):
            key = pair.split(None, 1)[1].strip() if " " in pair else ""
            res = telegram_admin_ext.admin_set_env(int(job_id), key, None)
        elif "=" in pair:
            key, _, val = pair.partition("=")
            res = telegram_admin_ext.admin_set_env(int(job_id), key.strip(), val)
        else:
            _send(chat_id, "Send `KEY=value` or `delete KEY`. Nothing changed.")
            return
        if not res.get("ok"):
            _send(chat_id, f"❌ {res.get('error')}")
            return
        note = "deleted" if res.get("deleted") else "set"
        restarted = " and restarted" if res.get("restarted") else ""
        _send(chat_id, f"✅ `{res.get('key')}` {note} on *{name}*{restarted}.",
              reply_markup={"inline_keyboard": [
                  [{"text": "⬅️ Job", "callback_data": f"admin:job:{job_id}"}]]})


def _tg(method, **params):
    """Call a Telegram Bot API method.

    POST + JSON body (not GET + query params): nested structures such as
    reply_markup's inline_keyboard cannot survive urlencoding — requests
    flattens them to "reply_markup=inline_keyboard" and the buttons vanish.
    """
    if not TG_API:
        return {}
    try:
        r = requests.post(f"{TG_API}/{method}", json=params, timeout=50)
        return r.json()
    except Exception as e:  # noqa: BLE001
        print(f"Telegram {method} failed: {e}")
        return {}


TG_MAX_UPLOAD_BYTES = 50 * 1024 * 1024   # Telegram bot upload ceiling


def _guide_path(name: str):
    """Path to a cartoon how-to PNG shipped with the site (static/guides/)."""
    from pathlib import Path as _P
    base = _P(__file__).resolve().parent.parent / "static" / "guides"
    path = base / f"{name}.png"
    return path if path.is_file() else None


def _send_guide(chat_id, name: str, caption: str = ""):
    """Send a step-guide cartoon if the PNG exists; otherwise just the caption.

    New users learn faster from a picture of the three taps than from a wall of
    commands. Missing art must never break /help — fall back to text only.
    Prefers sendPhoto so the cartoon shows inline; falls back to document.
    """
    path = _guide_path(name)
    if path is not None and TG_API:
        try:
            with open(path, "rb") as fh:
                r = requests.post(
                    f"{TG_API}/sendPhoto",
                    data={"chat_id": chat_id, "caption": (caption or "")[:1024]},
                    files={"photo": (path.name, fh, "image/png")},
                    timeout=60,
                )
            if (r.json() or {}).get("ok"):
                return True
            # Fallback: some clients prefer document for larger PNGs.
            _send_document(chat_id, str(path), caption=caption or "")
            return True
        except Exception:
            logger.exception("guide photo %s failed", name)
    if caption:
        _send_plain(chat_id, caption)
    return False


def _send_document(chat_id, filepath, caption=""):
    """Upload a real file to the chat (multipart, not the JSON endpoint)."""
    if not TG_API:
        return {}
    try:
        with open(filepath, "rb") as fh:
            r = requests.post(
                f"{TG_API}/sendDocument",
                data={"chat_id": chat_id, "caption": caption[:1024]},
                files={"document": (os.path.basename(filepath), fh)},
                timeout=120,
            )
        return r.json()
    except Exception as e:  # noqa: BLE001
        print("sendDocument failed:", e)
        _send(chat_id, "❌ Upload failed.")
        return {}


def _is_parse_error(result) -> bool:
    """True when Telegram rejected the message's MARKUP rather than its content.

    Deliberately WIDE. The narrow version recognised four exact phrasings, and
    every phrasing it did not know — "can't find end of the entity started at
    byte offset 41", "wrong URL host", "unsupported protocol" — meant the reply
    was dropped with nothing anywhere to say so. That is what "the bot didn't
    answer" actually is, and it is the one failure a user cannot diagnose from
    their phone. Losing the bold costs nothing; losing the answer costs a
    support message.
    """
    desc = str((result or {}).get("description") or "").lower()
    return any(word in desc for word in
               ("parse", "entit", "markup", "markdown", "bold", "italic",
                "url host", "unsupported", "blockquote"))


def _send(chat_id, text, reply_markup=None):
    data = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        # Telegram expects reply_markup as a JSON-serialised string.
        data["reply_markup"] = json.dumps(reply_markup)
    result = _tg("sendMessage", **data)
    if (result or {}).get("ok"):
        return result
    refused = str((result or {}).get("description") or "")
    if _is_parse_error(result):
        # Legacy Markdown breaks on a stray * _ ` [ in anything we do not
        # control — an app called my_bot, a log line, a traceback, a README.
        data.pop("parse_mode", None)
        result = _tg("sendMessage", **data)
        if (result or {}).get("ok"):
            return result
        refused = str((result or {}).get("description") or refused)
    # Never silent again. A dropped reply used to leave no trace at all, so
    # "the bot ignored me" and "the bot crashed" were indistinguishable — and
    # both were reported as /ping not working.
    logger.warning("TELEGRAM refused a message to chat %s (%s); first line was: %.140s",
                   chat_id, refused or result, str(text).splitlines()[0] if text else "")
    return result


def _send_plain(chat_id, text):
    """Send text we did not write (a README, a log) with no markup at all."""
    return _tg("sendMessage", chat_id=chat_id, text=text[:4096],
               disable_web_page_preview=True)


def _send_html(chat_id, text, reply_markup=None):
    """HTML parse mode — <code> chips are one-tap copy on mobile Telegram."""
    data = {"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML",
            "disable_web_page_preview": True}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    result = _tg("sendMessage", **data)
    if (result or {}).get("ok"):
        return result
    plain = re.sub(r"<[^>]+>", "", text or "")
    return _send_plain(chat_id, plain)


def _edit_or_send(chat_id, message_id, text, reply_markup=None):
    """Update the SAME message a button lives on, instead of sending a new
    one every press — without this, navigating the admin panel scrolled a
    new message down for every click, and the only obvious way back to a
    visible menu was retyping /admin. Falls back to a fresh message if the
    edit fails (message too old, or Telegram's "not modified" error)."""
    if not message_id:
        _send(chat_id, text, reply_markup)
        return
    data = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    result = _tg("editMessageText", **data)
    if not (result or {}).get("ok"):
        desc = str((result or {}).get("description") or "")
        if "message is not modified" in desc.lower():
            return  # content is already exactly this — nothing to do, not a failure
        _send(chat_id, text, reply_markup)


# ---------------------------------------------------------------------------
# "typing…" — the difference between a pause and a bot that looks dead
# ---------------------------------------------------------------------------
def _typing(chat_id) -> None:
    """One "typing…" indicator. Never raises: it is a courtesy, not a step."""
    try:
        _tg("sendChatAction", chat_id=chat_id, action="typing")
    except Exception:                                              # noqa: BLE001
        pass


class _working:
    """`with _working(chat_id):` — keep the indicator alive while working.

    Telegram shows "typing…" for about five seconds per call. A command that
    reads the runner, clones a repo or installs dependencies takes longer than
    that, and the complaint was exactly this: the bot answers two or three
    seconds later with nothing on screen in between, so people send the command
    again. One call every four seconds costs nothing and says "still working".
    """

    def __init__(self, chat_id, every: float = 4.0):
        self.chat_id = chat_id
        self.every = every
        self._stop = threading.Event()

    def __enter__(self):
        _typing(self.chat_id)
        threading.Thread(target=self._loop, daemon=True).start()
        return self

    def _loop(self):
        while not self._stop.wait(self.every):
            _typing(self.chat_id)

    def __exit__(self, *exc):
        self._stop.set()
        return False


# ---------------------------------------------------------------------------
# Live progress card — the difference between "bot stopped" and "still working"
# ---------------------------------------------------------------------------
# A deploy takes long enough that a bare "typing…" still feels like a freeze.
# This posts ONE message and rewrites it as each step finishes, with a bar that
# fills 0→100% and a short label that changes so the person always knows what
# is happening right now. Used by /code, /update, /import, zip deploy, and the
# admin edit path.

_PROGRESS_SPIN = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

_PROGRESS_SCRIPTS = {
    # (pct, label) — labels are the ones the user watches; keep them short.
    "create": [
        (8,  "Got your code"),
        (22, "Checking the name & your slot"),
        (40, "Uploading to the site"),
        (58, "Handing off to the runner"),
        (75, "Installing packages"),
        (88, "Starting the app"),
        (96, "Almost there"),
    ],
    "update": [
        (10, "Got the new code"),
        (28, "Saving a snapshot"),
        (48, "Uploading the edit"),
        (68, "Restarting in place"),
        (88, "Bringing it back up"),
        (96, "Almost there"),
    ],
    "import": [
        (8,  "Reading the GitHub link"),
        (22, "Scanning the repo"),
        (40, "Cloning the branch"),
        (58, "Installing requirements"),
        (78, "Starting the app"),
        (92, "Almost there"),
    ],
    "zip": [
        (8,  "Got the zip"),
        (25, "Unpacking files"),
        (45, "Finding the entry point"),
        (65, "Installing requirements"),
        (85, "Starting the app"),
        (95, "Almost there"),
    ],
}


def _progress_bar(pct: int, width: int = 10) -> str:
    """A fixed-width bar that reads cleanly inside a Telegram monospace run."""
    pct = max(0, min(100, int(pct)))
    filled = round(width * pct / 100)
    return "▓" * filled + "░" * (width - filled)


class _progress:
    """`with _progress(chat_id, kind, name=...) as p: p.at(40, "…"); p.done()`

    Posts one card and edits it in place. Typing indicator keeps spinning in
    the background so even between edits the chat never looks frozen.
    """

    def __init__(self, chat_id, kind: str = "create", name: str = ""):
        self.chat_id = chat_id
        self.kind = kind if kind in _PROGRESS_SCRIPTS else "create"
        self.name = name or "app"
        self.message_id = None
        self._pct = 0
        self._label = "Starting"
        self._spin_i = 0
        self._stop = threading.Event()
        self._failed = None
        self._done_text = None

    def __enter__(self):
        self._render(force_send=True)
        # Keep "typing…" alive AND nudge the spinner glyph every few seconds
        # so a long step (pip install) still looks alive even before the next
        # p.at() call rewrites the percentage.
        threading.Thread(target=self._pulse, daemon=True).start()
        return self

    def _pulse(self):
        while not self._stop.wait(3.5):
            try:
                _typing(self.chat_id)
                self._spin_i = (self._spin_i + 1) % len(_PROGRESS_SPIN)
                if self.message_id and self._failed is None and self._done_text is None:
                    self._render()
            except Exception:
                pass

    def _card(self) -> str:
        spin = _PROGRESS_SPIN[self._spin_i % len(_PROGRESS_SPIN)]
        bar = _progress_bar(self._pct)
        head = f"{spin} *Deploying* `{self.name}`"
        if self._failed is not None:
            return (f"🔴 *Failed* `{self.name}`\n"
                    f"`{bar}` {self._pct}%\n"
                    f"{self._failed}")
        if self._done_text is not None:
            return (f"✅ *Done* `{self.name}`\n"
                    f"`{bar}` 100%\n"
                    f"{self._done_text}")
        return (f"{head}\n"
                f"`{bar}` {self._pct}%\n"
                f"_{self._label}_")

    def _render(self, force_send: bool = False):
        text = self._card()
        if self.message_id and not force_send:
            data = {"chat_id": self.chat_id, "message_id": self.message_id,
                    "text": text, "parse_mode": "Markdown"}
            result = _tg("editMessageText", **data)
            if (result or {}).get("ok"):
                return
            desc = str((result or {}).get("description") or "")
            if "message is not modified" in desc.lower():
                return
            # Fall through to a fresh send if the edit is refused (too old, etc.)
        result = _send(self.chat_id, text)
        try:
            self.message_id = ((result or {}).get("result") or {}).get("message_id")                               or self.message_id
        except Exception:
            pass

    def at(self, pct: int, label: str = ""):
        """Jump the bar to `pct` and (optionally) change the label."""
        self._pct = max(self._pct, min(99, int(pct)))  # 100 reserved for done()
        if label:
            self._label = label
        self._spin_i = (self._spin_i + 1) % len(_PROGRESS_SPIN)
        self._render()

    def step(self, index: int):
        """Apply the Nth scripted step for this kind of deploy."""
        script = _PROGRESS_SCRIPTS.get(self.kind) or _PROGRESS_SCRIPTS["create"]
        if index < 0 or index >= len(script):
            return
        pct, label = script[index]
        self.at(pct, label)

    def fail(self, reason: str):
        self._failed = (reason or "something went wrong")[:300]
        self._render()

    def done(self, note: str = "Running."):
        self._pct = 100
        self._done_text = (note or "Running.")[:300]
        self._render()

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        if exc_type is not None and self._failed is None and self._done_text is None:
            try:
                self.fail(f"{exc_type.__name__}: {exc}")
            except Exception:
                pass
        return False  # never swallow the error


# Commands that touch the network or the runner, so they get the repeating
# indicator instead of a single one.
_SLOW_COMMANDS = frozenset((
    "/ping", "/apps", "/jobs", "/list", "/status", "/uptime", "/info", "/logs", "/restart", "/stop",
    "/delete", "/source", "/import", "/projects", "/latest", "/autodeploy",
    "/limits", "/admin", "/details", "/see", "/queen", "/rename", "/update", "/code",
    "/health", "/backup", "/restore", "/rollback", "/token", "/history", "/env",
    "/guide", "/guides", "/howto", "/recover", "/rescue", "/whoami",
    "/templates", "/usage",
))


# ==================== IDENTITY ====================
# An unlinked chat gets the SAME reply as an unknown command. Saying "you need
# to link first" confirms the bot is attached to something worth attacking;
# saying nothing useful costs a legitimate user one visit to /start, which
# does explain the link step — but only to a chat that asked for help, not to
# one probing for a deploy endpoint.
UNKNOWN_REPLY = "🤔 Unknown command. Send /start or /guide — or try /apps, /id, /help."


def _require_link(chat_id):
    """The account this chat speaks for, or None (and the chat is answered).

    Returns None for unlinked AND for suspended accounts, so a suspension
    closes the Telegram door too — otherwise suspending someone on the web
    would leave them a second way in.
    """
    user = telegram_link.user_for_chat(chat_id)
    if not user:
        _send(chat_id, UNKNOWN_REPLY)
        return None
    return user


def handle_link(chat_id, text, display_name=""):
    """/link 123456 — redeem a code issued by the website."""
    parts = (text or "").split()
    if len(parts) < 2:
        _send(chat_id,
              "🔗 *Connect your account*\n\n"
              "Open your CodeNest dashboard → Settings → *Connect Telegram* "
              "and tap the button. It brings you back here and connects you "
              "automatically — nothing to type.",
              reply_markup=_menu_buttons())
        return

    already = telegram_link.user_for_chat(chat_id)
    if already:
        _send(chat_id, f"✅ This chat is already connected to *{already['username']}*.")
        return

    # A 6-digit code is a million wide; without a per-chat cap the bot itself
    # becomes the brute-force tool.
    guard = _link_rate_ok(chat_id)
    if not guard:
        _send(chat_id, "⏳ Too many attempts. Wait a few minutes and try again.")
        return

    res = telegram_link.redeem_code(parts[1], chat_id, display_name)
    if res.get("ok"):
        # A button back, because the user arrived here FROM the dashboard and
        # the dashboard is where the connection now shows up. Telling them to
        # "go back" without a link is how a two-tap flow becomes a hunt again.
        #
        # The linked row is RE-READ instead of trusted from the redeem result:
        # it is the one that carries the 👑 flag, and this is the first screen a
        # newly connected queen ever sees. Showing everybody else's help here
        # was the same "the privileges are invisible" complaint /start had.
        linked = telegram_link.user_for_chat(chat_id) or {"username": res["username"]}
        rows = [[{"text": "📦 Open dashboard", "url": f"{SITE_BASE}/bots"}]] \
            if SITE_BASE else []
        if _user_is_queen(linked):
            rows.insert(0, [{"text": "👑 Queen panel", "callback_data": "queen:menu"},
                            {"text": "📦 Projects", "callback_data": "qproj:list"}])
        _send(chat_id,
              f"✅ Connected to *{res['username']}*.\n\n" +
              _help_text(linked).split("\n\n", 1)[-1],
              reply_markup={"inline_keyboard": rows} if rows else None)
        return

    telegram_link.note_failed_attempt(parts[1])
    reason = res.get("reason")
    if reason == "chat_already_linked":
        _send(chat_id, "❌ This Telegram account is already connected to another CodeNest account.")
    elif reason == "expired":
        _send(chat_id, "⌛ That code has expired. Generate a new one on the site.")
    elif reason == "suspended":
        _send(chat_id, "❌ That account is suspended.")
    else:
        # "unknown" and "malformed" get one message on purpose: telling a
        # guesser that a code was well-formed but wrong is a hint.
        _send(chat_id, "❌ That code is not valid. Generate a fresh one on the site.")


def _cmd_arg(text):
    """Everything after the command word. "/logs my bot" -> "my bot"."""
    parts = (text or "").split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _tg_display(msg):
    """A human label for whoever sent this message.

    Prefers @username because that is what a person recognises; falls back to
    the first name, which Telegram always provides.
    """
    frm = (msg or {}).get("from") or {}
    uname = (frm.get("username") or "").strip()
    if uname:
        return "@" + uname
    return (frm.get("first_name") or "").strip()


# A Mini App button needs an HTTPS URL — Telegram refuses http:// and refuses
# to render the button at all, so a local dev SITE_BASE must fall back to a
# plain link rather than producing a keyboard Telegram will reject.
def _miniapp_ok() -> bool:
    return SITE_BASE.startswith("https://")


def _open_button(label="🚀 Open CodeNest"):
    """The Mini App launch button, or a plain link when that is not possible.

    `web_app` opens the existing site INSIDE Telegram, where initData signs
    the user in automatically. `url` opens a browser, where they would have to
    log in — the same destination, a worse trip, but better than no button.
    """
    if not SITE_BASE:
        return None
    if _miniapp_ok():
        return {"text": label, "web_app": {"url": f"{SITE_BASE}/bots"}}
    return {"text": label, "url": f"{SITE_BASE}/bots"}


def _open_kb(label="🚀 Open CodeNest"):
    """A keyboard holding just the launch button, or None."""
    btn = _open_button(label)
    return {"inline_keyboard": [[btn]]} if btn else None


def _menu_buttons():
    """Kept as the name older call sites use; same single button."""
    return _open_kb()


def set_menu_button():
    """Register the persistent 'Open CodeNest' button next to the input box.

    This is the always-available entry point — it does not depend on the user
    finding an old message with an inline button in it.
    """
    if not BOT_TOKEN:
        return False
    if not _miniapp_ok():
        # SAY WHY THE BUTTON IS MISSING. This returned False in silence, so a
        # deployment with no SITE_BASE_URL (or an http:// one, which Telegram
        # refuses for web_app) produced a bot with no Open button and no
        # explanation anywhere — indistinguishable from a broken Mini App.
        logger.error(
            "TELEGRAM: no 'Open CodeNest' button — the Mini App URL is %s. "
            "Telegram only accepts an https:// URL for a web_app button. Set "
            "SITE_BASE_URL to this service's public https URL and redeploy.",
            f"'{SITE_BASE}'" if SITE_BASE else "not configured (SITE_BASE_URL "
            "and RENDER_EXTERNAL_URL are both unset)")
        return False
    res = _tg("setChatMenuButton", menu_button={
        "type": "web_app",
        "text": "Open CodeNest",
        "web_app": {"url": f"{SITE_BASE}/bots"},
    })
    ok = bool((res or {}).get("ok"))
    if not ok:
        print("menu button not set:", res)
    return ok


def _queen_help_block() -> str:
    """The 👑 half of /help: what queen access gets you, and how to use it.

    A 👑 account used to be shown exactly the same help as everybody else, so
    the privileges an admin had granted were invisible — nobody knew the memory
    ceiling was off, that a whole project could arrive as a zip, or that
    /projects existed at all. This is the separate interface for queens: the
    same commands, plus the ones only they have, spelled out with the steps.
    """
    return (
        "\n\n👑 *Your queen access*\n"
        "• *No memory ceiling* — your apps run at full size and won't be killed "
        "for using what they need.\n"
        "• *Whole projects* — send a `.zip` right after `/code name` or "
        "`/update name`: folders, `requirements.txt`, data files, all of it.\n"
        "• *Any public repo, any branch* — `/import owner/repo [name]`, and a "
        "branch with `/import owner/repo/tree/branch`.\n"
        "• *Keep it current* — ⬆️ `/latest name` and ⚙️ `/autodeploy name on` "
        "follow the branch the way a platform does.\n"
        "• *`/projects`* — your repo apps, the commit each is on, and a button "
        "to pull the newest."
    )


def _main_kb(user=None):
    """The keyboard under /start and /help.

    Everyone gets: Open + how-to guides + common actions.
    A 👑 account also gets the queen panel and projects row.
    Admin accounts get a one-tap /admin entry (same as typing /admin).
    """
    btn = _open_button()
    rows = []
    if btn:
        rows.append([btn])
    rows.append([
        {"text": "📖 How to start", "callback_data": "help:start"},
        {"text": "🌿 Import repo", "callback_data": "help:import"},
    ])
    rows.append([
        {"text": "🔢 My ids", "callback_data": "help:id"},
        {"text": "🔑 Token tips", "callback_data": "help:token"},
    ])
    rows.append([
        {"text": "📦 My apps", "callback_data": "apps:list"},
        {"text": "📡 Ping", "callback_data": "ping:site"},
    ])
    if _user_is_queen(user):
        rows.insert(1 if btn else 0, [
            {"text": "👑 Queen panel", "callback_data": "queen:menu"},
            {"text": "📦 My projects", "callback_data": "qproj:list"},
        ])
    # Admin shortcut — only when this chat is an admin, so non-admins never
    # see a dead button. Command /admin still works the same way.
    try:
        tg_id = (user or {}).get("telegram_id")
        if user and _is_admin(user, tg_id):
            rows.append([{"text": "🛠 Admin panel", "callback_data": "admin:menu"},
                         {"text": "🖥 Runners", "callback_data": "admin:runners"}])
    except Exception:
        pass
    return {"inline_keyboard": rows} if rows else None


def _help_guide_kb(user=None):
    """Buttons under how-to messages. Admin guides only for admins."""
    rows = [
        [{"text": "📖 Start", "callback_data": "help:start"},
         {"text": "🌿 Import", "callback_data": "help:import"}],
        [{"text": "✍️ Code", "callback_data": "help:code"},
         {"text": "♻️ Update", "callback_data": "help:update"}],
        [{"text": "🔢 Ids", "callback_data": "help:id"},
         {"text": "🔑 Token", "callback_data": "help:token"}],
        [{"text": "📦 Apps", "callback_data": "help:apps"},
         {"text": "🏠 Help home", "callback_data": "help:home"}],
    ]
    try:
        if user and _is_admin(user, (user or {}).get("telegram_id")):
            rows.append([{"text": "🛠 Admin how-to", "callback_data": "help:admin"}])
    except Exception:
        pass
    return {"inline_keyboard": rows}


def _queen_panel_kb():
    """Buttons for the 👑 panel: what a queen can actually do, one tap each."""
    rows = [[{"text": "📦 My projects", "callback_data": "qproj:list"},
             {"text": "📊 My apps", "callback_data": "queen:apps"}],
            [{"text": "🖥 Runner", "callback_data": "queen:runner"},
             {"text": "💎 Exclusive 512MB", "callback_data": "queen:runner"}],
            # An empty ref means "all of mine": the same screen /latest with no
            # argument shows, listing every repo app and whether its branch moved.
            [{"text": "⬆️ Deploy latest commits", "callback_data": "latest:"},
             {"text": "🌿 Import a repo", "callback_data": "help:import"}]]
    # Optional starter pack — only when an admin actually configured one.
    if _queen_repo_parts()[0]:
        rows.insert(1, [{"text": "▶️ Run starter", "callback_data": "qproj:run"},
                        {"text": "📖 Starter README", "callback_data": "qproj:readme"}])
    btn = _open_button("🚀 Open CodeNest")
    if btn:
        rows.append([btn])
    return {"inline_keyboard": rows}


def _queen_panel_text(user) -> str:
    """👑 Queen panel — this account's real limits, and how to use them.

    The numbers come from bot_ops.account_privileges(), the same single call the
    website dashboard uses, so chat cannot quote a limit the site does not
    enforce (that disagreement is what made "5/3 running slots" nonsense).
    """
    uid = _row_id(user)
    name = (user or {}).get("username") or (user or {}).get("name") or "there"
    try:
        priv = bot_ops.account_privileges(uid)
    except Exception:  # the panel is still useful if the privilege read fails
        priv = {}
    limit = priv.get("job_limit")
    try:
        running = bot_ops.active_count(uid)
    except Exception:
        running = None
    slots = f"{running}/{limit}" if running is not None and limit else (limit or "—")
    mem = priv.get("mem_limit_mb")
    mem_line = ("no ceiling — your apps are never killed for the memory they use"
                if not mem else f"{mem}MB per app")
    zip_mb = priv.get("zip_max_mb") or bot_ops.ZIP_MAX_MB
    zip_files = priv.get("zip_max_files") or bot_ops.ZIP_MAX_FILES
    # queen runner pin line — so a queen always sees where she is exclusive
    try:
        _pin = runner_client.get_queen_pin(uid) if uid else None
    except Exception:
        _pin = None
    if _pin and _pin.get("runner_url"):
        runner_line = f"🖥 Runner — `{_pin['runner_url']}` · exclusive 512MB (only you here)"
    else:
        runner_line = "🖥 Runner — auto (most free, 490MB+ when Fleet empty)"
    lines = [
        f"👑 *Queen panel* — {name}",
        "",
        "*Your limits*",
        f"🟢 Running apps — {slots}",
        f"🧠 Memory — {mem_line}",
        runner_line,
        f"🗜 Zip upload — up to {zip_mb}MB / {zip_files} files unzipped",
        "🌿 GitHub — any public repo, any branch",
        "",
        "*Run something*",
        "📦 `/projects` — your repo apps, the commit each is on, ⬆️ when the "
        "branch moved",
        "🌿 `/import owner/repo [name]` — any public repo; a branch with "
        "`/import owner/repo/tree/branch myapp`",
        "🗜 `/code name`, then send a `.zip` — a whole project, folders and "
        "`requirements.txt` included. `/update name` plus a zip replaces one.",
    ]
    if SITE_BASE:
        lines.append(
            f"⚠️ Telegram only lets a bot download 20MB. For a bigger zip use "
            f"{SITE_BASE}/bots — that upload doesn't go through Telegram, and "
            f"your {zip_mb}MB allowance applies there.")
    lines += [
        "",
        "*After it starts*",
        "`/apps` everything · `/status name` memory and live URL · "
        "`/logs name` output · `/restart name`",
        "A Telegram-bot project needs its own token: open the app → *Env* tab → "
        "paste `BOT_TOKEN` from @BotFather → *Save & restart*.",
        "Nothing is lost on a restart — your files and variables come back with it.",
        "",
        "*Keep it current*",
        "⬆️ `/latest name` — redeploy from the newest commit on its branch. In "
        "place: same address, same folder, so its database and sessions survive.",
        "⚙️ `/autodeploy name on` — I watch the branch and redeploy by myself "
        "when it moves (`off` stops it). `/projects` lists which apps are behind.",
    ]
    return "\n".join(lines)


def _queen_runner_text(user) -> str:
    """Runner picker for a queen — shows free MB, exclusive ownership, and how auto works."""
    uid = _row_id(user)
    pin = None
    try:
        pin = runner_client.get_queen_pin(uid)
    except Exception:
        pin = None
    pool = runner_client.runner_pool()
    # Use cached health — don't block the button for 18s probing all runners.
    try:
        health = runner_client.worker_health(refresh=False, max_age_s=20)
    except Exception:
        health = {}
    excl = {}
    try:
        excl = runner_client.list_exclusive_runners()  # url -> owner_id
    except Exception:
        excl = {}
    lines = ["🖥 *Queen runner* — pick where your next deploy lands", ""]
    if pin and pin.get("runner_url"):
        lines.append(f"📍 Pinned — `{pin['runner_url']}` · exclusive 512MB (only you here, no other user will be placed there).")
        lines.append("   Heavy / video projects get the whole box — upload & download stay on this runner.")
    else:
        lines.append("📍 Now — *auto* (most free runner). Next deploy picks the emptiest box, so 6 empty runners (490MB free) are used before a 180MB-loaded one.")
        lines.append("   Video-heavy uploads prefer the freest runner automatically.")
    lines.append("")
    lines.append(f"*Fleet* ({len(pool)} runners):")
    if not pool:
        lines.append("• Embedded mode — one runner inside this service.")
    else:
        # Show managed nodes with ids when possible
        nodes = {}
        try:
            for n in runner_client.managed_runner_nodes():
                nodes[n.get("url")] = n
        except Exception:
            nodes = {}
        for idx, url in enumerate(pool):
            h = health.get(url) or {}
            online = h.get("online")
            free_mb = int(h.get("free_mb") or 0)
            total_mb = int(h.get("total_mb") or 0)
            jobs = h.get("jobs", "?")
            # idle look: if health missing, still show url
            if online:
                status = f"🟢 {free_mb}MB free of {total_mb}MB · {jobs} job(s)"
            elif h == {}:
                status = "⚪ unknown (not probed yet — tap ↻)"
            else:
                status = "🔴 offline / waking"
            owner = excl.get(url.rstrip("/"))
            excl_note = ""
            if owner is not None:
                if int(owner) == int(uid):
                    excl_note = " · 👑 yours (exclusive)"
                else:
                    excl_note = f" · 🔒 queen #{owner} exclusive"
            label = nodes.get(url, {}).get("label") or f"runner-{idx+1}"
            pin_mark = " ← pinned" if pin and pin.get("runner_url","").rstrip("/") == url.rstrip("/") else ""
            lines.append(f"• `{label}` — `{url}`\n  {status}{excl_note}{pin_mark}")
    lines.append("")
    lines.append("Tap a runner to pin it *exclusive* (no other users there) — huge projects get 512MB alone.")
    lines.append("Tap *Auto* to clear pin and let the balancer pick the emptiest runner (recommended when fleet is mostly empty).")
    return "\n".join(lines)


def _queen_runner_kb(user) -> dict:
    uid = _row_id(user)
    pool = runner_client.runner_pool()
    rows = []
    # Auto first
    rows.append([{"text": "✨ Auto — most free", "callback_data": "queen:pin:auto"}])
    # managed nodes as buttons with id: for stable callback
    nodes = []
    try:
        nodes = runner_client.managed_runner_nodes()
    except Exception:
        nodes = []
    # Build map url -> id
    url_to_id = {n.get("url"): n.get("id") for n in nodes}
    for idx, url in enumerate(pool):
        nid = url_to_id.get(url)
        # label short
        label = None
        for n in nodes:
            if n.get("url") == url:
                label = n.get("label")
                break
        label = label or f"Runner {idx+1}"
        # truncate label for button
        blabel = (label[:28] + "…") if len(label) > 28 else label
        # Determine free display for button text
        try:
            h = runner_client.worker_health(refresh=False, max_age_s=20).get(url) or {}
            free_mb = int(h.get("free_mb") or 0)
            suffix = f" {free_mb}MB" if h.get("online") else " ?"
        except Exception:
            suffix = ""
        # check if exclusive other
        try:
            excl = runner_client.list_exclusive_runners()
            owner = excl.get(url.rstrip("/"))
            lock = " 🔒" if owner is not None and int(owner) != int(uid) else ""
            mine = " 👑" if owner is not None and int(owner) == int(uid) else ""
        except Exception:
            lock = ""; mine = ""
        text = f"🖥 {blabel}{suffix}{lock}{mine}"
        # callback uses id: if we have DB id else index
        cb = f"queen:pin:id:{nid}" if nid is not None else f"queen:pin:id:{idx}"
        rows.append([{"text": text[:60], "callback_data": cb}])
        # Telegram allows max 8 buttons per? We'll show all but limit rows to ~10 runners fine
    rows.append([{"text": "⬅️ Back to Queen panel", "callback_data": "queen:menu"}])
    return {"inline_keyboard": rows}


def cmd_queen_runner(chat_id, user):
    """`/runner` — queen picks where the next heavy deploy lands."""
    if not user or not _user_is_queen(user):
        _send(chat_id, "👑 *Runner* selection is queen access.\n\n"
                       "A queen can pin one runner exclusive 512MB (no other users there) — huge/video projects get the whole box.\n"
                       "Use `/limits` to see your allowances, or ask the bot owner for 👑.")
        return
    _send(chat_id, _queen_runner_text(user), reply_markup=_queen_runner_kb(user))


def cmd_limits(chat_id, user):
    """`/limits` — what this account is allowed. A 👑 gets the queen panel."""
    if _user_is_queen(user):
        _send(chat_id, _queen_panel_text(user), reply_markup=_queen_panel_kb())
        return
    uid = _row_id(user)
    priv = bot_ops.account_privileges(uid)
    running = bot_ops.active_count(uid)
    lines = [
        "*Your limits*",
        f"🟢 Running apps — {running}/{priv.get('job_limit')}",
        f"🧠 Memory — {priv.get('mem_limit_mb')}MB per app",
        f"🗜 Zip upload — {priv.get('zip_max_mb')}MB / {priv.get('zip_max_files')} files",
        "🌿 GitHub — `/import owner/repo` works for everyone",
        "",
        "Need more room? Ask the bot owner — they can raise limits for trusted accounts.",
    ]
    _send(chat_id, "\n".join(lines), reply_markup=_open_kb())


def _queen_help_text(user) -> str:
    """The 👑 /help screen — a different layout, not the same one with a footer.

    A queen used to get everybody else's help with a paragraph appended, which
    is not a separate interface: the thing only they can do was the last item on
    a long list. This leads with it, keeps the privileges in one block, and puts
    the commands everybody shares underneath, shortened.
    """
    name = (user or {}).get("username") or (user or {}).get("name") or "there"
    lines = [
        f"👑 *CodeNest — queen access*",
        f"Hi *{name}*. This is your interface: everything below is yours, and "
        f"the 👑 Queen panel button repeats it whenever you need it.",
        "",
        "*Run a project*",
        "1️⃣ `/import owner/repo [name]` — any public GitHub repo. Add a branch "
        "with `/import owner/repo/tree/branch myapp`. I scan it, show each "
        "runnable thing as a BUTTON, install its requirements and start it.",
        "2️⃣ Or `/code name` then send a `.zip` — whole folders, "
        "`requirements.txt`, data files. Bigger than Telegram's 20MB? Use the "
        "website upload; your queen zip allowance applies there.",
        "3️⃣ `/logs name` while it boots, `/status name` for memory and the "
        "live URL.",
        "4️⃣ If it's a Telegram bot, open the app → *Env* → paste its own "
        "`BOT_TOKEN` from @BotFather → *Save & restart*.",
        "5️⃣ Keep it current: ⬆️ or `/latest name` pulls the newest commit and "
        "redeploys in place — same address, same folder, so its database and "
        "sessions survive. ⚙️ or `/autodeploy name on` does that by itself.",
        "6️⃣ `/projects` — your repo apps at a glance, and which ones are behind.",
    ]
    lines.append(_queen_help_block())
    lines += [
        "",
        "*Everything else*",
        "`/limits` your allowances · `/apps` your apps · `/projects` the catalogue",
        "`/code name` then send source or a `.zip` · `/update name` to replace it",
        "`/import owner/repo [name]` any public repo · `/source name` download it back",
        "`/latest [name]` redeploy from the newest commit · `/autodeploy name on|off` "
        "👑 follow the branch by itself",
        "`/logs name` · `/status [name]` · `/restart name` · `/stop name` · "
        "`/delete name` · `/rename name newname`",
        "`/ping [url]` check a URL · `/cancel` abandon a pending upload · "
        "`/unlink` disconnect this chat",
        "",
        "I message you if an app stops on its own.",
    ]
    return "\n".join(lines)


def _plain_help_text(user) -> str:
    """The standard /help screen: what the bot can do for everybody.

    See the "CODE-VIA-CHAT" comment near the top of this file for how /code and
    /update are kept safe (account-gated, same rails as the website's editor).
    """
    return (
        f"👋 Hi *{user['username']}*!\n\n"
        "Tap *Open CodeNest* to write, edit and deploy — it opens right here "
        "in Telegram and signs you in automatically.\n\n"
        "*From chat you can also:*\n"
        "`/code myapp` — create an app, then send the source "
        "(text or a file)\n"
        "`/update name` — push new code to an existing app, then send it "
        "(auto-saves & restarts)\n"
        "`/import owner/repo [name]` — clone a public repo and deploy it\n"
        "`/latest [name]` — redeploy a repo app from its newest commit "
        "(same address, same data)\n"
        "`/source name` — download your app's current code as a file\n"
        "`/apps` — everything you have, with live status\n"
        "`/limits` — what your account is allowed\n"
        "`/status [name]` — account summary, or one app in full\n"
        "`/logs name` — the last lines it printed\n"
        "`/restart name`  `/stop name`  `/delete name`\n"
        "`/rename name newname`\n"
        "`/cancel` — stop a pending /code or /update\n"
        "`/projects` — your repo apps and whether a newer commit is waiting\n"
        "`/ping [url]` — check a URL (no URL = platform health)\n"
        "`/health` — is the platform alive, asked from inside\n"
        "`/id` — your Telegram id (long-press to copy)\n"
        "`/guide` — picture how-tos for every command\n"
        "`/token` — where BOT_TOKEN can live\n"
        "`/web` — open the site inside Telegram\n"
        "`/unlink` — disconnect this chat\n\n"
        "I message you if an app stops on its own — and I say *why* "
        "(memory, crash, host full), without taking other apps down."
    )


def _help_text(user):
    """Which /help screen this account gets: 👑 has its own, see above."""
    if _user_is_queen(user):
        return _queen_help_text(user)
    return _plain_help_text(user)


def cmd_commands(chat_id, user=None, telegram_user_id=None):
    """`/commands` — every command this chat can use right now."""
    user = user or telegram_link.user_for_chat(chat_id)
    lines = [
        "*Commands*",
        "",
        "Everyone:",
        "`/start` `/help` `/guide` `/id` `/ping` `/token`",
        "",
    ]
    if user:
        lines += [
            "Your apps:",
            "`/apps` `/status` `/logs name` `/restart name` `/stop name`",
            "`/code myapp` `/update myapp` `/import owner/repo`",
            "`/source name` `/rename old new` `/delete name` `/latest name`",
            "`/env name` `/backup name` `/history name`",
            "",
        ]
        if _user_is_queen(user):
            lines += [
                "👑 Queen:",
                "`/projects` `/autodeploy name on` · zip after `/code`",
                "",
            ]
        if _is_admin(user, telegram_user_id or (user or {}).get("telegram_id")):
            lines += [
                "🛠 Admin (also buttons under `/admin`):",
                "`/admin` `/runners` `/recover` `/health` `/user name`",
                "`/queen id` `/admin limit id 10` `/see id`",
                "",
            ]
    else:
        lines += ["Link first: `/link` or tap Open CodeNest.", ""]
    lines.append("Cartoon how-tos: `/guide` or `/guide code` · `/guide import` · …")
    _send(chat_id, "\n".join(lines), reply_markup=_main_kb(user))


def cmd_env(chat_id, user, arg=""):
    """`/env name` list keys · `/env name KEY=value` set · `/env name del KEY`."""
    parts = (arg or "").split(None, 1)
    if not parts:
        _send(chat_id, "Usage:\n`/env myapp` — list key names\n"
                       "`/env myapp BOT_TOKEN=123:AA…` — set\n"
                       "`/env myapp del BOT_TOKEN` — remove\n"
                       "Token may also live in the code (`/guide token`).")
        return
    name = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""
    row = bot_ops.find_app(user["id"], name)
    if not row:
        _send(chat_id, f"❌ No app called “{name}”. `/apps` lists yours.")
        return
    if not rest:
        env = bot_ops._row_env(row)
        keys = sorted(env.keys())
        tip = ""
        if row.get("telegram_bot_detected") and "BOT_TOKEN" not in env:
            tip = "\n\nNo BOT_TOKEN in Env — if it's in the source file, that's enough."
        _send(chat_id,
              f"🔑 *{row['name']}* env keys ({len(keys)}):\n"
              + ("`" + "`, `".join(keys) + "`" if keys else "_(none)_")
              + tip)
        return
    if rest.lower().startswith("del ") or rest.lower().startswith("delete "):
        key = rest.split(None, 1)[1].strip()
        res = bot_ops.set_env(user["id"], name, key, None)
    elif "=" in rest:
        key, _, val = rest.partition("=")
        res = bot_ops.set_env(user["id"], name, key.strip(), val)
    else:
        _send(chat_id, "Send `KEY=value` or `del KEY`.")
        return
    if not res.get("ok"):
        _send(chat_id, f"❌ {res['error']}")
        return
    act = "removed" if res.get("deleted") else "set"
    extra = " · restarted" if res.get("restarted") else " · applies on next start"
    _send(chat_id, f"✅ `{res['key']}` {act} on *{row['name']}*{extra}.")


def cmd_backup(chat_id, user, arg=""):
    """`/backup name` — snapshot workspace data into durable storage now."""
    name = (arg or "").strip()
    if not name:
        _send(chat_id, "Which app? `/backup myapp` — saves its data files "
                       "(database.db, sessions, …) so a runner rebuild can't wipe them.")
        return
    row = bot_ops.find_app(user["id"], name)
    if not row:
        _send(chat_id, f"❌ No app called “{name}”. `/apps` lists yours.")
        return
    rid = row.get("runner_job_id")
    if not rid:
        _send(chat_id, f"*{row['name']}* isn't on a runner right now. "
                       f"`/restart {row['name']}` first, then `/backup` again.")
        return
    try:
        from services import snapshots
        from services import bot_ops as bo
        out = snapshots.save_snapshot(row["id"], rid, worker=bo._worker_of(row))
    except Exception as exc:  # noqa: BLE001
        _send(chat_id, f"❌ Backup failed: {type(exc).__name__}: {exc}")
        return
    if out.get("saved"):
        _send(chat_id, f"💾 Snapshot saved for *{row['name']}*. "
                       "After a runner rebuild it will be restored automatically.")
    else:
        _send(chat_id, f"⚠️ Could not snapshot *{row['name']}*: "
                       f"{out.get('reason') or 'unknown'}. Try again when the runner is awake.")


def cmd_history(chat_id, user, arg=""):
    """`/history name` — recent code revisions (website deploys)."""
    name = (arg or "").strip()
    if not name:
        _send(chat_id, "Which app? `/history myapp`")
        return
    row = bot_ops.find_app(user["id"], name)
    if not row:
        _send(chat_id, f"❌ No app called “{name}”.")
        return
    from database import get_db_connection
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, version, action, status, created_at FROM bot_revisions "
            "WHERE job_id=? AND user_id=? ORDER BY version DESC LIMIT 12",
            (row["id"], user["id"]),
        ).fetchall()
        rows = [dict(r) for r in rows]
    except Exception:
        rows = []
    finally:
        conn.close()
    if not rows:
        _send(chat_id, f"*{row['name']}* has no saved revisions yet. "
                       "Website Save & Run creates them; chat `/update` does too.")
        return
    lines = [f"📜 *{row['name']}* revisions (newest first):"]
    for r in rows:
        lines.append(
            f"v{r.get('version')} · {r.get('action') or '—'} · "
            f"{r.get('status') or '—'} · {(r.get('created_at') or '')[:16]}"
        )
    lines.append("\nWebsite → bot details → Versions to roll back. "
                 "Admins: `/user` → job → Revisions.")
    _send(chat_id, "\n".join(lines))


def cmd_token_tips(chat_id):
    """`/token` — where BOT_TOKEN can live (Env or source)."""
    handle_callback(chat_id, "help:token", None)


def _cmd_health_smart(chat_id, telegram_user_id=None):
    """`/health` — full admin doctor for admins; public platform health for everyone else."""
    user = telegram_link.user_for_chat(chat_id)
    if _is_admin(user, telegram_user_id):
        _send(chat_id, _admin_health_text(), reply_markup=_admin_health_kb())
    else:
        cmd_health(chat_id, user or {})


# command name -> (guide png stem without guide_, short caption, markdown steps)
_COMMAND_GUIDES = {
    "start": ("guide_start", "Start here",
              "1. Tap *Open CodeNest* (or `/link`).\n"
              "2. Deploy with `/code`, a template, or `/import`.\n"
              "3. *Save & Run* — bots recover after runner restart."),
    "code": ("guide_code", "/code — new app",
             "`/code myapp` then paste code, send a file, or a `.zip`.\n"
             "I deploy it and give you Logs / Restart / Stop buttons."),
    "update": ("guide_update", "/update — replace code",
               "`/update myapp` then paste or upload the new source.\n"
               "Name stays; data files stay; only code changes."),
    "import": ("guide_import", "/import — GitHub",
               "`/import owner/repo [name]` — I show each runnable entry as a button.\n"
               "Branch: `/import owner/repo/tree/branch myapp`."),
    "apps": ("guide_apps", "/apps — your list",
             "`/apps` lists every app.\n"
             "Then `/logs name` · `/restart name` · `/stop name`."),
    "logs": ("guide_logs", "/logs — output",
             "`/logs myapp` — newest lines at the bottom."),
    "restart": ("guide_restart", "/restart — wake it",
                "`/restart myapp` brings a stopped or stuck app back."),
    "stop": ("guide_stop", "/stop — pause",
             "`/stop myapp` frees a running slot. Files stay yours."),
    "status": ("guide_status", "/status — health",
               "`/status` for the account · `/status myapp` for one app."),
    "source": ("guide_source", "/source — download",
               "`/source myapp` uploads the current code as a file."),
    "delete": ("guide_delete", "/delete — remove",
               "`/delete myapp` then confirm. Permanent."),
    "rename": ("guide_rename", "/rename — new label",
               "`/rename oldname newname` — data stays."),
    "latest": ("guide_latest", "/latest — pull commit",
               "`/latest myapp` for repo apps · bare `/latest` lists them."),
    "env": ("guide_env", "/env — variables",
            "`/env myapp` lists keys.\n"
            "`/env myapp KEY=value` sets · `del KEY` removes.\n"
            "Token may also live in the source file."),
    "backup": ("guide_backup", "/backup — snapshot",
               "`/backup myapp` saves data files so a rebuild cannot wipe them."),
    "history": ("guide_history", "/history — revisions",
                "`/history myapp` — recent deploys. Rollback on the website."),
    "id": ("guide_id", "/id — your numbers",
           "Long-press a number to copy. No broken markup."),
    "token": ("guide_token", "BOT_TOKEN tips",
              "Env tab *or* `BOT_TOKEN = '…'` in code — both work after restart."),
    "link": ("guide_link", "/link — connect",
             "Tap *Open CodeNest* or send `/link` so this chat can deploy."),
    "projects": ("guide_projects", "/projects — repo apps",
                 "`/projects` lists GitHub deploys · `/latest` pulls newest."),
}


def _show_command_guide(chat_id, topic: str, user=None):
    """Picture first, then short steps — only when the user asked (/guide).

    No home-menu keyboard: dumping the full help pad after every cartoon was
    spam. One optional "More guides" row is enough.
    """
    topic = (topic or "").strip().lower()
    info = _COMMAND_GUIDES.get(topic)
    if not info:
        _send(chat_id, "Unknown guide — try `/guide`.")
        return
    png, caption, body = info
    _send_guide(chat_id, png, caption)
    _send(chat_id, f"*{caption}*\n\n{body}",
          reply_markup={"inline_keyboard": [[
              {"text": "📖 More guides", "callback_data": "help:catalog"},
          ]]})


def _cmd_guide(chat_id, arg=""):
    """`/guide [code|import|…]` — cartoon first, then steps. No admin topics for public."""
    topic = (arg or "").strip().lower().split()[0] if (arg or "").strip() else ""
    alias = {
        "start": "start", "begin": "start", "home": "home", "help": "home",
        "import": "import", "repo": "import", "git": "import",
        "id": "id", "ids": "id",
        "token": "token", "bot_token": "token",
        "code": "code", "new": "code", "create": "code",
        "update": "update", "edit": "update",
        "apps": "apps", "list": "apps", "jobs": "apps",
        "logs": "logs", "log": "logs",
        "restart": "restart", "stop": "stop", "status": "status",
        "source": "source", "download": "source",
        "delete": "delete", "remove": "delete",
        "rename": "rename",
        "latest": "latest", "pull": "latest",
        "env": "env", "backup": "backup", "history": "history",
        "link": "link", "connect": "link",
        "projects": "projects", "project": "projects",
        # admin is NOT in the public alias map — only /admin path shows it
    }
    user = telegram_link.user_for_chat(chat_id)
    if not topic or topic in ("home", "help"):
        handle_callback(chat_id, "help:home", None)
        return
    ref = alias.get(topic)
    if ref is None:
        # Unknown word: show the public catalogue, never invent admin topics.
        lines = ["📖 *Guides* — send one of:",
                 "`/guide code` · `/guide update` · `/guide import`",
                 "`/guide apps` · `/guide logs` · `/guide restart` · `/guide stop`",
                 "`/guide status` · `/guide env` · `/guide token` · `/guide id`",
                 "`/guide backup` · `/guide history` · `/guide projects` · `/guide link`"]
        _send(chat_id, "\n".join(lines), reply_markup={"inline_keyboard": [[{"text": "📖 More guides", "callback_data": "help:catalog"}]]})
        return
    if ref in _COMMAND_GUIDES:
        _show_command_guide(chat_id, ref, user)
    else:
        handle_callback(chat_id, f"help:{ref}", None)


def handle_start(chat_id, first_name, payload=""):
    """/start, with or without a deep-link payload.

    Telegram delivers "t.me/<bot>?start=CODE" as the literal message
    "/start CODE" once the user taps START. Handling that payload is what
    turns the old nine-step flow — read a code, leave the site, find the bot,
    retype the code from memory — into two taps. The three steps a human could
    get wrong are exactly the three this removes.

    The payload is redeemed through the SAME redeem_code() the typed command
    uses. A shortcut that took a different path would be a second front door
    with its own rules to get wrong.
    """
    payload = (payload or "").strip()
    if payload:
        # Deliberately BEFORE the already-linked check: someone re-linking a
        # chat should hear that it is already connected, which handle_link
        # says, rather than have their tap silently ignored.
        handle_link(chat_id, f"/link {payload}", first_name)
        return

    user = telegram_link.user_for_chat(chat_id)
    if user:
        _send(chat_id, _help_text(user), reply_markup=_main_kb(user))
        return

    # UNLINKED: one button, and no instructions at all.
    #
    # There is nothing left to explain. Opening the Mini App verifies the same
    # Telegram identity and writes the same telegram_id the /link code used to
    # write — verified: user_for_chat() returns None before the first open and
    # the account straight after. So the button IS the connect step, and a
    # printed URL would only offer a worse route to the same place (a browser,
    # where the user would have to log in by hand).
    kb = _open_kb()
    if not kb:
        # A MESSAGE THAT SAYS "TAP BELOW" WITH NOTHING BELOW IT IS THE BUG,
        # NOT A COSMETIC ISSUE. _open_kb() returns None whenever the Mini App
        # URL is unusable, and the old code sent the invitation anyway — so
        # the very first thing a new user saw was an instruction pointing at
        # a button that was not there. Reproduced with SITE_BASE_URL unset.
        _send(chat_id,
              f"👋 Hi {first_name}!\n\n"
              "⚠️ CodeNest is not finished setting up: the owner still has to "
              "set `SITE_BASE_URL` to this site's public https address. "
              "Until then I cannot open the app for you.")
        return
    _send(chat_id,
          f"👋 Hi {first_name}!\n\n"
          "Tap below to open CodeNest — writing, editing and deploying all "
          "happen there, and you are signed in automatically.",
          reply_markup=kb)


def handle_unlink(chat_id):
    user = telegram_link.user_for_chat(chat_id)
    if not user:
        _send(chat_id, UNKNOWN_REPLY)
        return
    telegram_link.unlink(user["id"])
    _send(chat_id,
          "🔌 Disconnected. This chat can no longer deploy or see your apps.\n\n"
          "Your apps keep running — nothing was stopped.",
          reply_markup=_menu_buttons())


_link_attempts = defaultdict(list)
LINK_TRIES_PER_HOUR = int(os.getenv("TELEGRAM_LINK_TRIES_PER_HOUR", "8"))


def _link_rate_ok(chat_id):
    now = time.time()
    _link_attempts[chat_id] = [t for t in _link_attempts[chat_id] if now - t < 3600]
    if len(_link_attempts[chat_id]) >= LINK_TRIES_PER_HOUR:
        return False
    _link_attempts[chat_id].append(now)
    return True


# ==================== /ping ====================
PING_TIMEOUT_S = float(os.getenv("PING_TIMEOUT_S", "8"))
PING_MAX_REDIRECTS = int(os.getenv("PING_MAX_REDIRECTS", "3"))
PING_UA = "CodeNest-PingBot/1.0"

# Hosts that must never be fetched from a user-supplied /ping: this command is a
# server-side request, so without this list it doubles as a probe of the machine
# it runs on (and of a cloud provider's metadata endpoint). Same rule the
# standalone bot service applies — see bot/app.py's _PING_BLOCKED_HOSTS.
_PING_BLOCKED_HOSTS = ("localhost", "metadata", "metadata.google.internal",
                       "0.0.0.0", "ip6-localhost", "ip6-loopback")


def ping_default_target() -> str:
    """What a bare `/ping` measures: THIS site.

    It used to be a hardcoded foreign host. On any install that was not that
    one, `/ping` measured somebody else's server — and when it timed out the
    reply was a raw requests exception naming that server
    ("HTTPSConnectionPool(host='…', port=443): Read timed out."), which reads
    like the bot crashed. PING_DEFAULT_TARGET still overrides this for anyone
    who wants a fixed target; api.telegram.org is the last resort because it is
    the one service every install here actually depends on.
    """
    return (os.getenv("PING_DEFAULT_TARGET", "").strip() or SITE_BASE
            or "https://api.telegram.org")


def _ping_ip_blocked(value: str) -> bool:
    """True for an ADDRESS that must not be fetched.

    A hostname is not an address: it is judged by what it resolves to a few
    lines below, so "not an IP" means "not blocked here", not "blocked". (The
    opposite reading refused every /ping of a normal domain name.)
    """
    try:
        ip = ipaddress.ip_address(str(value).split("%")[0])
    except ValueError:
        return False
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified or str(ip).startswith("169.254."))


def _ping_host_allowed(host: str) -> tuple:
    """(ok, reason). Refuses internal names and addresses before any request."""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False, "there's no hostname in that URL"
    for blocked in _PING_BLOCKED_HOSTS:
        if host == blocked or host.endswith("." + blocked):
            return False, f"`{host}` is an internal address I'm not allowed to fetch"
    try:
        if _ping_ip_blocked(host):
            return False, f"`{host}` is in a private/internal IP range"
    except Exception:
        pass
    try:
        # No family/type here on purpose: getaddrinfo(host, None, SOCK_STREAM)
        # raises "ai_family not supported" on some resolvers, which would refuse
        # every domain name — a guard that blocks all pings is worse than none.
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        # A name with no record is a real answer. Anything else is the resolver
        # itself failing, and that is the request's problem to report (it does,
        # in words — see _ping_error_text), not a reason to refuse up front.
        nodata = getattr(socket, "EAI_NODATA", -5)
        if exc.errno in (socket.EAI_NONAME, nodata):
            return False, f"`{host}` doesn't resolve — no DNS record for that name"
        return True, ""
    except Exception:
        return True, ""            # resolver hiccup: let the request decide
    for info in infos:
        addr = info[4][0]
        if isinstance(addr, tuple):
            addr = addr[0]
        if _ping_ip_blocked(addr):
            return False, f"`{host}` resolves to an internal address ({addr})"
    return True, ""


def _ping_error_detail(exc: Exception) -> str:
    """The useful fragment of a requests exception, wrappers peeled off.

    "HTTPSConnectionPool(host='x', port=443): Max retries exceeded with url: /
    (Caused by SSLError(SSLZeroReturnError(6, '…')))" is four layers of library
    plumbing around one fact. This keeps the fact and drops the plumbing — that
    string is what made a two-second network hiccup look like a crash report.
    """
    text = str(exc).strip()
    caused = re.search(r"\(Caused by ([A-Za-z_.]+)\((.*)\)\)\s*$", text, re.S)
    if caused:
        text = f"{caused.group(1)}: {caused.group(2)}"
    text = re.sub(r"^(?:New ?|HTTPS?|HTTP)ConnectionPool\([^)]*\):\s*", "", text)
    text = re.sub(r"^Max retries exceeded with url:\s*\S+\s*", "", text)
    text = re.sub(r"^[A-Za-z_.]*(?:Error|Exception)\(\d+,\s*", "", text)
    text = re.sub(r"\(_ssl\.c:\d+\)", "", text)
    return (text.strip(" '\"") or type(exc).__name__)[:140]


def _ping_error_text(host: str, exc: Exception, seconds: float = None,
                     own_site: bool = False) -> str:
    """One short line a person can act on, instead of a requests traceback.

    `seconds` is the budget actually used (the own-site case gets a longer one),
    and `own_site` adds the one hint that matters when the address being
    measured is this deployment itself: a free-tier host can take a minute to
    wake, and 🩺 answers the same question without an HTTP round trip.
    """
    low = str(exc).lower()
    detail = _ping_error_detail(exc)
    wait = int(seconds if seconds is not None else PING_TIMEOUT_S)
    hint = ("\nA free-tier service can take up to a minute to wake up — try 🩺 "
            "*Health from inside* below, which doesn't depend on it answering "
            "HTTP at all." if own_site else "")
    if isinstance(exc, requests.Timeout) or "timed out" in low or "timeout" in low:
        return (f"🔴 *{host}* didn't answer within {wait}s.\n"
                f"It's down, still waking up, or too slow to reply.{hint}")
    if "name or service not known" in low or "failed to resolve" in low \
            or "nodename nor servname" in low or "getaddrinfo" in low:
        return f"🔴 *{host}* doesn't resolve — check the spelling of the address."
    if "connection refused" in low:
        return f"🔴 *{host}* refused the connection — nothing is listening there."
    if "certificate verify failed" in low or "self signed" in low or "self-signed" in low:
        return f"🔴 *{host}*'s TLS certificate isn't trusted from here: `{detail}`"
    if "ssl" in low or "certificate" in low or "tls" in low:
        return (f"🔴 *{host}* closed the secure connection before answering.\n"
                f"`{detail}`{hint}")
    if isinstance(exc, requests.TooManyRedirects) or "too many redirects" in low:
        return f"🔴 *{host}* keeps redirecting in a loop."
    if "connection aborted" in low or "connection reset" in low:
        return f"🔴 *{host}* dropped the connection part-way: `{detail}`{hint}"
    return f"🔴 Couldn't reach *{host}*: `{detail}`{hint}"


PING_SELF_TIMEOUT_S = float(os.getenv("PING_SELF_TIMEOUT_S", "25"))


def _ping_kb():
    """Buttons under a /ping result: measure again, or ask from the inside."""
    return {"inline_keyboard": [
        [{"text": "🔁 Measure again", "callback_data": "ping:again"},
         {"text": "🩺 Health from inside", "callback_data": "ping:health"}]]}


def handle_ping(chat_id, text):
    """`/ping [url]` — how long a URL takes to answer, in plain words.

    With no argument it measures THIS site, because that is the question being
    asked ("is my hosting alive?"). Two consequences follow, and both used to
    read as "/ping is broken":

    • A free-tier service that has been asleep takes tens of seconds to answer
      its FIRST request. Judging that with the 8s budget meant for an arbitrary
      URL produced "🔴 didn't answer" about a site that was answering — slowly,
      because the ping itself had just woken it. The own-site case gets a longer
      budget, and a slow answer is reported as a cold start, not as death.
    • Nobody could tell what had been measured, so a red result looked like the
      bot failing rather than a verdict about a named address. The reply now
      names its target and offers 🩺 — the same question asked from inside the
      process, where no HTTP round trip can confuse the answer.
    """
    parts = text.split()
    given = parts[1].strip() if len(parts) > 1 else ""
    target = given or ping_default_target()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", target):
        target = "https://" + target          # "/ping example.com" should work
    parsed = urlparse(target)
    if parsed.scheme not in ("http", "https"):
        _send(chat_id, "🔴 I can only ping `http://` and `https://` addresses.")
        return
    if parsed.username or parsed.password:
        _send(chat_id, "🔴 Please don't put a username or password in a pinged URL.")
        return
    host = parsed.hostname or target
    ok, why = _ping_host_allowed(host)
    if not ok:
        _send(chat_id, f"🔴 I can't ping that: {why}")
        return

    budget = PING_TIMEOUT_S if given else max(PING_TIMEOUT_S, PING_SELF_TIMEOUT_S)
    started = time.time()
    current, method, hops = target, "HEAD", 0
    try:
        while True:
            # Redirects are followed by hand so every hop is re-checked: a public
            # URL that 302s to an internal address would otherwise smuggle the
            # request past the guard above.
            resp = requests.request(method, current, timeout=budget,
                                    allow_redirects=False,
                                    headers={"User-Agent": PING_UA})
            if resp.status_code in (301, 302, 303, 307, 308) and hops < PING_MAX_REDIRECTS:
                location = resp.headers.get("Location") or ""
                if not location:
                    break
                nxt = urljoin(current, location)
                next_host = urlparse(nxt).hostname or ""
                ok, why = _ping_host_allowed(next_host)
                if not ok:
                    _send(chat_id, f"🔴 *{host}* redirected somewhere I can't follow: {why}")
                    return
                current, hops = nxt, hops + 1
                if urlparse(nxt).scheme not in ("http", "https"):
                    break
                continue
            # Some hosts answer HEAD with 403/405 but serve GET fine — measuring
            # that as "the site is broken" would be wrong, so try GET once.
            if method == "HEAD" and resp.status_code in (403, 405, 501):
                method = "GET"
                continue
            break
    except Exception as exc:                                   # noqa: BLE001
        if not given:
            # Own platform: no hostname in the reply.
            msg = _ping_error_text("the platform", exc, budget, own_site=True)
            msg = msg.replace("`the platform`", "the platform")
            _send(chat_id, msg, reply_markup=_ping_kb())
        else:
            _send(chat_id, _ping_error_text(host, exc, budget, own_site=False),
                  reply_markup=_ping_kb())
        return

    ms = round((time.time() - started) * 1000, 1)
    code = resp.status_code
    final_host = urlparse(current).hostname or host
    icon = "🟢" if code < 400 else ("🟡" if code < 500 else "🔴")
    lines = []
    # Bare /ping measures THIS platform. Never print the hostname — a normal
    # user does not need the deploy URL, and it is not a public brand name.
    if not given:
        lines.append(f"{icon} Platform is reachable — {ms}ms · HTTP {code}")
        lines.append("`/ping example.com` checks any other URL.")
    else:
        lines.append(f"{icon} *{host}* — {ms}ms · HTTP {code}")
        if final_host != host:
            lines.append(f"↳ landed on `{final_host}` after {hops} redirect(s)")
    if code in (401, 403):
        lines.append("It answered, but refused the request — normal for a page "
                     "behind a login.")
    elif code == 404 and given:
        lines.append("It answered, but that path doesn't exist.")
    elif code >= 500:
        lines.append("It answered with a server error.")
    elif ms > 5000:
        lines.append("⏳ Cold start — the service had been asleep. Next ping is fast.")
    _send(chat_id, "\n".join(lines), reply_markup=_ping_kb())


def cmd_id(chat_id, msg_from=None):
    """`/id` — numbers only, each in HTML <code> so Telegram copies on tap.

    No how-to cartoon, no home menu, no second "Open the site" bubble.
    One message: tap a grey code chip → copy that id alone.
    """
    frm = msg_from or {}
    tg_id = frm.get("id") or chat_id
    username = (frm.get("username") or "").strip()
    user = telegram_link.user_for_chat(chat_id)
    account_id = (user or {}).get("id")

    def esc(x):
        return (str(x).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))

    parts = [
        "<b>Your ids</b> — tap a grey number to copy",
        "",
        f"Telegram id:  <code>{esc(tg_id)}</code>",
        f"This chat:    <code>{esc(chat_id)}</code>"
        + ("  <i>(same — private chat)</i>" if chat_id == tg_id else
           "  <i>(group)</i>"),
    ]
    if username:
        parts.append(f"Username:     @{esc(username)}")
    if user:
        flags = []
        if _user_is_queen(user):
            flags.append("queen")
        if user.get("is_admin"):
            flags.append("admin")
        flag_s = (" · " + ", ".join(flags)) if flags else ""
        uname = user.get("username") or ""
        parts.append(
            f"CodeNest #:   <code>{esc(user['id'])}</code>"
            + (f"  ({esc(uname)})" if uname else "")
            + flag_s
        )
    else:
        parts.append("CodeNest #:   not linked — send /link")

    tg_uid = frm.get("id") or chat_id
    if _is_admin(user, tg_uid):
        sample = account_id or tg_id
        parts += [
            "",
            "<b>Admin</b> (you only) — tap a line to copy:",
            f"<code>/queen {esc(sample)}</code>",
            f"<code>/admin limit {esc(sample)} 10</code>",
            f"<code>/see {esc(sample)}</code>",
            f"<code>/user {esc(sample)}</code>",
        ]
    _send_html(chat_id, "\n".join(parts))




def cmd_web(chat_id, user=None):
    """`/web` — open the Telegram Web App: the whole site, inside Telegram.

    The Mini App is the front door. It verifies the same Telegram identity the
    bot uses, so there is no password to remember and no browser to leave — and
    it is the only way to upload a project bigger than Telegram's own 20MB file
    limit, which is why it has a command of its own rather than living only
    inside /start.
    """
    kb = _open_kb()
    if not kb:
        _send(chat_id, "⚠️ I can't offer the web app yet: this server has no "
                       "public URL of its own (`SITE_BASE_URL`). Until the owner "
                       "sets it, a button would open somebody else's site.")
        return
    user = user or telegram_link.user_for_chat(chat_id)
    name = (user or {}).get("username") or "there"
    body = [f"🌐 *CodeNest web* — hi {name}", "",
            "Tap below: it opens inside Telegram and signs you in automatically, "
            "no password."]
    if SITE_BASE:
        body.append(f"Direct link: {SITE_BASE}")
    body += ["", "What's in there:",
             "• Write, edit and deploy code, file by file",
             "• *Env* — a Telegram bot's own `BOT_TOKEN` goes here",
             "• Upload a `.zip` (bigger than Telegram's 20MB limit)",
             "• Files, logs, revisions, and each app's live URL"]
    _send(chat_id, "\n".join(body), reply_markup=kb)


def cmd_health(chat_id, user):
    """🩺 `/health` — is the platform alive, asked from the INSIDE.

    `/ping` sends a real HTTP request out of the container and back in. On a
    free-tier host that trip can be slow, cold-started, or refused by the edge —
    and then "🔴 didn't answer" reads as "my hosting is broken" when nothing is.
    This asks the same question without leaving the process: can the database be
    read, which runners answer, how many jobs are alive, and which URL the Mini
    App button opens. An admin gets the full 🩺 panel instead.
    """
    if _is_admin(user, chat_id):
        _send(chat_id, _admin_health_text(), reply_markup=_admin_health_kb())
        return

    lines = ["🩺 *Platform health*"]
    try:
        from database import DIALECT, get_db_connection
        conn = get_db_connection()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        lines.append(f"🟢 Database — reachable ({DIALECT})")
    except Exception as exc:                                   # noqa: BLE001
        lines.append(f"🔴 Database — {type(exc).__name__}: {str(exc)[:80]}")

    pool = runner_client.runner_pool()
    try:
        health = runner_client.worker_health(refresh=False, max_age_s=20) if pool else {}
        online = [u for u, h in health.items() if h.get("online")]
        if not pool:
            lines.append("🟢 Runner — embedded in this service")
        elif online:
            free = sum(int(h.get("free") or 0) for h in health.values())
            mb = sum(int(h.get("free_mb") or 0) for h in health.values())
            lines.append(f"🟢 Runner — {len(online)}/{len(pool)} answering · "
                         f"{free} free slot(s) · {mb}MB free")
        else:
            lines.append(f"🔴 Runner — none of the {len(pool)} runner(s) answered "
                         f"/health (asleep or restarting)")
    except Exception as exc:                                   # noqa: BLE001
        lines.append(f"⚠️ Runner — couldn't be checked ({type(exc).__name__})")

    try:
        running = bot_ops.active_count(_row_id(user))
        priv = bot_ops.account_privileges(_row_id(user))
        lines.append(f"🟢 Your apps — {running} running of {priv.get('job_limit')} allowed")
    except Exception:                                          # noqa: BLE001
        pass

    hook = (_tg("getWebhookInfo") or {}).get("result") or {}
    lines.append("🔔 Telegram — " + ("webhook (fast, wakes itself)"
                                     if str(hook.get("url") or "") else
                                     "long-polling (slower to wake on a free host)"))
    if SITE_BASE:
        lines.append("🌐 Mini App button — ready")
    else:
        lines.append("⚠️ Mini App button — site URL not configured yet")
    _send(chat_id, "\n".join(lines))



# ==================== APP BUTTONS ====================
def _app_buttons(job_id, url="", bot_username="", repo=False, queen=False):
    """Buttons keyed on the SITE job id, not the runner id.

    The runner id changes when a job is recreated, so buttons attached to an
    old message silently stopped working. The site id is stable for the life
    of the app, and it is also what scopes every action to its owner.
    """
    rows = [
        [{"text": "📜 Logs", "callback_data": f"logs:{job_id}"},
         {"text": "📊 Status", "callback_data": f"stat:{job_id}"}],
        [{"text": "🔄 Restart", "callback_data": f"restart:{job_id}"},
         {"text": "⏹ Stop", "callback_data": f"stop:{job_id}"}],
        [{"text": "📥 Download data", "callback_data": f"db:{job_id}"}],
    ]
    # An app that came from a repo can be brought up to date in one tap — the
    # same idea as a platform auto-deploy, and the reason nobody has to remember
    # which commit is running.
    if repo:
        rows.append([{"text": "⬆️ Deploy latest commit", "callback_data": f"latest:{job_id}"},
                     {"text": "🔀 Repo / branch", "callback_data": f"latestsrc:{job_id}"}])
        if queen:
            rows.append([{"text": "⚙️ Auto-deploy: on/off",
                          "callback_data": f"autodep:{job_id}"}])
    # The whole point of this platform is deploying a Telegram bot — so the
    # single most relevant thing to do right after a deploy is open THAT
    # bot and talk to it. This button was missing entirely; every other
    # action here manages the deployment, none of them let you use it.
    if bot_username:
        rows.append([{"text": "🤖 Open your bot", "url": f"https://t.me/{bot_username.lstrip('@')}"}])
    if url:
        rows.append([{"text": "🌐 Open live URL", "url": url}])
    btn = _open_button("🚀 Open in CodeNest")
    if btn:
        rows.append([btn])
    return {"inline_keyboard": rows}


# Kept under its old name: the reply_markup regression test drives it, and
# that regression (a nested dict urlencoded into "reply_markup=inline_keyboard"
# so every button vanished) is still worth guarding.
def get_job_buttons(runner_id, url):
    return _app_buttons(runner_id, url)


# ==================== APP COMMANDS ====================
def _fmt_uptime(sec):
    sec = int(sec or 0)
    if sec <= 0:
        return "—"
    d, h, m = sec // 86400, (sec % 86400) // 3600, (sec % 3600) // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m {sec % 60}s"


_ICON = {"running": "🟢", "crashed": "🔴", "installing": "🟡",
         "starting": "🟡", "restarting": "🟡", "recovering": "🟡", "stopped": "⚪", "offline": "⚪"}


def _user_is_queen(user) -> bool:
    """👑 flag for a linked account.

    telegram_link.user_for_chat already selects it (mem_unlimited AS is_queen),
    so this is normally free; the database fallback covers callers that built
    the user dict somewhere else (the admin panel, tests).
    """
    if not user:
        return False
    if "is_queen" in user:
        return bool(user.get("is_queen"))
    try:
        return bot_ops.is_queen(user.get("id"))
    except Exception:
        return False



def _send_photo_file(chat_id, filepath, caption=""):
    """Inline photo (not document download). Quiet on failure."""
    if not TG_API or not filepath:
        return {}
    try:
        import os as _os
        with open(filepath, "rb") as fh:
            r = requests.post(
                f"{TG_API}/sendPhoto",
                data={"chat_id": chat_id, "caption": (caption or "")[:1024]},
                files={"photo": (_os.path.basename(str(filepath)), fh, "image/png")},
                timeout=60,
            )
        return r.json() if r is not None else {}
    except Exception as e:  # noqa: BLE001
        logger.warning("sendPhoto failed: %s", e)
        return {}


def _render_apps_card(apps, running=0, limit=0, mem_mb=0):
    """One PNG dashboard: total / running / memory + per-app rows.

    This is the answer to /apps — a live snapshot, not a how-to cartoon.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    from pathlib import Path as _P
    import tempfile

    W, H = 720, min(900, 160 + max(len(apps), 1) * 44 + 40)
    BG, CARD, TEXT = (15, 23, 42), (30, 41, 59), (226, 232, 240)
    MUTED, OK, OFF, WARN = (148, 163, 184), (34, 197, 94), (100, 116, 139), (251, 191, 36)
    ACCENT = (99, 102, 241)

    def font(sz, bold=False):
        for c in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ):
            if _P(c).exists():
                return ImageFont.truetype(c, sz)
        return ImageFont.load_default()

    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    # header
    d.rounded_rectangle((16, 16, W - 16, 120), 16, fill=CARD)
    d.text((32, 28), "Your apps", fill=TEXT, font=font(26, True))
    d.text((32, 68), f"{len(apps)} total", fill=MUTED, font=font(16))
    # big stats
    stats = [
        (220, "Running", f"{running}/{limit or '—'}", OK if running else OFF),
        (400, "Memory", f"{round(mem_mb)} MB", ACCENT),
        (560, "Live", str(sum(1 for a in apps if a.get("status") == "running")), OK),
    ]
    for x, label, val, col in stats:
        d.text((x, 32), label, fill=MUTED, font=font(13))
        d.text((x, 54), val, fill=col, font=font(22, True))

    # rows
    y = 140
    for a in apps[:14]:
        st = (a.get("status") or "offline").lower()
        col = OK if st == "running" else (WARN if st in ("starting", "installing", "restarting", "recovering") else OFF)
        d.rounded_rectangle((16, y, W - 16, y + 40), 10, fill=CARD)
        d.ellipse((28, y + 12, 44, y + 28), fill=col)
        name = str(a.get("name") or "?")[:28]
        d.text((56, y + 10), name, fill=TEXT, font=font(16, True))
        right = f"{st}"
        if a.get("mem_mb"):
            right += f"  ·  {round(a['mem_mb'])}MB"
        if a.get("uptime_s"):
            right += f"  ·  {_fmt_uptime(a['uptime_s'])}"
        # right-align-ish
        bbox = d.textbbox((0, 0), right, font=font(13))
        tw = bbox[2] - bbox[0]
        d.text((W - 32 - tw, y + 12), right, fill=MUTED, font=font(13))
        y += 44
    if len(apps) > 14:
        d.text((32, y + 4), f"+{len(apps) - 14} more — scroll the list below", fill=MUTED, font=font(13))

    out = _P(tempfile.gettempdir()) / f"codenest_apps_{os.getpid()}.png"
    im.save(out, "PNG", optimize=True)
    return str(out)


def cmd_apps(chat_id, user):
    """Live fleet card + list. Not a how-to — the user asked what they own."""
    apps = bot_ops.list_apps(user["id"])
    queen = _user_is_queen(user)
    if not apps:
        _send(chat_id,
              "No apps yet.\n"
              "`/code myapp` · `/import owner/repo` · `/guide code`")
        return

    running = bot_ops.active_count(user["id"])
    limit = bot_ops.effective_job_limit(user["id"])
    total = len(apps)
    mem = sum(float(a.get("mem_mb") or 0) for a in apps)

    # One picture that IS the answer: counts + bars — not a tutorial.
    try:
        path = _render_apps_card(apps, running=running, limit=limit, mem_mb=mem)
        if path:
            _send_photo_file(chat_id, path,
                             caption=f"Your apps · {total} total · {running}/{limit} running")
    except Exception:
        logger.exception("apps card failed")

    # Compact text + per-app buttons (logs/status/restart) — no how-to footer.
    crown = " 👑" if queen else ""
    lines = [f"*{total} total* · *{running}/{limit} running*{crown}"]
    for a in apps[:20]:
        icon = _ICON.get(a["status"], "⚪")
        bits = [f"{icon} *{a['name']}* — `{a['status']}`"]
        if a.get("mem_mb"):
            bits.append(f"{round(a['mem_mb'])}MB")
        if a.get("uptime_s"):
            bits.append(_fmt_uptime(a["uptime_s"]))
        lines.append(" · ".join(bits))
    if total > 20:
        lines.append(f"… +{total - 20} more")
    if queen:
        lines.append("👑 queen · no memory ceiling")
    if running >= limit:
        lines.append(f"⚠️ {limit}/{limit} slots full — `/stop name` frees one")

    # Buttons: each app gets Logs | Status | Restart (max ~8 apps to stay under TG limit)
    rows = []
    for a in apps[:8]:
        jid = a.get("id")
        if jid is None:
            continue
        name = (a.get("name") or "?")[:16]
        rows.append([
            {"text": f"📜 {name}", "callback_data": f"logs:{jid}"},
            {"text": "📊", "callback_data": f"stat:{jid}"},
            {"text": "🔄", "callback_data": f"restart:{jid}"},
            {"text": "⏹", "callback_data": f"stop:{jid}"},
        ])
    kb = {"inline_keyboard": rows} if rows else None
    _send(chat_id, "\n".join(lines) if lines else "—", reply_markup=kb)




def cmd_status(chat_id, user, ref=""):
    """Whole-account summary, or one app in full."""
    if ref:
        res = bot_ops.logs(user["id"], ref, lines=0)
        if not res.get("ok"):
            _send(chat_id, f"❌ {res['error']}")
            return
        job, info = res["job"], res["info"]
        icon = _ICON.get(info.get("status"), "⚪")
        txt = [f"{icon} *{job['name']}*",
               f"Status: `{info.get('status', 'unknown')}`",
               f"Language: `{job.get('language') or '—'}`",
               f"Memory: {round(info.get('mem_mb') or 0)}MB now · "
               f"{round(info.get('peak_mem_mb') or 0)}MB peak",
               f"Uptime: {_fmt_uptime(info.get('uptime_s'))}",
               f"Restarts: {info.get('restarts', 0)}"]
        if info.get("last_exit_reason"):
            reason = info["last_exit_reason"]
            human = {
                "oom": "went over its memory limit (other apps kept running)",
                "limit": "stopped to protect the runner — host was full",
                "crash": "exited with an error",
                "crash_loop": "crash-looped and was stopped for good",
                "isolation": "stopped to protect every other app on the runner",
                "manual": "stopped on purpose",
                "exit": "finished cleanly",
                "workspace missing": "its files were gone on the runner",
            }.get(reason, reason)
            txt.append(f"Last exit: {human}")
            if info.get("last_exit_code") not in (None, ""):
                txt.append(f"Exit code: `{info['last_exit_code']}`")
        if info.get("libs"):
            txt.append(f"Packages: `{', '.join(info['libs'])}`")
        if info.get("env_keys"):
            # KEY NAMES ONLY — the values are bot tokens.
            txt.append(f"Env keys: `{', '.join(info['env_keys'])}`")
        elif job.get("telegram_bot_detected"):
            # Env empty is fine when the token lives in the file.
            txt.append("Token: check source or Env — either place is enough "
                       "(`/guide token`).")
        _send(chat_id, "\n".join(txt),
              reply_markup=_app_buttons(job["id"], bot_username=job.get("telegram_bot_username") or ""))
        return

    apps = bot_ops.list_apps(user["id"])
    running = [a for a in apps if a["status"] == "running"]
    mem = sum(a.get("mem_mb") or 0 for a in apps)
    limit = bot_ops.effective_job_limit(user["id"])
    crown = " 👑" if _user_is_queen(user) else ""
    _send(chat_id,
          f"*{user.get('username') or 'you'}*{crown}\n\n"
          f"Apps: {len(apps)} · running {len(running)}/{limit}\n"
          f"Memory in use: {round(mem)}MB\n\n"
          "`/apps` for the live card · `/status name` for one app")


def cmd_logs(chat_id, user, ref):
    if not ref:
        _send(chat_id, "Which app? `/logs name` — /apps lists them.")
        return
    res = bot_ops.logs(user["id"], ref)
    if not res.get("ok"):
        _send(chat_id, f"❌ {res['error']}")
        return
    body = res["logs"] or "(no output yet)"
    # Telegram rejects a message over ~4096 chars; trim from the FRONT so the
    # most recent lines — the ones that explain a crash — always survive.
    if len(body) > 3500:
        body = "…\n" + body[-3500:]
    head = "📜 last lines" + (" (trimmed)" if res.get("truncated") else "")
    _send(chat_id, f"*{res['job']['name']}* — {head}\n```\n{body}\n```",
          reply_markup=_app_buttons(res["job"]["id"], bot_username=res["job"].get("telegram_bot_username") or ""))


def cmd_restart(chat_id, user, ref):
    if not ref:
        _send(chat_id, "Which app? `/restart name` — or open `/apps`.")
        return
    res = bot_ops.restart(user["id"], ref)
    if not res.get("ok"):
        _send(chat_id, f"❌ {res['error']}")
        return
    job = res["job"]
    _send(chat_id, f"🔄 Restarting *{job['name']}*…",
          reply_markup=_app_buttons(job["id"],
                                    bot_username=job.get("telegram_bot_username") or ""))


def cmd_stop(chat_id, user, ref):
    if not ref:
        _send(chat_id, "Which app? `/stop name` — or open `/apps`.")
        return
    res = bot_ops.stop(user["id"], ref)
    if not res.get("ok"):
        _send(chat_id, f"❌ {res['error']}")
        return
    job = res["job"]
    _send(chat_id, f"⏹ Stopped *{job['name']}*.",
          reply_markup=_app_buttons(job["id"],
                                    bot_username=job.get("telegram_bot_username") or ""))


def cmd_source(chat_id, user, ref):
    """/source <name> — sends the bot's current code back as a file, so
    it can be edited locally and pushed back with /update. Owner-scoped
    like every other user command (find_app checks user_id) — this is NOT
    the admin /see tool, it only ever returns the caller's own code."""
    if not ref:
        _send(chat_id, "Usage: `/source name`")
        return
    app = bot_ops.find_app(user["id"], ref)
    if not app:
        _send(chat_id, f"No app called “{ref}”. /apps lists yours.")
        return
    code = app.get("code") or ""
    if not code.strip():
        _send(chat_id, f"*{app['name']}* has no inline source stored — it was likely "
                       f"deployed via GitHub import or a .zip bundle, so there's no "
                       f"single file to send. Check the repo/zip you uploaded it from.")
        return
    ext = {"python": "py", "node": "js", "bash": "sh", "ruby": "rb", "php": "php"}.get(app["language"], "txt")
    with tempfile.NamedTemporaryFile(mode="w", suffix=f"_{app['name']}.{ext}",
                                      delete=False, encoding="utf-8") as f:
        f.write(code)
        tmp_path = f.name
    try:
        _send_document(chat_id, tmp_path,
                       caption=f"{app['name']} — edit this, then /update {app['name']} to push it back.")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def cmd_delete(chat_id, user, ref):
    if not ref:
        _send(chat_id, "Which app? `/delete name` — this cannot be undone.")
        return
    app = bot_ops.find_app(user["id"], ref)
    if not app:
        _send(chat_id, f"❌ No app called “{ref}”.")
        return
    _send(chat_id, f"Delete *{app['name']}*? This cannot be undone.",
          {"inline_keyboard": [
              [{"text": "🗑 Yes, delete", "callback_data": f"delconfirm:{app['id']}"},
               {"text": "✖️ Cancel", "callback_data": f"delcancel:{app['id']}"}],
          ]})


def _cmd_delete_confirmed(chat_id, user, ref):
    res = bot_ops.delete(user["id"], ref)
    _send(chat_id, f"🗑 Deleted *{res['job']['name']}*." if res.get("ok")
          else f"❌ {res['error']}")


def cmd_rename(chat_id, user, args):
    parts = (args or "").split()
    if len(parts) < 2:
        _send(chat_id, "Usage: `/rename x x`")
        return
    res = bot_ops.rename(user["id"], parts[0], " ".join(parts[1:]))
    _send(chat_id, f"✏️ *{res['old']}* is now *{res['name']}*." if res.get("ok")
          else f"❌ {res['error']}")


# ==================== /code AND /update — see the module comment above ====

def _repo_branch_of(url: str) -> str:
    """The branch a GitHub URL asks for, or "" for the repo's default.

    Only used for what the user is TOLD (the runner does its own parsing in
    _repo_clone_target), so a mismatch here would be a wrong label, never a
    wrong deploy.
    """
    text = (url or "").strip()
    # The whole remainder, slashes included: a branch may be "arena/01a0ba14-b",
    # and the runner works out how much of it is the branch when it clones.
    m = re.search(r"github\.com/[^/\s]+/[^/\s]+?(?:\.git)?/(?:tree|blob|commits?)/([^#\s?]+)", text)
    if m:
        return m.group(1).strip("/")
    if "#" in text:
        return text.rsplit("#", 1)[1].strip()
    return ""


def cmd_import(chat_id, user, arg):
    """/import <github url> [name] — clone a public GitHub repo and deploy it.

    With no name, nothing has been decided yet, so the repo is scanned and what
    comes back is a button per runnable thing in it (offer_repo_choices) — a repo
    is rarely exactly one project, and asking for a name in a sentence full of
    placeholders is a form to fill in by hand. With a name, the person has said
    what they want: it deploys straight away and the scan only decides WHICH file
    runs and which dependency files belong to it.

    A branch can be part of the URL — `owner/repo/tree/<branch>` (what the
    browser shows) or `owner/repo#<branch>` — and the runner clones exactly that
    branch. Without it a repo whose work lives off `main` deploys the wrong code.
    """
    if not arg:
        ex_owner, ex_repo = _queen_repo_parts()
        example = f"github.com/{ex_owner}/{ex_repo}" if ex_owner else "github.com/user/repo"
        _send(chat_id, "Send me a repo and I'll show what can run in it:\n"
                       f"`/import {example}`   ← paste your own instead\n\n"
                       "Then tap the project you want — no name to invent, no "
                       "filename to guess.\n"
                       "• A branch: paste the address from your browser, "
                       "`/import " + example + "/tree/dev`\n"
                       "• A name of your own: add it at the end, "
                       "`/import " + example + " myapp`\n"
                       "Public repos only"
                       + (" — and 👑 `/projects` lists the ready-made ones."
                          if _user_is_queen(user) else "."))
        return
    parts = arg.split(None, 1)
    url = parts[0]
    name = parts[1].strip() if len(parts) > 1 else ""
    m = re.search(r"github\.com/([^/\s]+)/([^/\s]+)", url)
    if not m:
        _send(chat_id, "That doesn't look like a github.com repo URL — "
                       "expected something like `github.com/user/repo`.")
        return
    branch = _repo_branch_of(url)

    if not name:
        with _working(chat_id):
            if offer_repo_choices(chat_id, user, url):
                return
        # Nothing to choose between (one entry, or GitHub wouldn't answer):
        # deploy the repo itself, and let the runner detect the entry.
        name = m.group(2).replace(".git", "")

    clean = bot_ops.slugify_name(name)
    if not clean:
        _send(chat_id, "That name has no usable characters — letters, numbers, "
                       "spaces, `-` and `_` only.")
        return
    if bot_ops.find_app(user["id"], clean):
        _send(chat_id, f"You already have an app called “{clean}”. Pick a "
                       f"different name: `/import {url} name`.")
        return

    # Which file runs, and which dependency files belong to it. Exactly one
    # project in the repo means no ambiguity; more than one means the picker
    # above was the right answer, so the runner's own detection decides.
    entry, deps = "", None
    owner, repo_name, parsed_branch = github_repo.parse_repo(url)
    if owner:
        projects = github_repo.scan_projects(owner, repo_name, branch or parsed_branch)
        if len(projects) == 1:
            entry = projects[0]["entry"]
            deps = projects[0]["manifests"]

    with _progress(chat_id, "import", name=clean) as prog:
        prog.at(10, "Reading the GitHub link")
        prog.at(28, "Cloning" + (f" branch `{branch}`" if branch else " the repo"))
        if entry:
            prog.at(42, f"Will run `{entry}`")
        prog.at(58, "Installing requirements")
        res = bot_ops.create_app_from_repo(user["id"], clean, url, entry=entry, deps=deps)
        if not res.get("ok"):
            prog.fail(res.get("error") or "import failed")
            _send(chat_id, f"❌ {res['error']}")
            return
        prog.at(85, "Starting the app")
        prog.done("Deployed.")
    _send_deployed(chat_id, user, res, branch=branch, repo=True)


# ==================== CHOOSING WHAT TO RUN ====================
# A repo is rarely one thing. The project repo this install ships with holds a
# Telegram bot at the root (bot.py + requirements.txt) AND a web dashboard in
# web/ (dashboard.py + requirements-web.txt). Telling someone to type
# "/import owner/repo/tree/<branch> <name>" hands them a form with blanks in it —
# and the brackets read as something the bot failed to fill in. So the repo is
# scanned first and what comes back is one button per runnable thing in it.

_PICK_TTL_S = int(os.getenv("PROJECT_PICK_TTL_S", "900"))
_picks: dict = {}
_picks_lock = threading.Lock()


def _remember_picks(chat_id, state: dict) -> None:
    """Keep the scan result here; the button carries only an index."""
    now = time.time()
    with _picks_lock:
        for cid in [c for c, v in _picks.items() if now - v.get("at", 0) > _PICK_TTL_S]:
            _picks.pop(cid, None)
        _picks[chat_id] = dict(state, at=now)


def _take_pick(chat_id, key: str):
    """Resolve `pick:<key>` for THIS chat, or None when the list has expired.

    callback_data is attacker-supplied and capped at 64 bytes, so it holds an
    index into a list this server kept — never a URL, a path or a branch. A press
    from a chat that never asked for a list resolves to nothing at all.
    """
    with _picks_lock:
        state = _picks.get(chat_id)
        if not state or time.time() - state.get("at", 0) > _PICK_TTL_S:
            return None
        items = state.get("items") or []
        if key in ("all", "readme"):
            return dict(state, item=None)
        if not str(key).isdigit() or not (0 <= int(key) < len(items)):
            return None
        return dict(state, item=items[int(key)])


def _free_app_name(user, base: str) -> str:
    """A name this account does not already use: base, base-2, base-3, …"""
    base = bot_ops.slugify_name(base) or "app"
    candidate, n = base, 1
    while bot_ops.find_app(user["id"], candidate) and n < 50:
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def offer_repo_choices(chat_id, user, url, intro="", branch="") -> bool:
    """Scan `url` and offer one button per runnable thing inside it.

    Returns False when there is nothing to choose between — not a GitHub URL, the
    API is rate-limited, or the repo holds exactly one obvious entry — and the
    caller deploys the whole repo the way it always did. Degrading to the old
    behaviour beats showing an empty menu.
    """
    owner, repo, parsed_branch = github_repo.parse_repo(url)
    if not owner:
        return False
    branch = branch or parsed_branch
    projects = github_repo.scan_projects(owner, repo, branch)
    if not projects:
        return False
    _remember_picks(chat_id, {"owner": owner, "repo": repo, "branch": branch,
                              "url": github_repo.repo_url(owner, repo, branch),
                              "items": projects})
    lines = [intro or (f"🔎 *{len(projects)} thing(s) can run in `{owner}/{repo}`*"
                       + (f" — branch `{branch}`" if branch else "")), ""]
    for i, p in enumerate(projects, start=1):
        where = "repo root" if not p.get("dir") else f"in `{p['dir']}`"
        deps = ", ".join(f"`{os.path.basename(m)}`" for m in (p.get("manifests") or [])[:2])
        lines.append(f"{i}. *{p['name']}* — {where}, runs `{p['entry']}` "
                     f"({p['language']})" + (f", installs {deps}" if deps else ""))
    lines += ["", "Tap one: I clone it, install what it needs and start it."]
    rows = []
    for i, p in enumerate(projects):
        icon = "🌐" if p.get("kind") in ("web", "static") else "📦"
        label = f"{icon} {p['name']} · {os.path.basename(p['entry'])}"
        rows.append([{"text": label[:60], "callback_data": f"pick:{i}"}])
    if len(projects) > 1:
        rows.append([{"text": "▶️ Deploy the whole repo", "callback_data": "pick:all"}])
    rows.append([{"text": "📖 README", "callback_data": "pick:readme"},
                 {"text": "✖️ Not now", "callback_data": "pick:no"}])
    _send(chat_id, "\n".join(lines), reply_markup={"inline_keyboard": rows})
    return True


def deploy_pick(chat_id, user, state, whole=False):
    """Deploy one scanned project — or the whole repo — with nothing to type."""
    item = None if whole else state.get("item")
    owner, repo = state["owner"], state["repo"]
    branch = state.get("branch") or ""
    url = state.get("url") or github_repo.repo_url(owner, repo, branch)
    name = _free_app_name(user, (item or {}).get("name") or repo)
    entry = (item or {}).get("entry") or ""
    deps = (item or {}).get("manifests") or []
    with _progress(chat_id, "import", name=name) as prog:
        prog.at(10, "Reading the GitHub link")
        prog.at(28, "Cloning the branch" + (f" `{branch}`" if branch else ""))
        if entry:
            prog.at(40, f"Entry `{entry}`")
        prog.at(55, "Installing requirements")
        res = bot_ops.create_app_from_repo(user["id"], name, url, entry=entry, deps=deps)
        if not res.get("ok"):
            prog.fail(res.get("error") or "import failed")
            _send(chat_id, f"❌ {res.get('error')}")
            return res
        prog.at(85, "Starting the app")
        prog.done("Deployed.")
    _send_deployed(chat_id, user, res, branch=branch, repo=True)
    return res


def _send_deployed(chat_id, user, res, branch="", repo=False):
    """The one message every deploy path ends with, buttons included."""
    url_web = res.get("web") or ""
    queen = _user_is_queen(user)
    name = res.get("name")
    lines = [f"✅ *{name}* is deployed and running"
             + (f" from branch `{branch}`" if branch else "") + "."]
    if url_web:
        lines.append(url_web)
    if res.get("commit"):
        lines.append(f"🔖 commit `{str(res['commit'])[:7]}`")
    lines.append("⚠️ If this is a Telegram bot it needs its own token: open the app "
                 "→ *Env* → paste `BOT_TOKEN` from @BotFather → *Save & restart*.")
    lines.append(f"`/logs {name}` if anything looks wrong.")
    if repo:
        lines.append(f"⬆️ `/latest {name}` pulls the newest commit; the button below "
                     f"does the same.")
        if queen:
            lines.append(f"👑 `/autodeploy {name} on` makes it follow the branch by itself.")
    _send(chat_id, "\n".join(lines),
          reply_markup=_app_buttons(res.get("job_db_id"), url=url_web,
                                    repo=repo, queen=queen))


def _send_repo_readme(chat_id, owner, repo, branch=""):
    """📖 The repo's own instructions, as plain text — it is Markdown we do not
    control, so no parse mode, and raw.githubusercontent.com rather than the API:
    the same README read through the API counts against a rate limit shared with
    the scan that fills the project list."""
    raw = (f"https://raw.githubusercontent.com/{owner}/{repo}/"
           f"{quote(branch or 'HEAD', safe='')}/README.md")
    try:
        r = requests.get(raw, timeout=12, headers={"User-Agent": PING_UA})
    except Exception as exc:                                   # noqa: BLE001
        _send_plain(chat_id, f"📖 Couldn't fetch the README just now "
                             f"({type(exc).__name__}). It's here:\n{raw}")
        return
    if r.status_code != 200:
        _send_plain(chat_id, f"📖 No README.md on branch “{branch or 'HEAD'}” "
                             f"(HTTP {r.status_code}).\nRepo: {owner}/{repo}")
        return
    text = (r.text or "").strip() or "(that README is empty)"
    if len(text) > 3800:
        text = text[:3800].rstrip() + "\n\n… truncated — the rest is in the repo."
    _send_plain(chat_id, f"📖 README · {owner}/{repo}"
                         + (f" @ {branch}" if branch else "") + f"\n\n{text}")


# --------------------------------------------------------------------------
# Staying current: the commit an app was built from, and the one after it
# --------------------------------------------------------------------------
def _update_one_app(chat_id, user, row, force=False, new_url=""):
    """Redeploy one app from its branch's HEAD, in place.

    In place means the runner keeps the job id, the folder and the public
    address, so the bot's database and sessions survive — which is the whole
    difference between "updated" and "reinstalled and lost my data".
    """
    name = row["name"]
    old_owner, old_repo, old_branch = github_repo.parse_repo(row.get("repo_url") or "")
    target = (row.get("repo_url") or "")
    if new_url:
        # `/latest name <repo or branch>` — switch the app to another repo or
        # branch. In place: same job, same folder, so its database survives.
        if new_url.startswith("#"):
            if not old_owner:
                _send(chat_id, f"❌ *{name}* has a repo address I can't read, so "
                               f"I can't tell which repo that branch belongs to.")
                return
            target = github_repo.repo_url(old_owner, old_repo, new_url[1:].strip())
        else:
            target = new_url
    owner, repo, branch = github_repo.parse_repo(target)
    if not owner:
        _send(chat_id, f"❌ I can't read that repo address: `{target}`.")
        return
    switching = bool(new_url) and (
        (owner, repo, branch) != (old_owner, old_repo, old_branch))
    if switching:
        force = True
    with _working(chat_id):
        head = github_repo.head_commit(owner, repo, branch)
    current = (row.get("repo_commit") or "").strip()
    if not head:
        _send(chat_id, f"⚠️ I can't read `{owner}/{repo}` right now (GitHub may be "
                       f"rate-limiting this server). Nothing was changed.")
        return
    if head == current and not force:
        _send(chat_id, f"✅ *{name}* is already on the latest commit (`{head[:7]}`).")
        return
    if switching:
        _send(chat_id, f"🔀 *{name}* → `{owner}/{repo}`"
                       + (f" (branch `{branch}`)" if branch else "")
                       + f" at `{head[:7]}` — redeploying in place "
                         f"(same address, same data)…")
    else:
        _send(chat_id, f"⬆️ *{name}*: `{(current or 'unknown')[:7]}` → `{head[:7]}` — "
                       f"redeploying in place (same address, same data)…")
    entry, deps = None, None
    if switching:
        # Which file runs in the NEW repo/branch: keep the old entry if it
        # still exists there, take the only project if there is just one,
        # otherwise let the runner detect it.
        entry = ""
        with _working(chat_id):
            projects = github_repo.scan_projects(owner, repo, branch)
        old_entry = (row.get("repo_entry") or "").strip()
        match = [pr for pr in projects if pr["entry"] == old_entry]
        pick = match[0] if match else (projects[0] if len(projects) == 1 else None)
        if pick:
            entry, deps = pick["entry"], pick["manifests"]
    with _working(chat_id):
        res = bot_ops.update_from_repo(
            user["id"], name,
            repo_url=(target if switching else None), entry=entry, deps=deps)
    if not res.get("ok"):
        _send(chat_id, f"❌ {res.get('error')}")
        return
    commit = (res.get("commit") or head)[:7]
    _send(chat_id, f"✅ *{name}* is now on `{commit}`"
                   + (" — the worker that had it no longer did, so it was placed "
                      "again on another one" if res.get("recreated") else "")
                   + f".\n`/logs {name}` to watch it come back.",
          reply_markup=_app_buttons(row["id"], repo=True, queen=_user_is_queen(user)))


def _latest_menu(chat_id, user, row):
    """The one screen /latest shows: what is running, and what can be done."""
    owner, repo, branch = github_repo.parse_repo(row.get("repo_url") or "")
    commit = (row.get("repo_commit") or "").strip()
    _send(chat_id,
          f"🔁 *{row['name']}*\n"
          f"Repo: `{owner}/{repo}`" + (f" · branch `{branch}`" if branch else " · default branch")
          + (f"\nRunning: `{commit[:7]}`" if commit else "")
          + "\n\nYour data and address stay the same either way.",
          reply_markup={"inline_keyboard": [
              [{"text": "⬆️ Update to latest commit", "callback_data": f"latest:{row['id']}"}],
              [{"text": "🔀 Change repo / branch", "callback_data": f"latestsrc:{row['id']}"}]]})


def _latest_src_start(chat_id, user, row):
    """Ask where the app should come from now: branch buttons, or a pasted link."""
    owner, repo, branch = github_repo.parse_repo(row.get("repo_url") or "")
    with _working(chat_id):
        branches = github_repo.list_branches(owner, repo) if owner else []
    _pending[chat_id] = {
        "mode": "latest_src", "user": user, "user_id": user["id"],
        "job_id": row["id"], "ref": row["name"], "branches": branches,
        "owner": owner, "repo": repo,
        "expires": time.time() + _PENDING_TTL_S,
    }
    rows = [[{"text": ("✅ " if b == branch else "🌿 ") + b[:40],
              "callback_data": f"latestbr:{i}"}] for i, b in enumerate(branches)]
    rows.append([{"text": "✖️ Cancel", "callback_data": "latestcancel:"}])
    _send(chat_id,
          f"🔀 *{row['name']}* — where should it come from?\n"
          + ("Tap a branch of this repo, or " if branches else "")
          + "send a GitHub link for another repo or branch:\n"
            "`github.com/user/repo` or `github.com/user/repo/tree/dev`",
          reply_markup={"inline_keyboard": rows})


def _latest_src_apply(chat_id, pending, text):
    """Run the switch the person asked for, then clear the slot."""
    _pending.pop(chat_id, None)
    user = pending["user"]
    row = bot_ops.find_app(user["id"], str(pending["job_id"]))
    if not row:
        _send(chat_id, "❌ That app is not yours or no longer exists.")
        return
    t = (text or "").strip()
    if t and "/" not in t and "github" not in t.lower() and not t.startswith("#"):
        t = "#" + t                      # a bare word is a branch of this repo
    if not t:
        _send(chat_id, "I need a GitHub link or a branch name. `/latest` to start again.")
        return
    _update_one_app(chat_id, user, row, new_url=t)


def cmd_latest(chat_id, user, ref=""):
    """`/latest [name] [new repo or #branch]` — update a repo app, or move it to
    another repo/branch. The app keeps its job, folder and database."""
    apps = [a for a in bot_ops.list_apps(user["id"]) if (a.get("repo_url") or "").strip()]
    if not apps:
        _send(chat_id, "None of your apps came from a repo, so there is no commit to "
                       "follow. `/import x` deploys one"
                       + (" — and 👑 `/projects` lists ready-made ones."
                          if _user_is_queen(user) else "."))
        return
    if ref:
        # `/latest name`                       → newest commit of the same branch
        # `/latest name https://github.com/u/r/tree/dev` → switch repo/branch
        # `/latest name #dev`                  → switch branch, same repo
        new_url = ""
        toks = ref.split()
        last = toks[-1] if toks else ""
        if len(toks) > 1 and (
                "github.com" in last.lower() or last.startswith("#")
                or re.match(r"^[\w.-]+/[\w.-]+(?:[/#].*)?$", last)):
            new_url = last
            ref = " ".join(toks[:-1])
        row = bot_ops.find_app(user["id"], ref)
        if not row or not (row.get("repo_url") or "").strip():
            _send(chat_id, f"❌ No repo-backed app called “{ref}”. `/apps` lists yours.")
            return
        if new_url:
            _update_one_app(chat_id, user, row, new_url=new_url)
        else:
            _latest_menu(chat_id, user, row)
        return
    if len(apps) == 1:
        _latest_menu(chat_id, user, apps[0])
        return
    rows = [[{"text": f"🔁 {a['name']}", "callback_data": f"latestmenu:{a['id']}"}]
            for a in apps[:8]]
    _send(chat_id, "🔎 *Which app?*\nThen choose: update to the latest commit, or "
                   "switch it to another repo / branch.",
          reply_markup={"inline_keyboard": rows})


def cmd_autodeploy(chat_id, user, arg=""):
    """👑 `/autodeploy name on|off` — follow the branch without being asked."""
    if not _user_is_queen(user):
        _send(chat_id, "👑 Auto-deploy is part of queen access — ask the bot owner if you need it. "
                       "Anyone can still pull an update with `/latest name`.")
        return
    parts = (arg or "").split()
    states = ("on", "off", "yes", "no", "true", "false")
    if len(parts) < 2 or parts[1].lower() not in states:
        rows = [[{"text": f"⚙️ {a['name']} — {'ON' if a.get('auto_deploy') else 'off'}",
                  "callback_data": f"autodep:{a['id']}"}]
                for a in bot_ops.list_apps(user["id"])
                if (a.get("repo_url") or "").strip()][:8]
        _send(chat_id, "⚙️ *Auto-deploy* — I redeploy the app by itself when its branch "
                       "gets a new commit (checked every few minutes, in place, so the "
                       "data survives).\n\n"
                       "`/autodeploy name on` or `/autodeploy name off`, "
                       "or tap one below.",
              reply_markup={"inline_keyboard": rows} if rows else None)
        return
    res = bot_ops.set_auto_deploy(user["id"], parts[0], parts[1].lower() in ("on", "yes", "true"))
    if not res.get("ok"):
        _send(chat_id, f"❌ {res.get('error')}")
        return
    name = res["job"]["name"]
    if res["on"]:
        _send(chat_id, f"⚙️ *{name}* now follows its branch: when a new commit lands I "
                       f"redeploy it in place and tell you.\n`/autodeploy {name} off` "
                       f"stops that.")
    else:
        _send(chat_id, f"⚙️ *{name}* no longer deploys by itself. `/latest {name}` still "
                       f"updates it whenever you ask.")


def toggle_autodeploy(chat_id, user, row):
    """The ⚙️ button: flip auto-deploy for one app, and say which way it went."""
    on = not bool(row.get("auto_deploy"))
    res = bot_ops.set_auto_deploy(user["id"], row["name"], on)
    if not res.get("ok"):
        _send(chat_id, f"❌ {res.get('error')}")
        return
    _send(chat_id, f"⚙️ *{row['name']}* auto-deploy is now "
                   + ("ON — I redeploy when the branch moves." if on else "off."))


# ==================== 👑 PROJECTS ====================
# There is NO separate "queen projects" GitHub repo any more. A 👑 account gets
# higher limits (no RAM ceiling, big zips, auto-deploy) and can `/import` any
# public repo the same way everybody else can — the catalogue used to point at
# one hard-coded owner repo, which made "queen" look like "one special project"
# instead of "more capacity". QUEEN_PROJECTS_REPO is still read if an admin sets
# it (a personal starter pack), but it is empty by default and nothing breaks
# without it.
QUEEN_PROJECTS_REPO = os.getenv("QUEEN_PROJECTS_REPO", "").strip().rstrip("/")
QUEEN_PROJECTS_BRANCH = os.getenv("QUEEN_PROJECTS_BRANCH", "").strip()
QUEEN_PROJECTS_NAME = os.getenv("QUEEN_PROJECTS_NAME", "").strip()
def _queen_repo_parts() -> tuple:
    """(owner, repo) of the configured project repo, or ("", "")."""
    m = re.search(r"github\.com/([^/\s]+)/([^/\s#]+?)(?:\.git)?(?:[#/].*)?$",
                  QUEEN_PROJECTS_REPO)
    return (m.group(1), m.group(2)) if m else ("", "")


def queen_project_url() -> str:
    """The `/import`-ready URL for the project, branch included."""
    if QUEEN_PROJECTS_BRANCH:
        return f"{QUEEN_PROJECTS_REPO}/tree/{QUEEN_PROJECTS_BRANCH}"
    return QUEEN_PROJECTS_REPO


def _project_app_name(user) -> str:
    """A free app name for this project, so a second deploy doesn't collide."""
    _owner, repo = _queen_repo_parts()
    base = QUEEN_PROJECTS_NAME or (repo if len(repo or "") > 2 else f"{repo or 'queen'}-project")
    base = bot_ops.slugify_name(base) or "project"
    candidate, n = base, 1
    while bot_ops.find_app(user["id"], candidate) and n < 50:
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def _send_repo_apps(chat_id, user, limit=6):
    """Your apps that came from a repo: which commit they are on, and whether it
    moved since.

    One GitHub lookup per DISTINCT repo (cached), so this screen costs nothing
    extra when several apps share a source — and it is the answer to "did my
    deploy pick up the change I pushed?" without opening a browser.
    """
    apps = [a for a in bot_ops.list_apps(user["id"]) if (a.get("repo_url") or "").strip()]
    if not apps:
        return
    queen = _user_is_queen(user)
    lines = ["🔎 *Your repo apps*"]
    rows = []
    for a in apps[:limit]:
        owner, repo, branch = github_repo.parse_repo(a.get("repo_url") or "")
        head = github_repo.head_commit(owner, repo, branch) if owner else ""
        current = (a.get("repo_commit") or "").strip()
        if head and current and head != current:
            state = f"⬆️ newer commit waiting (`{current[:7]}` → `{head[:7]}`)"
        elif head and current:
            state = f"✅ up to date (`{current[:7]}`)"
        elif current:
            state = f"🔖 on `{current[:7]}`"
        else:
            state = "🔖 commit unknown (deployed before this was recorded)"
        auto = " · ⚙️ auto-deploy on" if a.get("auto_deploy") else ""
        lines.append(f"• *{a['name']}* — `{owner}/{repo}`"
                     + (f" @ `{branch}`" if branch else "") + f"\n   {state}{auto}")
        row = [{"text": f"⬆️ {a['name']}"[:60], "callback_data": f"latest:{a['id']}"}]
        if queen:
            row.append({"text": f"⚙️ auto: {'on' if a.get('auto_deploy') else 'off'}",
                        "callback_data": f"autodep:{a['id']}"})
        rows.append(row)
    if len(apps) > limit:
        lines.append(f"… and {len(apps) - limit} more — `/latest` lists them all.")
    lines.append("\n⬆️ redeploys in place: same address, same folder, same data.")
    _send(chat_id, "\n".join(lines), reply_markup={"inline_keyboard": rows})


def cmd_projects(chat_id, user, arg=""):
    """`/projects` — your repo-backed apps, and (optionally) a starter pack.

    There is no separate "queen projects" GitHub repo any more. Everyone who can
    talk to the bot can see the apps they already deployed from a repo, the
    commit each is on, and a ⬆️ button when the branch moved. A 👑 account also
    gets `/autodeploy` and the higher limits; if an admin has set
    QUEEN_PROJECTS_REPO as a personal starter pack, that is offered too — but
    nothing depends on it.
    """
    sub = (arg or "").strip().lower()
    if sub in ("run", "deploy", "start", "install"):
        if not _user_is_queen(user):
            _send(chat_id, "▶️ One-tap starter deploy is part of 👑 queen access. "
                           "Use `/import x` for any public repo — that "
                           "works for everyone.")
            return
        _deploy_queen_project(chat_id, user)
        return
    if sub in ("readme", "read", "doc", "docs"):
        _send_project_readme(chat_id)
        return
    if sub in ("latest", "update", "upgrade", "apps"):
        cmd_latest(chat_id, user)
        return

    # Optional starter pack — only when an admin actually configured one.
    owner, repo = _queen_repo_parts()
    if owner and _user_is_queen(user):
        branch = QUEEN_PROJECTS_BRANCH or ""
        url = queen_project_url()
        with _working(chat_id):
            offered = offer_repo_choices(
                chat_id, user, url,
                intro=f"📦 *Starter pack* — `{owner}/{repo}`"
                      + (f" · branch `{branch}`" if branch else ""))
        if not offered:
            _send(chat_id, f"📦 Starter pack `{owner}/{repo}` is configured but "
                           f"I couldn't list it just now. `/import {owner}/{repo}` "
                           f"still works, or ▶️ below deploys the default entry.",
                  reply_markup={"inline_keyboard": [
                      [{"text": "▶️ Run starter", "callback_data": "qproj:run"},
                       {"text": "📖 README", "callback_data": "qproj:readme"}]]})

    # The useful part for everyone: your own repo apps + how to add more.
    lines = ["📦 *Your projects*",
             "Deploy any public GitHub repo with "
             "`/import owner/repo [name]` — I scan it and show each runnable "
             "thing as a button. A branch: "
             "`/import owner/repo/tree/branch myapp`."]
    if _user_is_queen(user):
        lines.append("👑 You also have `/autodeploy name on` — I watch the "
                     "branch and redeploy by myself when it moves.")
    _send(chat_id, "\n".join(lines),
          reply_markup={"inline_keyboard": [
              [{"text": "⬆️ Deploy latest", "callback_data": "latest:"},
               {"text": "🌿 How to import", "callback_data": "help:import"}],
              ([{"text": "👑 Queen panel", "callback_data": "queen:menu"}]
               if _user_is_queen(user) else
               [{"text": "📋 My apps", "callback_data": "apps:list"}])]})
    _send_repo_apps(chat_id, user)


def _deploy_queen_project(chat_id, user, name=""):
    """One tap = the project cloned, installed and running.

    Delegates to cmd_import so a queen deploy goes through exactly the same
    rails as any other repo import — same cap check, same slug rules, same
    messages — instead of a second copy of that logic drifting apart.
    """
    url = queen_project_url()
    if not url:
        _send(chat_id, "👑 No project repo is configured on this server yet.")
        return
    clean = bot_ops.slugify_name(name or "") or _project_app_name(user)
    cmd_import(chat_id, user, f"{url} {clean}")


def _send_project_readme(chat_id):
    """📖 The configured 👑 project repo's own README.

    Delegates to the reader the picker's README button uses, so there is one
    implementation: raw.githubusercontent.com (the API copy would spend rate
    limit the project scan also needs) and no parse mode, because a README is
    Markdown we do not control and legacy Markdown would either mangle it or get
    the whole message rejected.
    """
    owner, repo = _queen_repo_parts()
    if not owner:
        _send(chat_id, "👑 No project repo is configured on this server yet.")
        return
    _send_repo_readme(chat_id, owner, repo, QUEEN_PROJECTS_BRANCH or "")



def _maybe_guide(chat_id, topic: str, user=None):
    """Send the cartoon for a command at most once per chat per process.

    First time someone runs /code (or /update, /import, …) they see the picture
    then the normal flow. Repeat runs stay quiet — they already know the steps.
    """
    topic = (topic or "").strip().lower()
    if topic not in _COMMAND_GUIDES:
        return
    seen = _guide_shown.setdefault(chat_id, set())
    if topic in seen:
        return
    seen.add(topic)
    try:
        _show_command_guide(chat_id, topic, user)
    except Exception:
        logger.exception("guide %s failed", topic)


def cmd_code_start(chat_id, user, name):
    """/code <new app name> — the NEXT message from this chat becomes the
    app's source (text or a file)."""
    if not name:
        _send(chat_id, "Usage: `/code myapp`, then send the source "
                       "as a message or upload a file.")
        return
    clean = bot_ops.slugify_name(name)
    if not clean:
        _send(chat_id, "That name has no usable characters — letters, numbers, "
                       "spaces, `-` and `_` only.")
        return
    if bot_ops.find_app(user["id"], clean):
        _send(chat_id, f"You already have an app called “{clean}”. "
                       f"Use `/update {clean}` to change its code instead.")
        return
    _pending[chat_id] = {
        "mode": "create", "user_id": user["id"], "name": clean,
        "step": "requirements",
        "expires": time.time() + _PENDING_TTL_S,
    }
    _send(chat_id, f"📦 Creating *{clean}*. Any pip packages it needs? "
                   f"Reply with names (e.g. `python-telegram-bot==21.4, requests`), "
                   f"or `/skip` if none. `/cancel` to stop.")


def cmd_update_start(chat_id, user, ref):
    """/update <existing app name> — the NEXT message becomes its new code,
    redeployed in place (workspace data preserved)."""
    if not ref:
        _send(chat_id, "Usage: `/update myapp`, then send the new "
                       "source as a message or upload a file.")
        return
    row = bot_ops.find_app(user["id"], ref)
    if not row:
        _send(chat_id, f"No app called “{ref}”. `/apps` lists yours.")
        return
    _pending[chat_id] = {
        "mode": "update", "user_id": user["id"], "ref": row["name"],
        "step": "requirements",
        "expires": time.time() + _PENDING_TTL_S,
    }
    _send(chat_id, f"📦 Updating *{row['name']}*. Any pip packages the new "
                   f"code needs? Reply with names, or `/skip` to leave "
                   f"requirements as they are. `/cancel` to stop.")


def cmd_cancel_pending(chat_id):
    had = _pending.pop(chat_id, None)
    _send(chat_id, "❎ Cancelled." if had else "Nothing pending.")


def _get_pending(chat_id):
    """The chat's pending /code or /update slot, or None if there isn't one
    or it has expired (and is cleaned up on the way out either way)."""
    p = _pending.get(chat_id)
    if not p:
        return None
    if time.time() > p["expires"]:
        _pending.pop(chat_id, None)
        return None
    return p


TG_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024   # Telegram bot API download ceiling

# .zip bundles: RunSpace only ever runs ONE source file (see _CODE_EXT_LANG —
# the runner has no multi-file support), so a zip is just a convenience for
# "my code plus a requirements.txt in one upload", not a real multi-file app.
# Extraction reads member bytes straight out of the ZipFile object in memory
# — nothing is ever written to disk — so there's no zip-slip path-traversal
# surface here at all.
ZIP_MAX_UNCOMPRESSED_BYTES = 5 * 1024 * 1024   # 5MB unzipped, plenty for source
ZIP_MAX_ENTRIES = 500
_ZIP_ENTRY_PREFERENCE = ("main", "bot", "app", "index", "run")


def _pick_zip_entry(code_names: list) -> str:
    """Choose which code file in the zip to deploy, preferring conventional
    entry-point names and shallower paths."""
    def sort_key(name):
        base = name.rsplit("/", 1)[-1]
        stem = base.rsplit(".", 1)[0].lower()
        depth = name.count("/")
        pref_rank = (_ZIP_ENTRY_PREFERENCE.index(stem)
                     if stem in _ZIP_ENTRY_PREFERENCE else len(_ZIP_ENTRY_PREFERENCE))
        return (depth, pref_rank, name)
    return sorted(code_names, key=sort_key)[0]


def _extract_zip_document(raw: bytes) -> tuple:
    """Pull one runnable source file (+ optional requirements.txt) out of an
    uploaded .zip. Returns (code, lang, requirements, note, error) — same
    error-tuple convention as _download_document, just two extra slots for
    the requirements text pulled from the zip and a heads-up note to show
    the user (e.g. "other files were ignored")."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return None, None, None, None, "That .zip file looks corrupted — try re-exporting it."

    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > ZIP_MAX_ENTRIES:
        return None, None, None, None, f"That zip has {len(infos)} files — over the {ZIP_MAX_ENTRIES} limit."
    total = sum(i.file_size for i in infos)
    if total > ZIP_MAX_UNCOMPRESSED_BYTES:
        mb = ZIP_MAX_UNCOMPRESSED_BYTES // (1024 * 1024)
        return None, None, None, None, f"Unzipped that's over {mb}MB — too big for a RunSpace bundle."

    names = [
        i.filename for i in infos
        if not i.filename.startswith("__MACOSX/")
        and not i.filename.rsplit("/", 1)[-1].startswith(".")
    ]
    code_names = [
        n for n in names
        if "." in n.rsplit("/", 1)[-1]
        and n.rsplit(".", 1)[-1].lower() in _CODE_EXT_LANG
    ]
    if not code_names:
        return None, None, None, None, ("No runnable file found inside — need one of "
                                         ".py/.js/.sh/.rb/.php (e.g. `main.py`).")

    entry = _pick_zip_entry(code_names)
    lang = _CODE_EXT_LANG[entry.rsplit(".", 1)[-1].lower()]
    try:
        code = zf.read(entry).decode("utf-8")
    except UnicodeDecodeError:
        return None, None, None, None, f"`{entry}` isn't plain text — can't deploy a binary as source code."

    req_name = next((n for n in names if n.rsplit("/", 1)[-1].lower() == "requirements.txt"), None)
    reqs = None
    if req_name:
        try:
            reqs = zf.read(req_name).decode("utf-8").strip() or None
        except UnicodeDecodeError:
            reqs = None

    ignored = len(code_names) - 1
    note = None
    if ignored > 0:
        note = (f"📦 Using `{entry}` as the entry point ({ignored} other code "
                f"file(s) in the zip were ignored — RunSpace apps run a single file).")

    return code, lang, reqs, note, None


def _download_document(doc) -> tuple:
    """Fetch an uploaded document and resolve it to deployable source.

    Returns (code, lang, requirements, note, error):
      - error is a user-facing string, or None on success.
      - lang/requirements/note are None for a plain source file; a .zip can
        populate lang (from whichever entry file it picked) and requirements
        (from a requirements.txt inside it) and note (a heads-up about
        files that were ignored).
    Binary files (images, compiled anything) are rejected — RunSpace runs
    source text, nothing else. .zip is the one archive format supported,
    handled by _extract_zip_document.
    """
    size = doc.get("file_size") or 0
    if size > TG_MAX_DOWNLOAD_BYTES:
        return None, None, None, None, f"That file is {size // (1024*1024)}MB — Telegram bots can only download up to 20MB."
    try:
        meta = _tg("getFile", file_id=doc["file_id"])
        file_path = (meta.get("result") or {}).get("file_path")
        if not file_path:
            return None, None, None, None, "Telegram didn't return that file. Try sending it again."
        r = requests.get(f"{TG_FILE_API}/{file_path}", timeout=60)
        r.raise_for_status()
        raw = r.content
    except Exception as exc:  # noqa: BLE001
        logger.warning("bot document download failed: %s", exc)
        return None, None, None, None, "Couldn't download that file from Telegram. Try again."

    filename = doc.get("file_name") or ""
    if filename.lower().endswith(".zip"):
        return _extract_zip_document(raw)

    try:
        code = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, None, None, None, "That file isn't plain text (looks binary) — RunSpace runs source code, not compiled files or archives."
    return code, _lang_for_document(filename), None, None, None


def _lang_for_document(filename: str) -> str:
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    return _CODE_EXT_LANG.get(ext)


def handle_pending_code(chat_id, msg, pending):
    """A text or document message arrived while /code or /update was
    waiting on this chat. Resolve it to source + language and deploy."""
    # Requirements are asked as their OWN step, before code — pasting code
    # that must also correctly contain a hand-typed "# requirements: ..."
    # comment is exactly what caused trouble: easy to forget, easy to
    # place wrong, easy to mistype. This way the runner still only ever
    # reads that same comment line (nothing changes downstream) — it's
    # just the bot's job to write it correctly, not the user's.
    if pending.get("mode") == "latest_src":
        _latest_src_apply(chat_id, pending, msg.get("text") or "")
        return
    if pending.get("step") == "requirements":
        text = (msg.get("text") or "").strip()
        if text and text.lower() != "/skip":
            pending["requirements"] = text
        pending["step"] = "code"
        pending["expires"] = time.time() + _PENDING_TTL_S
        _send(chat_id, "Now send the source — paste it as a message, or "
                       "upload a file (.py/.js/.sh/.rb/.php) or a .zip bundle. `/cancel` to stop.")
        return

    doc = msg.get("document")
    zip_raw = None
    if doc and (doc.get("file_name") or "").lower().endswith(".zip"):
        tg_uid = (msg.get("from") or {}).get("id")
        linked_user = telegram_link.user_for_chat(tg_uid)
        # 👑 implies zip access. The flag is an admin's decision to trust this
        # account with more of the box (no memory ceiling, bigger bundles), so
        # making them ask a second time with /admin allowzip was a privilege
        # that looked granted and behaved as if it wasn't.
        queen = _user_is_queen(linked_user)
        if not (_is_admin(linked_user, tg_uid) or (linked_user or {}).get("can_upload_zip")
                or queen):
            _send(chat_id, "🔒 .zip uploads need approval on this account. "
                           "Ask the bot owner, or send a single source file instead.")
            return
        # Multi-file path for BOTH create and update: hand the WHOLE zip to
        # the runner, which extracts it for real (see
        # runner/app.py:_extract_zip_bundle / the PATCH re-extraction added
        # alongside it) instead of the old behaviour of reading one file out
        # of the zip and dropping every other file in it — which is exactly
        # what broke any app whose entry file imported a sibling module.
        size = doc.get("file_size") or 0
        if size > TG_MAX_DOWNLOAD_BYTES:
            # The 20MB wall is Telegram's, not ours, and no setting lifts it.
            # What CAN be said is the way round it: the website upload has no
            # Telegram in the middle, and a 👑 account is allowed a big bundle
            # there (bot_ops.zip_limits_for).
            bigger = ""
            if queen:
                where = f"{SITE_BASE}/bots" if SITE_BASE else "the dashboard"
                bigger = (f"\n\n👑 Send the big one through {where} instead — the "
                          f"website upload doesn't go through Telegram, and your "
                          f"account is allowed up to {bot_ops.QUEEN_ZIP_MAX_MB}MB "
                          f"unzipped ({bot_ops.QUEEN_ZIP_MAX_FILES} files).")
            _send(chat_id, f"❌ That file is {size // (1024*1024)}MB — "
                           f"Telegram bots can only download up to 20MB.\nSend it again, or `/cancel`."
                           + bigger)
            return
        try:
            meta = _tg("getFile", file_id=doc["file_id"])
            file_path = (meta.get("result") or {}).get("file_path")
            r = requests.get(f"{TG_FILE_API}/{file_path}", timeout=60)
            r.raise_for_status()
            zip_raw = r.content
        except Exception as exc:  # noqa: BLE001
            logger.warning("bot zip download failed: %s", exc)
            _send(chat_id, "❌ Couldn't download that zip from Telegram. Try again, or `/cancel`.")
            return

    if zip_raw is not None:
        _pending.pop(chat_id, None)
        if pending["mode"] == "create":
            with _progress(chat_id, "zip", name=pending["name"]) as prog:
                prog.at(12, "Got the zip")
                prog.at(30, "Unpacking on the runner")
                res = bot_ops.create_app_from_zip(pending["user_id"], pending["name"], zip_raw)
                if not res.get("ok"):
                    prog.fail(res.get("error") or "deploy failed")
                    _send(chat_id, f"❌ {res['error']}")
                    return
                prog.at(70, "Installing & starting")
                prog.done("Running.")
            url = res.get("web") or ""
            _send(chat_id, f"✅ *{res['name']}* created and running.\n"
                           + (url + "\n" if url else "")
                           + "⚠️ No Telegram bot token check on a zip import yet — if this is a "
                             f"bot, `/update {res['name']}` once (with the same entry file's code) "
                             f"to verify it.\n`/status {res['name']}` for details.",
                  reply_markup=_app_buttons(res["job_db_id"], url=url))
        else:
            with _progress(chat_id, "zip", name=pending.get("ref") or "app") as prog:
                prog.at(12, "Got the zip")
                prog.at(35, "Unpacking into the workspace")
                res = bot_ops.update_from_zip(pending["user_id"], pending["ref"], zip_raw)
                if not res.get("ok"):
                    prog.fail(res.get("error") or "update failed")
                    _send(chat_id, f"❌ {res['error']}")
                    return
                prog.at(75, "Restarting")
                prog.done("Updated.")
            _send(chat_id, f"✅ *{res['job']['name']}* updated from zip and restarted.\n"
                           f"Existing data files (databases, sessions) were left alone — "
                           f"only what's in the zip was written.",
                  reply_markup=_app_buttons(res["job"]["id"],
                                             bot_username=res["job"].get("telegram_bot_username") or ""))
        return

    if doc:
        code, doc_lang, zip_reqs, note, err = _download_document(doc)
        if err:
            _send(chat_id, f"❌ {err}\nSend the file again, or `/cancel`.")
            return  # slot stays open — let them retry without re-typing the command
        if note:
            _send(chat_id, note)
        if zip_reqs and not (pending.get("requirements") or "").strip():
            pending["requirements"] = zip_reqs
    else:
        code = (msg.get("text") or "").strip()
        doc_lang = None
        if not code:
            _send(chat_id, "Send the source as text or a file, or `/cancel`.")
            return

    reqs = (pending.get("requirements") or "").strip()
    if reqs and not re.search(r"^#\s*requirements:", code, re.I | re.M):
        code = f"# requirements: {reqs}\n{code}"

    _pending.pop(chat_id, None)  # slot consumed either way from here on

    if pending["mode"] == "create":
        lang = doc_lang or "python"
        with _progress(chat_id, "create", name=pending["name"]) as prog:
            prog.at(10, "Got your code")
            if (pending.get("requirements") or "").strip():
                prog.at(22, "Writing requirements")
            prog.at(38, "Uploading to the site")
            res = bot_ops.create_app(pending["user_id"], pending["name"], lang, code)
            if not res.get("ok"):
                prog.fail(res.get("error") or "deploy failed")
                _send(chat_id, f"❌ {res['error']}")
                return
            prog.at(72, "Starting on the runner")
            prog.at(90, "Almost done")
            prog.done("Running.")
        url = res.get("web") or ""
        note = ""
        if not res.get("telegram_bot_username"):
            note = "\n⚠️ Couldn't verify a Telegram bot token in this code — no “Open your bot” button yet."
        _send(chat_id, f"✅ *{res['name']}* created and running ({lang}).\n"
                       + (url + "\n" if url else "") + note
                       + f"\n`/status {res['name']}` for details.",
              reply_markup=_app_buttons(res["job_db_id"], url=url,
                                         bot_username=res.get("telegram_bot_username") or ""))
    else:
        # /update never changes the runtime on its own — a .js file dropped
        # onto a python app would silently swap what it runs. Only apply the
        # inferred language if it MATCHES what's already there; otherwise
        # keep the app's existing language and let the code speak for itself.
        row = bot_ops.find_app(pending["user_id"], pending["ref"])
        lang = doc_lang if (doc_lang and row and doc_lang == row.get("language")) else None
        name = (row or {}).get("name") or pending.get("ref") or "app"
        with _progress(chat_id, "update", name=name) as prog:
            prog.at(12, "Got the new code")
            prog.at(35, "Saving a snapshot")
            res = bot_ops.update_code(pending["user_id"], pending["ref"], code, lang)
            if not res.get("ok"):
                prog.fail(res.get("error") or "update failed")
                _send(chat_id, f"❌ {res['error']}")
                return
            prog.at(70, "Restarting in place")
            prog.at(92, "Almost done")
            prog.done("Updated.")
        _send(chat_id, f"✅ *{res['job']['name']}* updated, saved and restarted.",
              reply_markup=_app_buttons(res["job"]["id"],
                                         bot_username=res["job"].get("telegram_bot_username") or ""))


# ==================== CALLBACK HANDLER ====================
def handle_callback(chat_id, data, message_id=None):
    """Inline buttons. Every action re-resolves the app FOR THIS USER.

    callback_data is attacker-supplied — anyone can craft a button press with
    someone else's job id — so the id is looked up scoped to the pressing
    chat's account, never trusted on its own.
    """
    try:
        action, ref = data.split(":", 1)
    except Exception:
        return

    if action == "admin":
        try:
            sub_action, sub_ref = ref.split(":", 1)
        except ValueError:
            sub_action, sub_ref = ref, ""
        # Private chat: chat_id IS the Telegram user id of whoever pressed
        # the button — same identity handle_admin_callback re-checks itself.
        handle_admin_callback(chat_id, chat_id, sub_action, sub_ref, message_id)
        return

    user = telegram_link.user_for_chat(chat_id)
    # A few actions work without a linked account — same rule as /ping and /id.
    # Everything else needs one, and must say so (never silent).
    if not user and action not in ("ping", "help"):
        _send(chat_id, "🔗 This chat isn't linked to a CodeNest account yet.\n"
                       "Send `/link`, pick your account, and the buttons work.")
        return

    if action == "logs":
        cmd_logs(chat_id, user, ref)
    elif action == "stat":
        cmd_status(chat_id, user, ref)
    elif action == "restart":
        cmd_restart(chat_id, user, ref)
    elif action == "stop":
        cmd_stop(chat_id, user, ref)
    elif action == "db":
        _send_job_data(chat_id, user, ref)
    elif action == "delconfirm":
        _cmd_delete_confirmed(chat_id, user, ref)
    elif action == "delcancel":
        _send(chat_id, "Cancelled — nothing was deleted.")
    elif action == "qproj":
        # "list" is for everyone (your own repo apps). run/readme only make
        # sense when a starter pack is configured, and still require 👑.
        if ref == "list":
            cmd_projects(chat_id, user)
        elif not _user_is_queen(user):
            _send(chat_id, "👑 That's part of queen access — ask the bot owner if you need it. "
                           "`/import owner/repo` works for anyone.")
        elif ref == "readme":
            _send_project_readme(chat_id)
        else:
            _deploy_queen_project(chat_id, user)
    elif action == "help":
        # Cartoon + short steps. Each guide mirrors a typed command so the
        # "every action = button + command" rule holds for newcomers too.
        if ref in ("", "home"):
            u = user or telegram_link.user_for_chat(chat_id)
            _send(chat_id, _help_text(u) if u else (
                "👋 *CodeNest help*\n\n"
                "Tap a guide below, or type `/help` any time.\n"
                "New here? Start with *How to start*."),
                  reply_markup=_main_kb(u))
        elif ref == "import":
            _send_guide(chat_id, "guide_import",
                        "Import a GitHub repo — three steps")
            _send(chat_id,
                  "🌿 *Import a GitHub repo*\n\n"
                  "Command: `/import owner/repo [name]`\n"
                  "Or paste a full `https://github.com/…` URL.\n\n"
                  "I scan it and show each runnable entry as a *button* — "
                  "tap one to deploy.\n"
                  "Branch: `/import owner/repo/tree/branch myapp`\n\n"
                  "After it starts: `/logs name` · `/status name` · "
                  "`/latest name`."
                  + ("\n👑 `/autodeploy name on` follows the branch."
                     if user and _user_is_queen(user) else ""),
                  reply_markup={"inline_keyboard": [[{"text": "📖 More guides", "callback_data": "help:catalog"}]]})
        elif ref == "start":
            _send_guide(chat_id, "guide_start", "Start here — three taps")
            _send(chat_id,
                  "📖 *How to start*\n\n"
                  "1. Tap *Open CodeNest* (or send `/link`).\n"
                  "2. Deploy: paste code, pick a template, or `/import` a repo.\n"
                  "3. *Save & Run*. After a runner restart, bots come back alone.\n\n"
                  "Handy: `/apps` · `/status` · `/logs name` · `/id`",
                  reply_markup={"inline_keyboard": [[{"text": "📖 More guides", "callback_data": "help:catalog"}]]})
        elif ref == "id":
            _send_guide(chat_id, "guide_id", "Your ids — long-press to copy")
            # Reuse the real /id output so the numbers match what admins need.
            cmd_id(chat_id)
            _send(chat_id, "Tip: long-press a number to copy it. No broken <id> markup.",
                  reply_markup={"inline_keyboard": [[{"text": "📖 More guides", "callback_data": "help:catalog"}]]})
        elif ref == "token":
            _send_guide(chat_id, "guide_token", "BOT_TOKEN — Env or inside code")
            _send(chat_id,
                  "🔑 *BOT_TOKEN tips*\n\n"
                  "Either place works — recovery finds both:\n"
                  "• *Env tab* on the site → paste BotFather token → Save & restart\n"
                  "• *In the code*: `BOT_TOKEN = '123:AA…'` (no Env needed)\n\n"
                  "After a runner restart your bot comes back if the token is "
                  "in either place. You do *not* have to set it only in Env.",
                  reply_markup={"inline_keyboard": [[{"text": "📖 More guides", "callback_data": "help:catalog"}]]})
        elif ref in ("code", "update", "apps", "logs", "restart", "stop",
                     "status", "source", "delete", "rename", "latest",
                     "env", "backup", "history", "link", "projects"):
            _show_command_guide(chat_id, ref, user)
        elif ref == "catalog":
            lines = ["📖 *Guides* (how-to pictures — only when you ask):",
                     "`/guide code` · `/guide update` · `/guide import`",
                     "`/guide env` · `/guide token` · `/guide link`",
                     "",
                     "Day-to-day (no picture — direct answer):",
                     "`/apps` · `/logs name` · `/status name` · `/restart name`"]
            _send(chat_id, "\n".join(lines))
        elif ref == "commands":
            cmd_commands(chat_id, user)
        elif ref == "admin":
            u = user or telegram_link.user_for_chat(chat_id)
            if not _is_admin(u, chat_id):
                # Silent for non-admins — do not advertise the admin surface.
                _send(chat_id, "Unknown guide — try /help.",
                      reply_markup={"inline_keyboard": [[{"text": "📖 More guides", "callback_data": "help:catalog"}]]})
            else:
                _send_guide(chat_id, "guide_admin", "Admin — command + button")
                _send(chat_id,
                      "🛠 *Admin how-to*\n\n"
                      "Every admin action has *both* a button under `/admin` "
                      "and a matching typed command.\n\n"
                      "`/admin` · `/runners` · `/recover` · `/user name`\n"
                      "`/queen id` · `/admin limit id 10` · `/see id`",
                      reply_markup={"inline_keyboard": [[{"text": "📖 More guides", "callback_data": "help:catalog"}]]})
        else:
            _send(chat_id, "Unknown guide — try /help.", reply_markup={"inline_keyboard": [[{"text": "📖 More guides", "callback_data": "help:catalog"}]]})
    elif action == "apps" and ref == "list":
        cmd_apps(chat_id, user)
    elif action == "queen":
        # The 👑 panel: a queen's own limits and the buttons that use them.
        if not user or not _user_is_queen(user):
            _send(chat_id, "👑 That panel is part of queen access — an admin "
                           "can turn it on for you. "
                           "`/limits` shows what your account can do now.")
        elif ref == "apps":
            cmd_apps(chat_id, user)
        elif ref == "runner":
            if not _user_is_queen(user):
                _send(chat_id, "👑 Runner selection is part of queen access.")
            else:
                _send(chat_id, _queen_runner_text(user), reply_markup=_queen_runner_kb(user))
        elif ref.startswith("pin:"):
            if not user or not _user_is_queen(user):
                _send(chat_id, "👑 Only queen accounts can pin a runner.")
            else:
                val = ref[4:]
                uid = _row_id(user)
                if val == "auto":
                    runner_client.clear_queen_pin(uid)
                    _send(chat_id, "✅ Runner set to *auto* — next deploy picks the most free runner (490MB+ when fleet empty).\n\nHeavy video uploads will land on the emptiest box, not on a 180MB-loaded one.", reply_markup=_queen_panel_kb())
                    try:
                        _send(chat_id, _queen_runner_text(user), reply_markup=_queen_runner_kb(user))
                    except Exception:
                        pass
                elif val.startswith("id:"):
                    try:
                        rid = int(val[3:])
                        # map id -> url
                        chosen = None
                        for n in runner_client.managed_runner_nodes():
                            if int(n.get("id")) == rid:
                                chosen = n.get("url")
                                break
                        if not chosen:
                            # fallback: runner_pool index
                            pool = runner_client.runner_pool()
                            if 0 <= rid < len(pool):
                                chosen = pool[rid]
                        if chosen and runner_client.set_queen_pin(uid, chosen, exclusive=1):
                            _send(chat_id, f"✅ Pinned to `{chosen}` — exclusive 512MB (no other user will be placed there).\nVideo upload/download runs alone here.\n\nSend a zip or `/import` now.", reply_markup=_queen_panel_kb())
                        else:
                            _send(chat_id, "❌ Could not pin to that runner — it may be offline or disabled.")
                    except Exception as exc:
                        _send(chat_id, f"❌ Could not pin: {exc}")
                else:
                    _send(chat_id, _queen_runner_text(user), reply_markup=_queen_runner_kb(user))
        else:
            _send(chat_id, _queen_panel_text(user), reply_markup=_queen_panel_kb())
    elif action == "ping":
        if ref == "health":
            # cmd_health wants a user for the admin branch; None is fine for
            # the public platform-health path.
            cmd_health(chat_id, user or {})
        else:
            handle_ping(chat_id, "/ping")
    elif action == "latest":
        if not ref:
            # "latest:" with nothing after it is the 👑 panel's button: show
            # every repo app and which ones are behind, exactly like /latest.
            cmd_latest(chat_id, user)
            return
        row = bot_ops.find_app(user["id"], ref)
        if not row:
            _send(chat_id, "❌ That app is not yours or no longer exists.")
        elif not (row.get("repo_url") or "").strip():
            _send(chat_id, f"❌ *{row['name']}* wasn't deployed from a repo, so there "
                           f"is no commit to pull. `/update {row['name']}` replaces "
                           f"its code.")
        else:
            _update_one_app(chat_id, user, row)
    elif action in ("latestmenu", "latestsrc"):
        row = bot_ops.find_app(user["id"], ref)
        if not row or not (row.get("repo_url") or "").strip():
            _send(chat_id, "❌ That app is not yours, or it didn't come from a repo.")
        elif action == "latestmenu":
            _latest_menu(chat_id, user, row)
        else:
            _latest_src_start(chat_id, user, row)
    elif action == "latestbr":
        pend = _get_pending(chat_id)
        try:
            branch = (pend or {}).get("branches", [])[int(ref)]
        except (ValueError, IndexError):
            branch = ""
        if not pend or pend.get("mode") != "latest_src" or not branch:
            _send(chat_id, "That choice expired — `/latest` to start again.")
        else:
            _latest_src_apply(chat_id, pend, "#" + branch)
    elif action == "latestcancel":
        _pending.pop(chat_id, None)
        _send(chat_id, "❎ Cancelled.")
    elif action == "autodep":
        row = bot_ops.find_app(user["id"], ref)
        if not row:
            _send(chat_id, "❌ That app is not yours or no longer exists.")
        elif not _user_is_queen(user):
            _send(chat_id, "👑 Auto-deploy is queen access — ask the bot owner if you need it. "
                           "`/latest name` updates any repo app by hand.")
        elif not (row.get("repo_url") or "").strip():
            _send(chat_id, f"❌ *{row['name']}* wasn't deployed from a repo, so there "
                           f"is no branch for it to follow.")
        else:
            toggle_autodeploy(chat_id, user, row)
    elif action == "pick":
        # A button from a scanned repo list. The index resolves against the list
        # THIS chat was shown: another chat's list, or one that has expired, is
        # not reachable by guessing a number.
        if ref == "no":
            _send(chat_id, "✖️ Nothing was deployed.")
            return
        state = _take_pick(chat_id, ref)
        if not state:
            _send(chat_id, "⌛ That list has expired — send `/projects` (or "
                           "`/import x`) again and I'll show it once more.")
            return
        if ref == "readme":
            _send_repo_readme(chat_id, state["owner"], state["repo"],
                              state.get("branch") or "")
            return
        deploy_pick(chat_id, user, state, whole=(ref == "all"))


def _send_job_data(chat_id, user, ref):
    """Upload the app's data file (SQLite/JSON) to the chat."""
    app = bot_ops.find_app(user["id"], ref)
    if not app:
        _send(chat_id, "❌ That app is not yours or no longer exists.")
        return
    rid = app.get("runner_job_id")
    if not rid:
        _send(chat_id, "❌ That app was never deployed.")
        return
    try:
        r = runner_client._runner_http("GET", f"/internal/jobs/{rid}",
                                       worker=bot_ops._worker_of(app))
        jdir = (r.json() or {}).get("dir") or ""
    except Exception:
        _send(chat_id, "❌ The worker did not answer.")
        return
    if not jdir or not os.path.isdir(jdir):
        # Remote workers do not share a filesystem with this process, so the
        # path is only readable in the embedded/single-service layout. Say so
        # instead of reporting "no database".
        _send(chat_id, "📭 Data files are not reachable from here — "
                       "download them from the dashboard.")
        return
    best, best_size = None, -1
    for root, dirs, files in os.walk(jdir):
        dirs[:] = [d for d in dirs if d not in
                   ("__pycache__", ".git", "node_modules", "pylibs", ".cache")]
        for fn in files:
            if not fn.lower().endswith((".db", ".sqlite", ".sqlite3", ".json")):
                continue
            fp = os.path.join(root, fn)
            try:
                sz = os.path.getsize(fp)
            except OSError:
                continue
            if sz > best_size:
                best, best_size = fp, sz
    if not best:
        _send(chat_id, "📭 No data file yet — the app has not created one.")
        return
    if best_size > TG_MAX_UPLOAD_BYTES:
        _send(chat_id, f"❌ `{os.path.basename(best)}` is "
                       f"{best_size // (1024 * 1024)}MB — over Telegram's 50MB "
                       f"limit. Download it from the dashboard.")
        return
    _send_document(chat_id, best,
                   caption=f"📥 {os.path.basename(best)} ({best_size} bytes)")


# ==================== UPDATE DISPATCH ====================
def _command_parts(text):
    """Return Telegram command + argument; accept /cmd@BotName in groups."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return "", ""
    head, _, arg = text.partition(" ")
    command = head.split("@", 1)[0].lower()
    return command, arg.strip()


def _row_id(row):
    try:
        return row["id"] if row else None
    except (KeyError, TypeError):
        return None


def handle_update(upd):
    """Dispatch one Telegram update and always leave an analytics record."""
    event = {"chat_id": "", "event_type": "unknown", "command": "",
             "payload": "", "outcome": "ok", "error": "",
             "display_name": "", "telegram_user_id": None, "user_id": None}
    try:
        if "message" in upd:
            msg = upd["message"]
            chat_id = msg["chat"]["id"]
            tg_uid_early = (msg.get("from") or {}).get("id")
            if telegram_admin_ext.is_banned(tg_uid_early):
                # Silent drop — no reply at all. A banned id gets nothing to
                # probe with, not even an error message confirming the bot
                # is listening.
                event.update(chat_id=chat_id, event_type="banned", outcome="refused",
                             telegram_user_id=tg_uid_early)
                return
            text = msg.get("text", "") or ""
            command, arg = _command_parts(text)
            event.update(chat_id=chat_id,
                         event_type="command" if command else
                                    ("document" if "document" in msg else "message"),
                         command=command, payload=arg if command else "",
                         display_name=_tg_display(msg),
                         telegram_user_id=msg.get("from", {}).get("id"))
            linked = telegram_link.user_for_chat(chat_id)
            event["user_id"] = _row_id(linked)

            if command and not _is_admin(linked, tg_uid_early) and _rate_limited(chat_id):
                _send(chat_id, f"⏳ একটু wait করো ({int(_CMD_RATE_WINDOW_S)}s)")
                event.update(outcome="rate_limited")
                return

            if command == "/cancel" and chat_id in _admin_flow:
                _admin_flow.pop(chat_id, None)
                _send(chat_id, "Cancelled.")
                return
            if not command and chat_id in _admin_flow:
                # jobedit can take a document; broadcast can take photo/video/document with caption.
                state = _admin_flow.get(chat_id) or {}
                if (state.get("flow") == "jobedit"
                        and "document" in msg
                        and not text.strip()):
                    code, _lang, _reqs, _note, err = _download_document(msg["document"])
                    if err:
                        _send(chat_id, f"❌ {err}")
                        return
                    text = code or ""
                # Broadcast media capture — admin sends photo/video with caption as broadcast message
                if state.get("flow") == "broadcast" and not text.strip():
                    # check media
                    media_type = None
                    media_file_id = None
                    caption = (msg.get("caption") or "").strip()
                    if "photo" in msg and msg["photo"]:
                        photos = msg["photo"]
                        try:
                            # largest last
                            media_file_id = photos[-1].get("file_id") if isinstance(photos, list) else None
                            media_type = "photo" if media_file_id else None
                        except Exception:
                            media_type = None
                    elif "video" in msg and isinstance(msg.get("video"), dict):
                        media_file_id = msg["video"].get("file_id")
                        media_type = "video" if media_file_id else None
                    elif "document" in msg and isinstance(msg.get("document"), dict):
                        media_file_id = msg["document"].get("file_id")
                        media_type = "document" if media_file_id else None
                    elif "animation" in msg and isinstance(msg.get("animation"), dict):
                        media_file_id = msg["animation"].get("file_id")
                        media_type = "animation" if media_file_id else None
                    if media_type and media_file_id:
                        # stash media in flow extra, use caption as message (fallback if empty)
                        if not caption:
                            caption = "(media)"
                        # store for later steps
                        state.setdefault("extra", {})["broadcast_media_type"] = media_type
                        state.setdefault("extra", {})["broadcast_media_file_id"] = media_file_id
                        text = caption
                if text.strip() and _advance_admin_flow(chat_id, text):
                    return

            # A pending upload/text is claimed before normal non-command input.
            # "/skip" is the one command-shaped exception: it's only ever
            # meaningful as the answer to the requirements step below, so it
            # must reach handle_pending_code instead of falling through to
            # "unknown command" like every other slash-word would.
            pending_now = _get_pending(chat_id)
            skipping = command == "/skip" and pending_now and pending_now.get("step") == "requirements"
            if not command or skipping:
                pending = pending_now
                if pending:
                    event["event_type"] = "code_upload"
                    event["payload"] = str(pending.get("name") or pending.get("ref") or "")
                    handle_pending_code(chat_id, msg, pending)
                    return
                if "document" in msg:
                    _send(chat_id, "I wasn't expecting a file — send "
                                   "`/code myapp` or `/update myapp` first, "
                                   "then the file.")
                else:
                    _send(chat_id, UNKNOWN_REPLY, reply_markup=_open_kb())
                return

            def gated(fn):
                user = _require_link(chat_id)
                if not user:
                    event["outcome"] = "refused"
                    return
                event["user_id"] = _row_id(user)
                # "typing…" BEFORE the work starts, not after it finishes. These
                # commands talk to a runner and to GitHub — seconds, not
                # milliseconds — and a chat with no reaction in it for three
                # seconds reads as a bot that died, so the command gets sent
                # again. One indicator costs one request.
                if command in _SLOW_COMMANDS:
                    _typing(chat_id)
                fn(user)

            handlers = {
                "/start": lambda: handle_start(chat_id, _tg_display(msg) or
                                                 msg.get("from", {}).get("first_name", "user"), arg),
                "/link": lambda: handle_link(chat_id, text, _tg_display(msg)),
                "/unlink": lambda: handle_unlink(chat_id),
                "/ping": lambda: gated(lambda _u: handle_ping(chat_id, text)),
                # 🩺 the same question /ping asks, from inside the process — so
                # a free-tier cold start or a blocked round trip cannot make the
                # platform look dead when it is not.
                "/health": lambda: gated(lambda u: cmd_health(chat_id, u)),
                # NOT gated: "what is my id?" is asked before an account is
                # linked, and it is the number an admin needs to grant one.
                "/id": lambda: cmd_id(chat_id, msg.get("from") or {}),
                "/whoami": lambda: cmd_id(chat_id, msg.get("from")),
                "/me": lambda: cmd_id(chat_id, msg.get("from")),
                "/web": lambda: cmd_web(chat_id),
                "/webapp": lambda: cmd_web(chat_id),
                "/cancel": lambda: cmd_cancel_pending(chat_id),
                "/apps": lambda: gated(lambda u: cmd_apps(chat_id, u)),
                "/jobs": lambda: gated(lambda u: cmd_apps(chat_id, u)),
                "/list": lambda: gated(lambda u: cmd_apps(chat_id, u)),
                "/status": lambda: gated(lambda u: cmd_status(chat_id, u, arg)),
                "/uptime": lambda: gated(lambda u: cmd_status(chat_id, u, arg)),
                "/info": lambda: gated(lambda u: cmd_status(chat_id, u, arg)),
                "/logs": lambda: gated(lambda u: cmd_logs(chat_id, u, arg)),
                "/env": lambda: gated(lambda u: cmd_env(chat_id, u, arg)),
                "/backup": lambda: gated(lambda u: cmd_backup(chat_id, u, arg)),
                "/history": lambda: gated(lambda u: cmd_history(chat_id, u, arg)),
                "/restart": lambda: gated(lambda u: cmd_restart(chat_id, u, arg)),
                "/stop": lambda: gated(lambda u: cmd_stop(chat_id, u, arg)),
                "/delete": lambda: gated(lambda u: cmd_delete(chat_id, u, arg)),
                "/rename": lambda: gated(lambda u: cmd_rename(chat_id, u, arg)),
                "/source": lambda: gated(lambda u: cmd_source(chat_id, u, arg)),
                "/code": lambda: gated(lambda u: cmd_code_start(chat_id, u, arg)),
                "/update": lambda: gated(lambda u: cmd_update_start(chat_id, u, arg)),
                "/import": lambda: gated(lambda u: cmd_import(chat_id, u, arg)),
                # 👑 the catalogue of ready-made projects, with the steps and a
                # one-tap deploy. cmd_projects explains to a non-queen what they
                # are missing instead of staying silent.
                "/projects": lambda: gated(lambda u: cmd_projects(chat_id, u, arg)),
                # Repo deploys, the two halves of a Render-style workflow:
                # "is there a newer commit?" (/latest, everyone) and "keep it
                # current without me" (/autodeploy, 👑).
                "/latest": lambda: gated(lambda u: cmd_latest(chat_id, u, arg)),
                "/autodeploy": lambda: gated(lambda u: cmd_autodeploy(chat_id, u, arg)),
                # /limits is the plain-language answer to "what am I allowed?"
                # and, for a 👑 account, the door to their panel.
                "/limits": lambda: gated(lambda u: cmd_limits(chat_id, u)),
                "/runner": lambda: gated(lambda u: cmd_queen_runner(chat_id, u)),
                "/myrunner": lambda: gated(lambda u: cmd_queen_runner(chat_id, u)),
                "/admin": lambda: cmd_admin(chat_id, msg.get("from", {}).get("id"), arg),
                "/details": lambda: cmd_details(chat_id, msg.get("from", {}).get("id"), arg),
                "/zip": lambda: cmd_admin_short_toggle(chat_id, msg.get("from", {}).get("id"), arg, "allowzip"),
                "/unzip": lambda: cmd_admin_short_toggle(chat_id, msg.get("from", {}).get("id"), arg, "denyzip"),
                "/see": lambda: cmd_see(chat_id, msg.get("from", {}).get("id"), arg),
                "/user": lambda: cmd_user(chat_id, msg.get("from", {}).get("id"), arg),
                # 👑 admin-only, and NOT behind gated(): /queen has to work for
                # an admin who never ran /link, exactly like /admin and /see.
                # cmd_queen re-checks _is_admin itself and stays silent otherwise.
                "/queen": lambda: cmd_queen(chat_id, msg.get("from", {}).get("id"), arg),
                "/unqueen": lambda: cmd_queen(chat_id, msg.get("from", {}).get("id"),
                                              f"off {arg}".strip()),
                # Admin shortcuts that match the inline buttons 1:1
                "/runners": lambda: cmd_admin(chat_id, msg.get("from", {}).get("id"), "runners"),
                "/recover": lambda: cmd_admin(chat_id, msg.get("from", {}).get("id"), "recover"),
                "/rescue": lambda: cmd_admin(chat_id, msg.get("from", {}).get("id"), "recover"),
                "/fleet": lambda: cmd_admin(chat_id, msg.get("from", {}).get("id"), "runners"),
                "/health": lambda: _cmd_health_smart(chat_id, msg.get("from", {}).get("id")),
                "/overview": lambda: cmd_admin(chat_id, msg.get("from", {}).get("id"), "overview"),
                "/signups": lambda: cmd_admin(chat_id, msg.get("from", {}).get("id"),
                                              f"signups {arg}".strip()),
                "/bans": lambda: cmd_admin(chat_id, msg.get("from", {}).get("id"), "bans"),
                "/audit": lambda: cmd_admin(chat_id, msg.get("from", {}).get("id"), "audit"),
                "/help": lambda: handle_start(chat_id, _tg_display(msg) or
                                                msg.get("from", {}).get("first_name", "user")),
                "/guide": lambda: _cmd_guide(chat_id, arg),
                "/token": lambda: cmd_token_tips(chat_id),
                "/commands": lambda: cmd_commands(chat_id, telegram_link.user_for_chat(chat_id),
                                                  msg.get("from", {}).get("id")),
                "/cmds": lambda: cmd_commands(chat_id, telegram_link.user_for_chat(chat_id),
                                              msg.get("from", {}).get("id")),
                "/guides": lambda: _cmd_guide(chat_id, arg),
                "/howto": lambda: _cmd_guide(chat_id, arg),
            }
            handler = handlers.get(command)
            if handler:
                handler()
            else:
                event["outcome"] = "unknown"
                _send(chat_id, UNKNOWN_REPLY, reply_markup=_open_kb())

        elif "callback_query" in upd:
            cb = upd["callback_query"]
            # The message a button lives on can be MISSING: Telegram keeps
            # delivering taps for a message it has already dropped from the chat
            # and sends an "inaccessible_message" stub for old ones. Reading
            # cb["message"]["chat"]["id"] straight through then raised a
            # TypeError inside this branch, the tap went unanswered, and the
            # button looked dead — one more way "the inline buttons don't work"
            # happens without any bug in the buttons themselves. The person who
            # tapped is always known, and in a private chat their id IS the chat.
            msg_cb = cb.get("message") or {}
            chat_id = ((msg_cb.get("chat") or {}).get("id")
                       or (cb.get("from") or {}).get("id"))
            data = str(cb.get("data") or "")
            tg_uid_cb = cb.get("from", {}).get("id")
            if telegram_admin_ext.is_banned(tg_uid_cb):
                try:
                    _tg("answerCallbackQuery", callback_query_id=cb["id"])
                except Exception:
                    pass
                event.update(chat_id=chat_id, event_type="banned", outcome="refused",
                             telegram_user_id=tg_uid_cb)
                return
            linked = telegram_link.user_for_chat(chat_id) if chat_id is not None else None
            event.update(chat_id=chat_id if chat_id is not None else "",
                         event_type="callback",
                         command=data.partition(":")[0], payload=data.partition(":")[2],
                         display_name=_tg_display(msg_cb),
                         telegram_user_id=cb.get("from", {}).get("id"),
                         user_id=_row_id(linked))
            # Telegram requires answerCallbackQuery within ~3s or the
            # button sits in a spinner / looks unresponsive on the user's
            # phone. We now answer *immediately* before doing any work
            # (runner health, DB lookups) so the spinner stops instantly;
            # work still happens after. The old code only answered AFTER
            # handle_callback, so a slow runner probe (18s for 6 runners)
            # kept the button spinning the whole time — that's the
            # "inline buttons take long" bug.
            try:
                try:
                    _tg("answerCallbackQuery", callback_query_id=cb["id"])
                except Exception:
                    pass
                # admin: buttons check _is_admin() themselves and were never
                # meant to require a linked CodeNest account — but this
                # `if linked` gate (meant for job-action buttons like
                # restart/stop, which DO need one) ran first and silently
                # ate every admin button press for an admin who hadn't run
                # /link, with no error message at all. That's exactly what
                # made retyping /admin look like the only thing that worked.
                if chat_id is None:
                    event["outcome"] = "refused"
                elif linked or data.startswith(("admin:", "queen:", "qproj:",
                                                  "ping:", "help:")):
                    # ping: and help:import do not need a linked account — the
                    # same way /ping and /id work before /link. Without this
                    # allowlist an unlinked tap was answered with "link first"
                    # and looked exactly like "the button is broken".
                    handle_callback(chat_id, data, msg_cb.get("message_id"))
                else:
                    # A button that needs an account, pressed from a chat that
                    # has none (someone tapped /unlink, or a button outlived the
                    # link). Silence here is indistinguishable from a broken
                    # button, so the tap is answered and the reason is said.
                    event["outcome"] = "refused"
                    _send(chat_id, "This chat isn't linked to an account right now. "
                                   "Send `/link`, pick your account, and the buttons "
                                   "will work.")
            except Exception as cb_exc:
                event["outcome"] = "error"
                event["error"] = f"{type(cb_exc).__name__}: {cb_exc}"
                logger.exception("callback %s failed", data)
                # answer quietly (no full-screen alert). A show_alert popup that
                # says "Something went wrong" made every admin button look dead
                # even when the real work partly succeeded or was a one-off blip.
                try:
                    _tg("answerCallbackQuery", callback_query_id=cb["id"],
                        text="Couldn't finish — try again.", show_alert=False)
                except Exception:
                    logger.exception("Could not even answer the callback query")
                try:
                    if chat_id is not None:
                        _send(chat_id,
                              "⚠️ That button didn't finish. "
                              "Send the command again, or `/admin` if you were in the panel.")
                except Exception:
                    pass
                # Do NOT re-raise: webhook must return 200 so Telegram stops retrying
                # the same broken update in a loop.
    except Exception as exc:
        event["outcome"] = "error"
        event["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        try:
            if event["chat_id"] != "":
                bot_analytics.record(**event)
        except Exception:
            # A monkeypatched/broken recorder still cannot replace the command result.
            logger.exception("Bot analytics recorder failed")


# ==================== MAIN LOOP ====================
import hashlib
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from services import telegram_webhook_queue

# secret_token Telegram echoes back in the X-Telegram-Bot-Api-Secret-Token
# header on every webhook delivery — derived from BOT_TOKEN itself so no
# new secret has to be generated, stored, or set as an env var.
_WEBHOOK_SECRET = hashlib.sha256((BOT_TOKEN or "unset").encode()).hexdigest()[:32]

webhook_router = APIRouter()
_WEBHOOK_UPDATE_LEASE_NS = telegram_webhook_queue.LEASE_NS
_WEBHOOK_UPDATE_KEEP = telegram_webhook_queue.KEEP_UPDATES
_WEBHOOK_UPDATE_REUSE_NS = telegram_webhook_queue.REUSE_AFTER_NS


def _claim_webhook_update(update_id: int, payload=None):
    """Compatibility wrapper around the durable webhook queue's claim."""
    return telegram_webhook_queue.claim_update(update_id, payload)


def _complete_webhook_update(update_id: int, claim: int) -> None:
    telegram_webhook_queue.complete_update(update_id, claim)


@webhook_router.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """WHY THIS EXISTS: this bot used to ONLY long-poll (see poll_loop
    below) — getUpdates in a background thread inside the same web
    process. On a host that suspends the whole process after a period of
    no INCOMING HTTP traffic (Render's free tier does exactly this), the
    polling thread dies right along with everything else, because polling
    only ever makes OUTBOUND requests — it never gives the platform a
    reason to consider the service "active". Button presses queue up on
    Telegram's side with nobody fetching them, and the service only wakes
    up again when something else happens to hit an HTTP endpoint — which
    is why 10-20 taps could go nowhere and then one would suddenly land.

    A webhook flips the direction: Telegram POSTs the update TO this
    endpoint. That POST *is* incoming HTTP traffic, so it wakes a sleeping
    dyno itself. The update is saved in the site's database before the fast
    acknowledgement; a worker handles it afterward and resumes pending work
    after a process restart.
    """
    got = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if got != _WEBHOOK_SECRET:
        return {"ok": False}  # not from Telegram (or a stale secret) — drop it
    try:
        upd = await request.json()
    except Exception:
        return {"ok": False}
    # Store the update in the site's database before acknowledging it. The
    # handler runs on the queue's worker, so runner/GitHub calls cannot consume
    # Telegram's webhook timeout; a process restart can still resume the saved
    # payload after its short claim lease expires.
    queued = telegram_webhook_queue.enqueue(upd, handle_update)
    if queued == "unavailable":
        # No durable write means we must not acknowledge it; Telegram will retry.
        return JSONResponse(status_code=503, content={"ok": False})
    if queued == "invalid":
        return JSONResponse(status_code=400, content={"ok": False})
    return {"ok": True}


def enable_webhook():
    """Point Telegram at telegram_webhook instead of polling. Call this
    from start_bot INSTEAD OF starting poll_loop's thread — running both
    at once makes Telegram reject getUpdates with 409 "webhook is active"
    (see poll_loop's own self-healing comment for that exact failure)."""
    if not BOT_TOKEN or not SITE_BASE:
        logger.error("Cannot enable webhook: BOT_TOKEN or SITE_BASE is not set.")
        return False
    url = f"{SITE_BASE.rstrip('/')}/telegram/webhook"
    # allowed_updates has two accepted spellings and the docs call it a
    # "JSON-serialized list", which is ambiguous for a JSON body: a real array,
    # or the string form a urlencoded request needs. Guessing wrong makes
    # Telegram reject setWebhook, this service falls back to polling, and on a
    # host that sleeps between requests button presses queue up on Telegram's
    # side with nobody fetching them — "the inline buttons don't work", behind a
    # green deploy. So both are offered and whichever Telegram accepts wins.
    wanted = ["message", "callback_query"]
    res = {}
    for form in (json.dumps(wanted), wanted):
        res = _tg("setWebhook", url=url, secret_token=_WEBHOOK_SECRET,
                  allowed_updates=form)
        if (res or {}).get("ok"):
            logger.warning("TELEGRAM: webhook registered at %s (allowed_updates "
                           "sent as a %s) — polling is NOT started.", url,
                           "JSON string" if isinstance(form, str) else "array")
            return True
    logger.error("TELEGRAM: setWebhook failed both ways: %s", res)
    return False


def poll_loop():
    if not BOT_TOKEN:
        return
    print("🤖 Advanced Bot starting...")
    offset = 0

    _fail_streak = 0
    _webhook_cleared = False

    while True:
        try:
            updates = _tg("getUpdates", offset=offset, timeout=40)

            # A REJECTED getUpdates USED TO BE INVISIBLE.
            #
            # This branch was `time.sleep(1); continue` with no logging at
            # all, and it is the branch Telegram takes for the two failures
            # that actually stop a bot dead:
            #
            #   409 "can't use getUpdates method while webhook is active"
            #       — someone (often a hosting UI or an old deploy) left a
            #         webhook registered on this token. Polling can NEVER
            #         work until it is deleted.
            #   409 "terminated by other getUpdates request"
            #       — a second instance is polling the same token: a stale
            #         Render service, or a local run left open. The two
            #         steal each other's updates and both look broken.
            #
            # Reproduced against a fake Telegram API: in both cases the loop
            # spun forever, wrote NOTHING to the log, and the bot answered no
            # message. From the outside "the bot is dead" — with a healthy
            # /health endpoint and a green deploy.
            if not updates or not updates.get("ok"):
                _fail_streak += 1
                desc = str((updates or {}).get("description") or
                           "no response from Telegram")

                if "webhook is active" in desc.lower() and not _webhook_cleared:
                    # Self-heal, once. This service polls; a webhook on the
                    # same token is always wrong for it, and deleting it is
                    # the documented remedy. Once only, so a genuine webhook
                    # deployment is not fought over in a loop.
                    _webhook_cleared = True
                    logger.error(
                        "TELEGRAM: a webhook is registered on this bot token, so "
                        "getUpdates is refused and the bot receives NOTHING. "
                        "Deleting it automatically (this service polls).")
                    res = _tg("deleteWebhook", drop_pending_updates=False)
                    if (res or {}).get("ok"):
                        logger.warning("TELEGRAM: webhook deleted — polling resumes.")
                        _fail_streak = 0
                        continue
                    logger.error("TELEGRAM: deleteWebhook failed: %s", res)

                elif "terminated by other getupdates" in desc.lower():
                    logger.error(
                        "TELEGRAM: another instance is polling this same bot "
                        "token — updates are being split between them and both "
                        "look broken. Run ONE service per token, or give this "
                        "deployment its own bot.")

                # Never silent again, but never a flood either: the first few
                # failures are logged, then one line a minute.
                elif _fail_streak <= 3 or _fail_streak % 60 == 0:
                    logger.error("TELEGRAM getUpdates failed (%d in a row): %s",
                                 _fail_streak, desc)

                # Back off so a hard failure is not a hot loop against
                # Telegram's API — the old code retried every second forever.
                time.sleep(min(2 * _fail_streak, 30))
                continue

            if _fail_streak:
                logger.warning("TELEGRAM: polling recovered after %d failures.",
                               _fail_streak)
                _fail_streak = 0

            for upd in updates.get("result", []):
                offset = upd["update_id"] + 1
                handle_update(upd)

        except Exception as e:
            print("Poll error:", e)
            time.sleep(3)


def start_bot():
    # Resume any pending rich broadcasts before other subsystems start
    try:
        from services import pingbot_broadcast as _pb
        _pb.ensure_broadcast_schema()
        cnt = _pb.resume_pending_broadcasts()
        if cnt:
            logger.warning("Resumed %d pending broadcast(s) after restart.", cnt)
    except Exception as exc:
        logger.warning("broadcast resume skipped: %s", exc)
    if not BOT_TOKEN:
        print("TELEGRAM_PING_BOT_TOKEN not set")
        return

    try:
        telegram_webhook_queue.start(handle_update)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Telegram webhook queue did not start (%s)", type(exc).__name__)

    # ASK TELEGRAM WHO WE ARE, ONCE, AT BOOT.
    #
    # A token belonging to a different bot than the Mini App is opened from
    # produces bad_hash, and nothing in the running system could say so — the
    # only way to find out was to open the app, fail, read the log, and guess.
    # getMe answers it in one call at startup, so the fact is in the logs
    # before anyone tries to sign in.
    try:
        from services import miniapp_auth

        # TWO TOKEN NAMES, ONE WINNER, AND NOTHING SAID WHICH. BOT_TOKEN
        # silently outranks TELEGRAM_PING_BOT_TOKEN — but render.yaml only
        # documents the latter, so an owner who replaced their bot by editing
        # the documented name, while a stale BOT_TOKEN sat above it, kept
        # running the OLD bot and had no way to see that. Reproduced.
        src = miniapp_auth.token_sources()
        if src.get("conflict"):
            logger.error(
                "TWO DIFFERENT BOT TOKENS ARE CONFIGURED: %s. Only %s is used "
                "(it takes priority), so every Mini App sign-in is checked "
                "against bot %s. If that is not the bot you are opening, "
                "DELETE the unused variable and redeploy.",
                ", ".join(f"{k}=bot {v}" for k, v in src["bot_ids"].items()),
                src["used"], src["bot_ids"].get(src["used"]))

        who = miniapp_auth.whoami()
        if who.get("ok"):
            # PRINT THE FINGERPRINT AT BOOT, so the value this process is
            # actually running with can be compared against BotFather BEFORE
            # anyone fails to sign in. Every diagnosis so far had to wait for
            # a user to hit the error and then reason backwards; this puts the
            # decisive fact in the deploy log. It is a one-way digest — the
            # secret is never written anywhere.
            fp = miniapp_auth.token_fingerprint()
            logger.warning(
                "TELEGRAM BOT: this server is @%s (id %s), token sha256:%s "
                "(secret %d chars). The Mini App must be opened from THIS bot. "
                "To confirm the deployed token matches BotFather, run: "
                "printf '%%s' '<token>' | sha256sum  — the first 12 characters "
                "must equal %s.",
                who.get("username"), who.get("bot_id"), fp.get("sha256_12"),
                fp.get("secret_length", 0), fp.get("sha256_12"))
            # A truncated or partially-selected paste is invisible in a
            # dashboard field and produces exactly this failure, so measure it.
            _sl = fp.get("secret_length")
            if _sl and _sl != fp.get("secret_length_expected"):
                logger.error(
                    "TELEGRAM TOKEN LOOKS TRUNCATED: the secret half is %d "
                    "characters, expected %d. A partial paste still passes "
                    "getMe in some cases but can never verify a Mini App "
                    "sign-in. Re-copy the whole token.",
                    _sl, fp.get("secret_length_expected"))
            env_name = os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")
            if env_name and env_name.lower() != (who.get("username") or "").lower():
                logger.error(
                "TELEGRAM MISCONFIGURED: TELEGRAM_BOT_USERNAME is @%s but the "
                    "token belongs to @%s. These must be the same bot.",
                    env_name, who.get("username"))
        else:
            logger.error(
                "TELEGRAM TOKEN REJECTED by getMe (%s). Sign-in will fail until "
                "BOT_TOKEN is a valid token: %s",
                who.get("reason"), who.get("detail", ""))
    except Exception as exc:  # noqa: BLE001
        logger.warning("bot identity check skipped: %s", exc)

    # Register the persistent Mini App button before polling starts, so the
    # entry point exists even for a user who never sends a command.
    try:
        set_menu_button()
    except Exception as exc:  # noqa: BLE001
        print("menu button registration failed:", exc)
    t = threading.Thread(target=poll_loop, daemon=True)
    if enable_webhook():
        # Webhook registered — do NOT also start polling. Telegram refuses
        # getUpdates with 409 "webhook is active" the moment both are
        # attempted on the same token (see poll_loop's own comment on that
        # exact failure), so this is an either/or, never both.
        print("✅ Advanced Bot started (webhook mode)")
    else:
        # Only reached if SITE_BASE/BOT_TOKEN are missing or Telegram
        # rejected setWebhook — falling back to the old polling behaviour
        # so the bot still works, just with the slow-wake problem webhook
        # mode exists to fix.
        logger.warning("TELEGRAM: falling back to polling — buttons/messages "
                       "may be slow to respond on a host that sleeps between "
                       "requests. Fix SITE_BASE/BOT_TOKEN and redeploy to use "
                       "the webhook instead.")
        t.start()
        print("✅ Advanced Bot started (polling mode, fallback)")
    threading.Thread(target=_admin_notify_loop, daemon=True).start()