"""Everything the Telegram bot does to a user's apps, on the SAME rails the
website uses.

WHY THIS EXISTS
---------------
services/pingbot.py called the runner directly. Measured:

    bot deploys a job  ->  rows in jobs table        : 0
                           jobs visible in /admin/jobs: 0
                           runner actually running    : 1

So a bot-deployed app burned real memory while being invisible to the admin
console, exempt from MAX_JOBS_PER_USER, and unable to appear in the owner's own
dashboard. Two deploy paths meant two sets of rules, and only one of them was
enforced.

Everything here goes through the jobs table and runner_client, so an app acted
on from Telegram behaves exactly as it does on the site — same worker routing,
same admin visibility.

CREATION AND CODE EDITING WERE ONCE REMOVED FROM HERE, AND ARE NOW BACK — READ
THIS BEFORE TOUCHING create_app() / update_code().
deploy() and update_code() used to live here while the bot accepted pasted
snippets directly in chat; both were deleted for two reasons: (1) a Telegram
TEXT message caps at ~4096 characters with no editor, so pasted code could
only ever be a toy script, and (2) pingbot.py's code-collection path ran
BEFORE the account-link check existed, so an unlinked stranger's chat could
execute code on the server (reproduced: os.system('whoami') ran unauthenticated).

/code and /update are back for a genuine, requested use case — pushing a fix
from a phone without opening the site — but neither weakness above is allowed
to return:
  · Every command that reaches create_app()/update_code() is gated on
    services.telegram_link.user_for_chat() in pingbot.py's dispatcher, same
    as /restart or /delete. There is no code path here that skips it.
  · Code can arrive as an uploaded FILE (Telegram bot API allows up to 20MB
    on download), not just a 4096-char text message, so a real app's source
    can actually make the trip. Plain-text paste still works too, for quick
    one-line fixes, and still caps out at ~4096 characters — long edits
    should be sent as a file.
  · create_app() and update_code() call the exact same jobs-table insert and
    runner_client calls as POST /api/jobs and PATCH /api/jobs/{id} below —
    same MAX_JOBS_PER_USER cap, same admin visibility. There is still only
    ONE set of rules, just triggered from two UIs now instead of one.
"""
import json
import logging
import os
import re

from database import get_db_connection
from routes.deps import now_utc_str
from services import runner_client
from services import secrets_store
# Re-exported: pingbot reads bot_ops.MAX_JOBS_PER_USER when showing how many

# slots an account has left, so there is one value, not two.
from services.runner_client import MAX_JOBS_PER_USER  # noqa: F401

logger = logging.getLogger("codenest-app")

# How many apps a 👑 account may run at once when no admin typed a specific
# number for it. Deliberately generous but not unlimited: the runner's shared-box
# admission check is what actually protects the machine, and a count that cannot
# be reached is a promise the box may not keep.
QUEEN_MAX_JOBS = int(os.getenv("QUEEN_MAX_JOBS", "10"))

def _effective_job_limit(user_id: int) -> int:
    """How many apps this account may have RUNNING at once.

    Three sources, in the order they should win:
      1. a per-user override an admin typed (`/admin limit <user> <n>`) — a
         specific instruction beats a general one;
      2. 👑 (users.mem_unlimited) — a queen account gets QUEEN_MAX_JOBS, because
         "no memory ceiling, big uploads, any repo" that still stops at three
         running apps reads as a privilege that was not actually granted;
      3. the global MAX_JOBS_PER_USER.
    NULL means "use the default that applies to this account".
    """
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT job_limit_override, mem_unlimited FROM users WHERE id = ?",
                           (user_id,)).fetchone()
    finally:
        conn.close()
    if row and row["job_limit_override"] is not None:
        return int(row["job_limit_override"])
    if row and row["mem_unlimited"]:
        return QUEEN_MAX_JOBS
    return MAX_JOBS_PER_USER


# Public aliases. The chat bot shows "x of y slots" and routes/runspace.py
# enforces the cap on the website; both must ask THIS function, because a second
# copy of the rule is how the two surfaces drift apart — the website used to
# check the global default and quietly ignored an admin's per-user override.
effective_job_limit = _effective_job_limit


def is_queen(user_id: int) -> bool:
    """Is this account granted 👑 (users.mem_unlimited)?

    One place to ask, so "queen" means the same thing in the chat bot, on the
    website and in recovery: no memory ceiling on its jobs, zip upload allowed,
    and the 👑 interface in the bot.
    """
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT mem_unlimited FROM users WHERE id = ?",
                           (user_id,)).fetchone()
    except Exception:
        return False
    finally:
        conn.close()
    return bool(row and row["mem_unlimited"])


def _mem_limit_for(user_id: int):
    """None (runner's global MAX_MEM_MB default) unless /queen granted this
    user mem_unlimited=1, in which case 0 tells the runner to skip the
    per-job RLIMIT entirely (see runner/app.py's mem_limit_mb).

    Public alias below: routes/runspace.py needs the SAME answer when a queen
    deploys from the web editor, and a second copy of this rule is how the two
    surfaces would drift apart."""
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT mem_unlimited FROM users WHERE id = ?",
                            (user_id,)).fetchone()
    finally:
        conn.close()
    return 0 if (row and row["mem_unlimited"]) else None


mem_limit_for = _mem_limit_for

# Zip bundles. The default is a source-sized upload; a 👑 account may send a
# whole project — assets, a vendored folder, a starter database. The runner
# still enforces its own hard ceiling (ZIP_BUNDLE_CEILING_*), so these numbers
# only decide what the site ASKS for on this account's behalf.
ZIP_MAX_MB = 5  # hard-coded (no env var) — queen gets the big box, see queen_runner_pins
ZIP_MAX_FILES = 500
QUEEN_ZIP_MAX_MB = 150  # heavy video projects need real room (runner ceiling is 200MB)
QUEEN_ZIP_MAX_FILES = 8000


def account_privileges(user_id: int) -> dict:
    """Everything a UI needs about what this account is allowed, in one call.

    The dashboard's bots list and the chat bot's /apps both show the 👑 flag,
    the running-app limit and the upload size at once; asking three separate
    functions for those is how a surface ends up disagreeing with itself (one
    reads the flag, another reads the override, a third guesses).
    """
    queen = is_queen(user_id)
    return {"is_queen": queen,
            "job_limit": _effective_job_limit(user_id),
            "mem_limit_mb": _mem_limit_for(user_id),
            "zip_max_mb": QUEEN_ZIP_MAX_MB if queen else ZIP_MAX_MB,
            "zip_max_files": QUEEN_ZIP_MAX_FILES if queen else ZIP_MAX_FILES}


def zip_limits_for(user_id: int) -> dict:
    """The zip fields to send to the runner for this account.

    One function so a 👑 grant means the same thing on every upload path
    (create, update, and the re-create after a 404) instead of three places
    that can drift.
    """
    if is_queen(user_id):
        return {"zip_max_mb": QUEEN_ZIP_MAX_MB, "zip_max_files": QUEEN_ZIP_MAX_FILES}
    return {"zip_max_mb": ZIP_MAX_MB, "zip_max_files": ZIP_MAX_FILES}


def reapply_mem_limit(user_id: int) -> int:
    """Push the CURRENT /queen flag to this user's already-running bots.

    WHY THIS EXISTS: the runner stores a job's RLIMIT when the job is created,
    so granting or revoking 👑 changed only FUTURE deploys. The bot that was
    being OOM-killed right now kept its old ceiling until its owner happened to
    redeploy — which reads exactly like "/queen did nothing". One PATCH per
    running job closes that: the runner re-spawns the process with the new
    limit and the job's directory (database.db, session.json, data/) is
    untouched, same as any in-place update.

    Returns how many bots were re-limited. Best-effort: a bot on a runner that
    is asleep is skipped, not an error — it picks the flag up on its next
    deploy or cold start anyway.
    """
    limit = _mem_limit_for(user_id)
    conn = get_db_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, name, runner_job_id, worker_url FROM jobs "
            "WHERE user_id = ? AND runner_job_id IS NOT NULL "
            "AND desired_state != 'stopped'", (user_id,)).fetchall()]
    finally:
        conn.close()

    done = 0
    for row in rows:
        try:
            resp = runner_client._runner_http(
                "PATCH", f"/internal/jobs/{row['runner_job_id']}",
                {"mem_limit_mb": limit}, worker=row.get("worker_url"))
            if resp.status_code == 200:
                done += 1
        except Exception as exc:
            logger.info("mem-limit reapply skipped for job %s: %s", row.get("id"), exc)
    return done


def slugify_name(raw: str) -> str:
    """A job name the site would also accept. Used by /rename."""
    s = re.sub(r"[^A-Za-z0-9 _-]+", "", (raw or "")).strip()
    s = re.sub(r"\s+", "-", s).strip("-")
    return s[:40]


def list_apps(user_id: int) -> list:
    """This account's apps, with live status from the worker holding each."""
    conn = get_db_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, name, language, runner_job_id, worker_url, desired_state, created_at, "
            "telegram_bot_username, repo_url, repo_entry, repo_commit, auto_deploy "
            "FROM jobs WHERE user_id = ? ORDER BY id DESC", (user_id,)
        ).fetchall()]
    finally:
        conn.close()
    live = runner_client.fleet_jobs()
    for r in rows:
        info = live.get(r.get("runner_job_id")) or {}
        r["status"] = info.get("status") or ("stopped" if r.get("desired_state")=="stopped" else "recovering")
        r["mem_mb"] = info.get("mem_mb")
        r["uptime_s"] = info.get("uptime_s")
        r["restarts"] = info.get("restarts")
        r["last_exit_reason"] = info.get("last_exit_reason")
        r["last_exit_code"] = info.get("last_exit_code")
        r["oom"] = info.get("oom")
    return rows


def find_app(user_id: int, ref: str) -> dict:
    """Resolve a name or numeric id to one of THIS user's apps.

    Scoped to user_id on purpose: a bot command must never be able to address
    someone else's job by guessing an id.
    """
    ref = (ref or "").strip()
    if not ref:
        return None
    conn = get_db_connection()
    try:
        row = None
        if ref.isdigit():
            row = conn.execute(
                "SELECT * FROM jobs WHERE user_id = ? AND id = ?",
                (user_id, int(ref))).fetchone()
        if not row:
            row = conn.execute(
                "SELECT * FROM jobs WHERE user_id = ? AND LOWER(name) = LOWER(?)",
                (user_id, ref)).fetchone()
        if not row:
            # Partial match, so "/logs mybot" works when the app is
            # "mybot-2" — but only when it is unambiguous.
            hits = conn.execute(
                "SELECT * FROM jobs WHERE user_id = ? AND LOWER(name) LIKE LOWER(?)",
                (user_id, f"%{ref}%")).fetchall()
            if len(hits) == 1:
                row = hits[0]
    finally:
        conn.close()
    return dict(row) if row else None


def _worker_of(row) -> str:
    try:
        return (dict(row).get("worker_url") or "") or None
    except Exception:
        return None


def _row_env(row, rescue: bool = True) -> dict:
    """Env vars saved for a job row (empty when unset / unparsable). Same
    logic as routes/runspace.py's private copy — duplicated rather than
    imported because routes/ should not become an import target for
    services/, but kept in sync deliberately.

    When the stored row cannot be decoded at all and `rescue` is on, the
    runner's own copy of the env is read back and repaired into the database
    (services/env_rescue.py). Every caller here is about to START or EDIT the
    job, and both would otherwise act on an empty env: a start with no
    BOT_TOKEN, or a set_env that silently deletes every other variable the bot
    had. Pass rescue=False only for a read that must not touch the network.

    A BOT_TOKEN found only in the source file is promoted into the returned
    map (in-memory). Env tab is optional when the code already holds the token.
    """
    try:
        raw = dict(row).get("env")
    except Exception:
        values, readable = {}, True
    else:
        values, readable = secrets_store.read_env(raw)
    if not readable and rescue:
        from services import env_rescue
        got, outcome = env_rescue.rescue_job_env(row)
        if got:
            try:
                row["env"] = secrets_store.pack_env(got)
            except Exception:
                pass
            values = got
        else:
            logger.error(
                "job %s: stored variables could not be read and the runner had "
                "no copy to restore (outcome=%s)", dict(row).get("name"), outcome)
    # Defined below; fine at call time. Avoids every caller forgetting.
    return ensure_bot_token_in_env(row, values)


def _url_slug(name) -> str:
    """Mirror of static/pro.js `_slugify`, so a link built here opens the app
    the dashboard will actually select."""
    s = re.sub(r"['’]", "", str(name or "").lower().strip())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:80]
    return s or "untitled"


def dashboard_link(row) -> str:
    """Public link to one app's page, or '' when this site's URL is unknown."""
    item = dict(row) if not isinstance(row, dict) else row
    if not item.get("name"):
        return ""
    try:
        from services.pingbot import SITE_BASE
    except Exception:
        return ""
    return f"{SITE_BASE}/bots/{_url_slug(item['name'])}" if SITE_BASE else ""


def token_from_source(code: str = "", env: dict = None) -> str:
    """A BOT_TOKEN found in source or env — without calling Telegram.

    Many bots ship the token literally in the file (no Env tab). Recovery and
    restart must treat that as a real token: refusing with "set BOT_TOKEN in
    Env" when the code already has one is exactly the loop owners hate.
    """
    try:
        from services import telegram_detector
        text = telegram_detector._text(code or "", env or {})
        m = telegram_detector.TOKEN_RE.search(text or "")
        return m.group(1) if m else ""
    except Exception:
        return ""


def ensure_bot_token_in_env(row, env: dict = None) -> dict:
    """Return env, promoting a token found in code into BOT_TOKEN when missing.

    Does NOT rewrite the database: the code stays the source of truth the owner
    already trusts. We only make sure the runner process receives BOT_TOKEN so
    frameworks that read os.environ still work, and so our own "is there a
    token?" gates stop blocking a perfectly runnable bot.
    """
    env = dict(env or {})
    if (env.get("BOT_TOKEN") or "").strip():
        return env
    tok = token_from_source(row.get("code") or "", env)
    if tok:
        env["BOT_TOKEN"] = tok
    return env


def env_missing_message(row) -> str:
    """What the owner sees when a bot cannot start for want of its token.

    It says nothing about keys or storage formats: from the owner's side the
    only fact that matters is "my token isn't on the server", and the only
    useful response is where to put it back. Everything else about the app is
    still there, so the message says that too — a bot that will not start reads
    like a lost bot otherwise.
    """
    item = dict(row) if not isinstance(row, dict) else row
    name = item.get("name") or "your app"
    link = dashboard_link(item)
    where = f"{link} → *Env*" if link else "the dashboard → your app → *Env*"
    return (f"❌ *{name}* can't start — no `BOT_TOKEN` found in Env *or* in the source.\n"
            f"Fix it either way:\n"
            f"• open {where}, paste the token from @BotFather, *Save & restart*, **or**\n"
            f"• put `BOT_TOKEN = '…'` (or `os.environ['BOT_TOKEN'] = '…'`) in the code itself.\n"
            f"Your files and database are untouched — only the token is missing.")


def _set_assignment(row, runner_id, worker, desired="running"):
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE jobs SET runner_job_id=?,worker_url=?,desired_state=?,updated_at=? "
            "WHERE id=? AND user_id=?",
            (runner_id, worker, desired, now_utc_str(), row["id"], row["user_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    row["runner_job_id"] = runner_id
    row["worker_url"] = worker
    row["desired_state"] = desired


def _cold_start(row, code=None, language=None) -> dict:
    """Create a fresh internal job and atomically replace its assignment.

    The jobs-table id/name remains the user's stable identity. Runner ids are
    disposable implementation details and may change after any runner deploy.
    """
    env = ensure_bot_token_in_env(row, _row_env(row))
    if row.get("telegram_bot_detected") and not env.get("BOT_TOKEN"):
        # Still nothing: not in env, not in the runner's copy, not as a literal
        # in the source. Only THEN do we refuse — a token sitting in the code
        # is enough to run, Env is optional.
        logger.error("cannot start %s: Telegram bot with no BOT_TOKEN in env, "
                     "runner, or source", row["name"])
        return {"ok": False, "error": env_missing_message(row)}
    # Older Telegram-created rows did not persist worker_url. Before creating
    # anything, find the process by its stable internal name across the whole
    # fleet. This adopts the real assignment and prevents a second poller from
    # being launched with the same Telegram token.
    stable_name = f"u{row['user_id']}-{row['name']}"
    try:
        matches = [info for info in runner_client.fleet_jobs(refresh=True).values()
                   if info.get("name") == stable_name]
    except Exception:
        matches = []
    if matches:
        chosen = next((x for x in matches if x.get("status") == "running"), matches[0])
        worker = chosen.get("worker")
        _set_assignment(row, chosen["id"], worker, "running")
        # Clean up duplicates left by the old ambiguous routing code.
        for duplicate in matches:
            if duplicate.get("id") == chosen.get("id"):
                continue
            try:
                runner_client._runner_http("POST", f"/internal/jobs/{duplicate['id']}/stop",
                                           worker=duplicate.get("worker"))
            except Exception:
                pass
        try:
            restarted = runner_client._runner_http(
                "POST", f"/internal/jobs/{chosen['id']}/restart", worker=worker)
            if restarted.status_code == 200:
                chosen = restarted.json()
        except Exception:
            pass
        return {"ok": True, "job": row, "info": chosen}
    body = {
        "language": language or row.get("language") or "python",
        "code": code if code is not None else (row.get("code") or ""),
        "name": f"u{row['user_id']}-{row['name']}",
        "env": env,
        "mem_limit_mb": _mem_limit_for(row["user_id"]),
    }
    try:
        response = runner_client._runner_http("POST", "/internal/jobs", body)
    except Exception as exc:
        logger.warning("cold start failed for DB job %s: %s", row.get("id"), exc)
        return {"ok": False, "error": "No runner is ready yet. Try again shortly."}
    if response.status_code != 201:
        try:
            detail = response.json().get("detail", "Runner rejected the bot.")
        except Exception:
            detail = "Runner rejected the bot."
        return {"ok": False, "error": detail}
    info = response.json()
    worker = getattr(response, "placed_on", None)
    _set_assignment(row, info["id"], worker, "running")
    try:
        from services import snapshots
        restored = snapshots.restore_snapshot(
            row["id"], info["id"], overwrite=True, worker=worker)
        if restored.get("restored"):
            restarted = runner_client._runner_http(
                "POST", f"/internal/jobs/{info['id']}/restart", worker=worker)
            if restarted.status_code == 200:
                info = restarted.json()
    except Exception as exc:
        logger.warning("cold start snapshot failed for DB job %s: %s", row.get("id"), exc)
    return {"ok": True, "job": row, "info": info}


def _ensure_present(row) -> dict:
    rid = row.get("runner_job_id")
    if rid:
        try:
            response = runner_client._runner_http(
                "GET", f"/internal/jobs/{rid}", worker=_worker_of(row))
            if response.status_code == 200:
                return {"ok": True, "job": row, "info": response.json() or {}}
            if response.status_code != 404:
                return {"ok": False, "error": f"Runner returned HTTP {response.status_code}."}
        except Exception as exc:
            logger.warning("assignment check failed for DB job %s: %s", row.get("id"), exc)
            return {"ok": False, "error": "The assigned runner did not answer. Try again shortly."}
    if row.get("desired_state") == "stopped":
        return {"ok": False, "error": f"“{row['name']}” is stopped. Use /restart {row['name']}."}
    return _cold_start(row)


def active_count(user_id: int) -> int:
    """Apps the runner reports as alive. Counting rows would lock an account
    out after MAX_JOBS_PER_USER lifetime jobs even with all of them stopped."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT runner_job_id,desired_state FROM jobs WHERE user_id = ?", (user_id,)
        ).fetchall()
    finally:
        conn.close()
    live = set(runner_client.fleet_jobs())
    if not live:
        return sum(1 for r in rows if dict(r).get("desired_state") != "stopped")
    return sum(1 for r in rows if dict(r).get("runner_job_id") in live)


def _act(user_id: int, ref: str, verb: str) -> dict:
    row = find_app(user_id, ref)
    if not row:
        return {"ok": False, "error": f"No app called “{ref}”. /apps lists yours."}
    rid = row.get("runner_job_id")
    if verb == "restart":
        if rid:
            try:
                response = runner_client._runner_http(
                    "POST", f"/internal/jobs/{rid}/restart", worker=_worker_of(row))
                if response.status_code == 200:
                    _set_assignment(row, rid, _worker_of(row), "running")
                    return {"ok": True, "job": row}
                if response.status_code != 404:
                    return {"ok": False, "error": f"Runner rejected restart (HTTP {response.status_code})."}
            except Exception as exc:
                logger.warning("bot restart failed for job %s: %s", row.get("id"), exc)
                return {"ok": False, "error": "The assigned runner did not answer. Try again shortly."}
        # Internal id vanished after a deploy: create on the least-loaded
        # runner and replace the DB assignment instead of claiming success.
        return _cold_start(row)

    # Stop is idempotent: DB desired_state is the source of truth.
    # Even when the runner is offline/waking we MARK STOPPED so the dashboard
    # never stays green while the polling process is still alive elsewhere.
    # The orphan reaper kills the live process when the worker answers again.
    if rid:
        try:
            response = runner_client._runner_http(
                "POST", f"/internal/jobs/{rid}/stop", worker=_worker_of(row))
            if response.status_code not in (200, 404):
                logger.warning("bot stop rejected for job %s: HTTP %s — marking stopped anyway",
                               row.get("id"), response.status_code)
        except Exception as exc:
            logger.warning("bot stop unreachable for job %s: %s — marking stopped anyway",
                           row.get("id"), exc)
    _set_assignment(row, rid, _worker_of(row), "stopped")
    return {"ok": True, "job": row}


def restart(user_id: int, ref: str) -> dict:
    return _act(user_id, ref, "restart")





def stop(user_id: int, ref: str) -> dict:
    return _act(user_id, ref, "stop")


def delete(user_id: int, ref: str) -> dict:
    """Remove the app entirely — runner first, then the row."""
    row = find_app(user_id, ref)
    if not row:
        return {"ok": False, "error": f"No app called “{ref}”. /apps lists yours."}
    rid = row.get("runner_job_id")
    if rid:
        try:
            runner_client._runner_http("DELETE", f"/internal/jobs/{rid}",
                                       worker=_worker_of(row))
        except Exception as exc:
            # Best effort: a worker that is asleep must not strand the row
            # forever, or the user can never get back under their cap.
            logger.warning("bot delete: runner call failed for %s: %s", rid, exc)
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM jobs WHERE id = ? AND user_id = ?",
                     (row["id"], user_id))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "job": row}


def rename(user_id: int, ref: str, new_name: str) -> dict:
    row = find_app(user_id, ref)
    if not row:
        return {"ok": False, "error": f"No app called “{ref}”. /apps lists yours."}
    clean = slugify_name(new_name)
    if not clean:
        return {"ok": False, "error": "That name has no usable characters."}
    conn = get_db_connection()
    try:
        dup = conn.execute(
            "SELECT id FROM jobs WHERE user_id = ? AND LOWER(name) = LOWER(?) AND id != ?",
            (user_id, clean, row["id"])).fetchone()
        if dup:
            return {"ok": False, "error": f"You already have an app called “{clean}”."}
        conn.execute("UPDATE jobs SET name = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                     (clean, now_utc_str(), row["id"], user_id))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "old": row["name"], "name": clean}


def set_env(user_id: int, ref: str, key: str, value) -> dict:
    """Set one env var on a job (value=None deletes it). Same rails as the
    website's Env tab — same jobs.env column, same secrets_store packing.
    Restarts the job if it's currently running so the change actually
    takes effect; otherwise it applies on the next start."""
    row = find_app(user_id, ref)
    if not row:
        return {"ok": False, "error": f"No app called “{ref}”. /apps lists yours."}
    key = (key or "").strip()
    if not key or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
        return {"ok": False, "error": "Env var names must look like BOT_TOKEN — "
                                       "letters, numbers, underscore, not starting with a digit."}
    env = _row_env(row)
    deleted = value is None
    if deleted:
        env.pop(key, None)
    else:
        env[key] = value
    conn = get_db_connection()
    try:
        conn.execute("UPDATE jobs SET env = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                     (secrets_store.pack_env(env), now_utc_str(), row["id"], user_id))
        conn.commit()
    finally:
        conn.close()
    restarted = False
    if row.get("runner_job_id"):
        _act(user_id, ref, "restart")
        restarted = True
    return {"ok": True, "job": row, "key": key, "deleted": deleted, "restarted": restarted}


def logs(user_id: int, ref: str, lines: int = 40) -> dict:
    row = find_app(user_id, ref)
    if not row:
        return {"ok": False, "error": f"No app called “{ref}”. /apps lists yours."}
    present = _ensure_present(row)
    if not present.get("ok"):
        return present
    info = present.get("info") or {}
    text = (info.get("logs") or "").splitlines()
    return {"ok": True, "job": row, "info": info,
            "logs": "\n".join(text[-lines:]),
            "truncated": len(text) > lines}


# ── /code and /update — see the module docstring before changing these ────

def create_app_from_zip(user_id: int, name: str, zip_bytes: bytes, language: str = "") -> dict:
    """Multi-file variant of create_app: the runner extracts the WHOLE zip
    into the job's directory and picks the entry point itself (same
    _detect_entry pipeline as a GitHub import), instead of the old chat
    behaviour of reading one file out of the zip and silently dropping
    the rest — which broke any app whose entry file imported a sibling
    module that never made it onto disk."""
    import base64
    clean = slugify_name(name)
    if not clean:
        return {"ok": False, "error": "That name has no usable characters."}

    conn = get_db_connection()
    try:
        dup = conn.execute(
            "SELECT id FROM jobs WHERE user_id = ? AND LOWER(name) = LOWER(?)",
            (user_id, clean)).fetchone()
        if dup:
            return {"ok": False,
                    "error": f"You already have an app called “{clean}”. Use /update {clean} instead."}
        rows = conn.execute(
            "SELECT runner_job_id,desired_state FROM jobs WHERE user_id = ?", (user_id,)
        ).fetchall()
    finally:
        conn.close()

    live = set(runner_client.fleet_jobs())
    active = (sum(1 for r in rows if dict(r).get("runner_job_id") in live)
              if live else sum(1 for r in rows if dict(r).get("desired_state") != "stopped"))
    _limit = _effective_job_limit(user_id)
    if active >= _limit:
        return {"ok": False,
                "error": (f"You already have {active} of {_limit} bots "
                          f"running — stop one before making another.")}

    body = {"language": language or "python", "code": "", "name": f"u{user_id}-{clean}",
            "env": {}, "zip_b64": base64.b64encode(zip_bytes).decode("ascii"),
            "mem_limit_mb": _mem_limit_for(user_id), **zip_limits_for(user_id)}
    resp = runner_client._runner_http("POST", "/internal/jobs", body)
    if resp.status_code != 201:
        try:
            detail = resp.json().get("detail", "Runner rejected the app.")
        except Exception:
            detail = "Runner rejected the app."
        return {"ok": False, "error": detail}

    info = resp.json()
    now = now_utc_str()
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO jobs (user_id,name,language,code,runner_job_id,worker_url,desired_state,env,"
            "created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,'running',?,?,?)",
            (user_id, clean, info.get("language") or language or "python", "",
             info["id"], getattr(resp, "placed_on", None), None, now, now))
        conn.commit()
        job_db_id = cursor.lastrowid
    finally:
        conn.close()

    web = runner_client._job_web_fields(info, getattr(resp, "placed_on", None))
    return {"ok": True, "name": clean, "job_db_id": job_db_id,
            "web": web.get("web") or web.get("web_url")}


def create_app_from_repo(user_id: int, name: str, repo_url: str, language: str = "",
                         entry: str = "", deps=None) -> dict:
    """GitHub-import variant of create_app, for /import in chat.

    Same uniqueness/cap rules as create_app. No inline code is sent — the
    runner clones the repo itself and auto-detects which file to run (see
    runner/app.py:_detect_entry: conventional names like main.py/bot.py
    first, then a manifest-aware fallback). No Telegram token check here
    yet, since there's no code text to inspect before the clone happens —
    unlike /code, so a repo-imported bot won't get an "Open your bot"
    button until its first /update or a manual check.
    """
    clean = slugify_name(name)
    if not clean:
        return {"ok": False, "error": "That name has no usable characters."}

    conn = get_db_connection()
    try:
        dup = conn.execute(
            "SELECT id FROM jobs WHERE user_id = ? AND LOWER(name) = LOWER(?)",
            (user_id, clean)).fetchone()
        if dup:
            return {"ok": False,
                    "error": f"You already have an app called “{clean}”. Use /update {clean} instead."}
        rows = conn.execute(
            "SELECT runner_job_id,desired_state FROM jobs WHERE user_id = ?", (user_id,)
        ).fetchall()
    finally:
        conn.close()

    live = set(runner_client.fleet_jobs())
    active = (sum(1 for r in rows if dict(r).get("runner_job_id") in live)
              if live else sum(1 for r in rows if dict(r).get("desired_state") != "stopped"))
    _limit = _effective_job_limit(user_id)
    if active >= _limit:
        return {"ok": False,
                "error": (f"You already have {active} of {_limit} bots "
                          f"running — stop one before making another.")}

    # "python" is a placeholder the runner replaces: with a repo_url present it
    # clones first and _detect_entry() picks the real language from the entry
    # file it finds. Sending an empty string used to be rejected outright by an
    # older runner ("Unsupported language: ."), so a GitHub import from chat
    # failed before the clone -- naming python keeps it working against a runner
    # that has not been redeployed yet, and costs nothing on one that has.
    body = {"language": language or "python", "code": "", "name": f"u{user_id}-{clean}",
            "env": {}, "repo_url": repo_url, "mem_limit_mb": _mem_limit_for(user_id)}
    # Which runnable thing inside the repo, and which manifests belong to it.
    # The caller scanned the repo (services/github_repo.py) and knows; without
    # this a repo holding two projects always deployed whichever file the
    # runner's own guess found first.
    if entry:
        body["entry"] = entry
    if deps:
        body["deps"] = [str(d) for d in list(deps)[:4]]
    resp = runner_client._runner_http("POST", "/internal/jobs", body)
    if resp.status_code != 201:
        try:
            detail = resp.json().get("detail", "Runner rejected the app.")
        except Exception:
            detail = "Runner rejected the app."
        return {"ok": False, "error": detail}

    info = resp.json()
    now = now_utc_str()
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO jobs (user_id,name,language,code,runner_job_id,worker_url,desired_state,env,"
            "repo_url,repo_entry,repo_commit,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,'running',?,?,?,?,?,?)",
            (user_id, clean, info.get("language") or language or "python", "",
             info["id"], getattr(resp, "placed_on", None), None,
             # What it was built from, and which revision: the runner reports the
             # commit it actually cloned, so "is there a newer one?" is a
             # comparison later instead of a guess from a date.
             repo_url, info.get("repo_entry") or entry or None,
             info.get("repo_commit") or None, now, now))
        conn.commit()
        job_db_id = cursor.lastrowid
    finally:
        conn.close()

    web = runner_client._job_web_fields(info, getattr(resp, "placed_on", None))
    return {"ok": True, "name": clean, "job_db_id": job_db_id,
            "web": web.get("web") or web.get("web_url"),
            # Which revision was built, and which file inside it runs: the
            # reply can name the commit, and "is there a newer one?" becomes a
            # comparison instead of a guess from a date.
            "commit": info.get("repo_commit") or "",
            "entry": info.get("repo_entry") or entry or ""}


def _runner_detail(resp, fallback: str) -> str:
    """The runner's own explanation when it refuses, or a usable sentence."""
    try:
        detail = (resp.json() or {}).get("detail")
    except Exception:                                              # noqa: BLE001
        detail = None
    return str(detail or fallback)


def _record_repo_state(job_db_id: int, repo_url: str, entry, commit) -> None:
    """Remember which revision an app is running, so an update can be offered."""
    conn = get_db_connection()
    try:
        conn.execute("UPDATE jobs SET repo_url=?, repo_entry=?, repo_commit=?, updated_at=? "
                     "WHERE id=?",
                     (repo_url or None, entry or None, commit or None,
                      now_utc_str(), job_db_id))
        conn.commit()
    finally:
        conn.close()


def update_from_repo(user_id: int, ref: str, repo_url: str = None, entry=None,
                     deps=None) -> dict:
    """Pull the newest revision of the repo this app was built from.

    In place, the way a platform redeploy works: same job id, same folder — so
    the bot's database and sessions survive — same public address, same env. The
    runner re-clones, installs the manifests and restarts. The commit it built
    comes back so the caller can say which version is now running.
    """
    row = find_app(user_id, ref)
    if not row:
        return {"ok": False, "error": f"No app called “{ref}”. `/apps` lists yours."}
    old_url = (row.get("repo_url") or "").strip()
    url = (repo_url or old_url or "").strip()
    if not url:
        return {"ok": False,
                "error": f"*{row['name']}* wasn't deployed from a repo, so there is "
                         f"no newer version to pull. `/update {row['name']}` and send "
                         f"the code or a `.zip` instead."}
    # A NEW repo or branch was asked for: accept `owner/repo`, `github.com/...`,
    # `/tree/<branch>` and `#<branch>`, and turn them into the https form the
    # runner requires. The job id, folder and database stay exactly as they are.
    changed_source = False
    if repo_url and repo_url.strip():
        from services import github_repo
        o, r, b = github_repo.parse_repo(url)
        if not o:
            return {"ok": False,
                    "error": "That doesn't look like a GitHub repo — expected "
                             "`github.com/user/repo` or `user/repo/tree/branch`."}
        url = github_repo.repo_url(o, r, b)
        changed_source = url != old_url
    if entry is None:
        # The old entry file belongs to the old repo/branch; on a changed
        # source let the runner detect it again instead of failing on a
        # file that may not exist there.
        entry = "" if changed_source else (row.get("repo_entry") or "")
    env = _row_env(row)
    rid = row.get("runner_job_id")

    patch = {"name": row["name"], "env": env, "repo_url": url,
             # Re-sync the 👑 flag on every redeploy: the runner stores the limit
             # when a job is created, so a grant made afterwards only reaches a
             # job that gets redeployed.
             "mem_limit_mb": _mem_limit_for(user_id)}
    if entry:
        patch["entry"] = entry
    if deps:
        patch["deps"] = [str(d) for d in list(deps)[:4]]

    if rid:
        try:
            from services import snapshots
            snapshots.save_snapshot(row["id"], rid, worker=_worker_of(row))
        except Exception as exc:                                   # noqa: BLE001
            logger.warning("update_from_repo: pre-update snapshot failed for job %s: %s",
                           row["id"], exc)
        resp = runner_client._runner_http("PATCH", f"/internal/jobs/{rid}", patch,
                                          worker=_worker_of(row))
        if resp.status_code == 200:
            info = resp.json() or {}
            commit = info.get("repo_commit") or ""
            _set_assignment(row, rid, _worker_of(row), "running")
            _record_repo_state(row["id"], url, info.get("repo_entry") or entry, commit)
            return {"ok": True, "job": row, "commit": commit}
        if resp.status_code != 404:
            return {"ok": False, "error": _runner_detail(resp, "Runner rejected the update.")}
        # 404: the runner no longer has it (redeployed, drained, moved). Fall
        # through and create it again on whichever worker has room.

    create = {"language": row.get("language") or "python", "code": "",
              "name": f"u{user_id}-{row['name']}", "env": env, "repo_url": url,
              "mem_limit_mb": _mem_limit_for(user_id)}
    if entry:
        create["entry"] = entry
    if deps:
        create["deps"] = [str(d) for d in list(deps)[:4]]
    resp = runner_client._runner_http("POST", "/internal/jobs", create)
    if resp.status_code != 201:
        return {"ok": False, "error": _runner_detail(resp, "Runner rejected the update.")}
    info = resp.json()
    now = now_utc_str()
    conn = get_db_connection()
    try:
        conn.execute("UPDATE jobs SET runner_job_id=?, worker_url=?, desired_state='running', "
                     "repo_url=?, repo_entry=?, repo_commit=?, updated_at=? WHERE id=?",
                     (info["id"], getattr(resp, "placed_on", None), url,
                      info.get("repo_entry") or entry or None,
                      info.get("repo_commit") or None, now, row["id"]))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "job": row, "commit": info.get("repo_commit") or "",
            "recreated": True}


def set_auto_deploy(user_id: int, ref: str, on: bool) -> dict:
    """👑 Follow the branch: redeploy by itself when a new commit lands."""
    row = find_app(user_id, ref)
    if not row:
        return {"ok": False, "error": f"No app called “{ref}”. `/apps` lists yours."}
    if not (row.get("repo_url") or "").strip():
        return {"ok": False,
                "error": f"*{row['name']}* wasn't deployed from a repo, so there is "
                         f"nothing to follow. `/import <github url>` creates one that can."}
    conn = get_db_connection()
    try:
        conn.execute("UPDATE jobs SET auto_deploy=?, updated_at=? WHERE id=?",
                     (1 if on else 0, now_utc_str(), row["id"]))
        conn.commit()
    finally:
        conn.close()
    row["auto_deploy"] = 1 if on else 0
    return {"ok": True, "job": row, "on": bool(on)}


def auto_deploy_jobs() -> list:
    """Every app that follows its branch, across all accounts.

    Read by the recovery loop — the only thing here that runs on a schedule. The
    sweep is one SELECT plus one cheap GitHub lookup per DISTINCT repo (cached),
    so ten apps following one repo cost one request.
    """
    conn = get_db_connection()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, user_id, name, repo_url, repo_entry, repo_commit, worker_url, "
            "runner_job_id FROM jobs WHERE auto_deploy = 1 AND repo_url IS NOT NULL "
            "AND repo_url != '' ORDER BY id").fetchall()]
    finally:
        conn.close()


def create_app(user_id: int, name: str, language: str, code: str) -> dict:
    """Make a brand-new app from chat-supplied name + code. Identical rules
    to POST /api/jobs: per-user name uniqueness, the MAX_JOBS_PER_USER cap,
    and a real jobs-table row so the app shows up in the dashboard and
    /admin/jobs exactly like one made on the site."""
    clean = slugify_name(name)
    if not clean:
        return {"ok": False, "error": "That name has no usable characters."}

    conn = get_db_connection()
    try:
        dup = conn.execute(
            "SELECT id FROM jobs WHERE user_id = ? AND LOWER(name) = LOWER(?)",
            (user_id, clean)).fetchone()
        if dup:
            return {"ok": False,
                    "error": f"You already have an app called “{clean}”. Use /update {clean} instead."}
        rows = conn.execute(
            "SELECT runner_job_id,desired_state FROM jobs WHERE user_id = ?", (user_id,)
        ).fetchall()
    finally:
        conn.close()

    live = set(runner_client.fleet_jobs())
    active = (sum(1 for r in rows if dict(r).get("runner_job_id") in live)
              if live else sum(1 for r in rows if dict(r).get("desired_state") != "stopped"))
    _limit = _effective_job_limit(user_id)
    if active >= _limit:
        return {"ok": False,
                "error": (f"You already have {active} of {_limit} bots "
                          f"running — stop one before making another.")}

    body = {"language": language, "code": code, "name": f"u{user_id}-{clean}", "env": {},
            "mem_limit_mb": _mem_limit_for(user_id)}
    resp = runner_client._runner_http("POST", "/internal/jobs", body)
    if resp.status_code != 201:
        try:
            detail = resp.json().get("detail", "Runner rejected the app.")
        except Exception:
            detail = "Runner rejected the app."
        return {"ok": False, "error": detail}

    info = resp.json()
    now = now_utc_str()
    # Same detection the website's Connect step does — getMe against a token
    # found in the pasted code. Never blocks the deploy: unverified/no token
    # just means no "Open your bot" button later, not a rejected deploy.
    from services import telegram_detector
    bot_meta = telegram_detector.inspect_bot(code)
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO jobs (user_id,name,language,code,runner_job_id,worker_url,desired_state,env,"
            "telegram_bot_detected,telegram_bot_username,telegram_bot_id,telegram_check_status,telegram_verified_at,"
            "created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,'running',?,?,?,?,?,?,?,?)",
            (user_id,clean,language,code,info["id"],getattr(resp,"placed_on",None),None,
             1 if bot_meta.get("detected") else 0, bot_meta.get("username"), bot_meta.get("bot_id"),
             bot_meta.get("check_status"), bot_meta.get("verified_at"), now,now))
        conn.commit()
        job_db_id = cursor.lastrowid
    finally:
        conn.close()

    web = runner_client._job_web_fields(info, getattr(resp, "placed_on", None))
    return {"ok": True, "name": clean, "job_db_id": job_db_id,
            "telegram_bot_username": bot_meta.get("username") if bot_meta.get("check_status") == "verified" else None,
            "web": web.get("web") or web.get("web_url")}


def update_from_zip(user_id: int, ref: str, zip_bytes: bytes, language: str = None) -> dict:
    """Multi-file variant of update_code, for a permitted /update with a
    .zip. The runner extracts the WHOLE zip into the app's existing
    workspace (files not in the zip — a database, a session file — are
    left alone, since extraction only ever WRITES what's inside the zip,
    never clears the directory first) and re-detects the entry point."""
    import base64
    row = find_app(user_id, ref)
    if not row:
        return {"ok": False, "error": f"No app called “{ref}”. /apps lists yours."}

    rid = row.get("runner_job_id")
    lang = language or row["language"]
    now = now_utc_str()
    zip_b64 = base64.b64encode(zip_bytes).decode("ascii")
    conn = get_db_connection()
    try:
        conn.execute("UPDATE jobs SET language = ?, updated_at = ? WHERE id = ?",
                     (lang, now, row["id"]))
        conn.commit()
    finally:
        conn.close()
    env = _row_env(row)

    if not rid:
        body = {"language": lang, "code": "", "name": f"u{user_id}-{row['name']}",
                "env": env, "zip_b64": zip_b64, "mem_limit_mb": _mem_limit_for(user_id),
                **zip_limits_for(user_id)}
        resp = runner_client._runner_http("POST", "/internal/jobs", body)
        if resp.status_code != 201:
            try:
                detail = resp.json().get("detail", "Runner rejected the app.")
            except Exception:
                detail = "Runner rejected the app."
            return {"ok": False, "error": detail}
        info = resp.json()
        conn = get_db_connection()
        try:
            conn.execute("UPDATE jobs SET runner_job_id=?,worker_url=?,desired_state='running',updated_at=? WHERE id=?",
                         (info["id"],getattr(resp,"placed_on",None),now_utc_str(),row["id"]))
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "job": row}

    try:
        from services import snapshots
        snapshots.save_snapshot(row["id"], rid, worker=_worker_of(row))
    except Exception as exc:
        logger.warning("bot update_from_zip: pre-update snapshot failed for job %s: %s", row["id"], exc)

    patch_body = {"name": row["name"], "language": lang, "env": env, "zip_b64": zip_b64,
                  **zip_limits_for(user_id),
                  # Re-sync the /queen flag on every redeploy: the runner only
                  # ever stored it at creation time, so a 👑 granted after the
                  # first deploy never reached a job updated in place.
                  "mem_limit_mb": _mem_limit_for(user_id)}
    resp = runner_client._runner_http("PATCH", f"/internal/jobs/{rid}", patch_body,
                                       worker=_worker_of(row))
    if resp.status_code == 200:
        _set_assignment(row, rid, _worker_of(row), "running")
        return {"ok": True, "job": row}

    if resp.status_code == 404:
        create_body = {"language": lang, "code": "", "name": f"u{user_id}-{row['name']}",
                       "env": env, "zip_b64": zip_b64, "mem_limit_mb": _mem_limit_for(user_id),
                       **zip_limits_for(user_id)}
        resp2 = runner_client._runner_http("POST", "/internal/jobs", create_body)
        if resp2.status_code != 201:
            try:
                detail = resp2.json().get("detail", "Runner rejected the update.")
            except Exception:
                detail = "Runner rejected the update."
            return {"ok": False, "error": detail}
        info = resp2.json()
        conn = get_db_connection()
        try:
            conn.execute("UPDATE jobs SET runner_job_id=?,worker_url=?,desired_state='running',updated_at=? WHERE id=?",
                         (info["id"],getattr(resp2,"placed_on",None),now_utc_str(),row["id"]))
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "job": row}

    try:
        detail = resp.json().get("detail", "Runner rejected the update.")
    except Exception:
        detail = "Runner rejected the update."
    return {"ok": False, "error": detail}


def update_code(user_id: int, ref: str, code: str, language: str = None) -> dict:
    """Redeploy an EXISTING app in place with new code — the chat equivalent
    of PATCH /api/jobs/{id}. Same worker, same slug/URL, same persistent
    workspace (SQLite files, session data): only the source changes. Falls
    back to a cold create if the runner no longer holds the job, same as the
    website's edit path does."""
    row = find_app(user_id, ref)
    if not row:
        return {"ok": False, "error": f"No app called “{ref}”. /apps lists yours."}

    rid = row.get("runner_job_id")
    lang = language or row["language"]
    now = now_utc_str()
    from services import telegram_detector
    bot_meta = telegram_detector.inspect_bot(code)
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE jobs SET code = ?, language = ?, updated_at = ?, "
            "telegram_bot_detected = ?, telegram_bot_username = ?, telegram_bot_id = ?, "
            "telegram_check_status = ?, telegram_verified_at = ? WHERE id = ?",
            (code, lang, now,
             1 if bot_meta.get("detected") else 0, bot_meta.get("username"), bot_meta.get("bot_id"),
             bot_meta.get("check_status"), bot_meta.get("verified_at"), row["id"]))
        conn.commit()
    finally:
        conn.close()
    # Reflect the fresh detection in the row we return — callers (like the
    # chat bot's "Open your bot" button) read this immediately, before any
    # separate re-fetch would see the UPDATE above.
    row["telegram_bot_username"] = bot_meta.get("username") if bot_meta.get("check_status") == "verified" else None

    env = _row_env(row)

    if not rid:
        # Never actually deployed (e.g. imported but never started) — bring
        # it up fresh instead of PATCHing a job the runner has never heard of.
        body = {"language": lang, "code": code, "name": f"u{user_id}-{row['name']}", "env": env,
                "mem_limit_mb": _mem_limit_for(user_id)}
        resp = runner_client._runner_http("POST", "/internal/jobs", body)
        if resp.status_code != 201:
            try:
                detail = resp.json().get("detail", "Runner rejected the app.")
            except Exception:
                detail = "Runner rejected the app."
            return {"ok": False, "error": detail}
        info = resp.json()
        conn = get_db_connection()
        try:
            conn.execute("UPDATE jobs SET runner_job_id=?,worker_url=?,desired_state='running',updated_at=? WHERE id=?",
                         (info["id"],getattr(resp,"placed_on",None),now_utc_str(),row["id"]))
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "job": row}

    # Best-effort backup before touching the running job — mirrors the
    # website's update_job(): an edit is the moment most likely to lose data
    # if the runner has to cold-start.
    try:
        from services import snapshots
        snapshots.save_snapshot(row["id"], rid, worker=_worker_of(row))
    except Exception as exc:
        logger.warning("bot update_code: pre-update snapshot failed for job %s: %s", row["id"], exc)

    patch_body = {"name": row["name"], "language": lang, "code": code, "env": env,
                  # See update_from_zip: keeps the runner's RLIMIT in step with
                  # the account's current 👑 flag instead of the one it was
                  # first deployed with.
                  "mem_limit_mb": _mem_limit_for(user_id)}
    resp = runner_client._runner_http("PATCH", f"/internal/jobs/{rid}", patch_body,
                                       worker=_worker_of(row))
    if resp.status_code == 200:
        _set_assignment(row, rid, _worker_of(row), "running")
        return {"ok": True, "job": row}

    if resp.status_code == 404:
        # Runner restarted since — fall back to a cold create, same as
        # routes/runspace.py's update_job().
        create_body = {"language": lang, "code": code, "name": f"u{user_id}-{row['name']}", "env": env,
                       "mem_limit_mb": _mem_limit_for(user_id)}
        resp2 = runner_client._runner_http("POST", "/internal/jobs", create_body)
        if resp2.status_code != 201:
            try:
                detail = resp2.json().get("detail", "Runner rejected the update.")
            except Exception:
                detail = "Runner rejected the update."
            return {"ok": False, "error": detail}
        info = resp2.json()
        conn = get_db_connection()
        try:
            conn.execute("UPDATE jobs SET runner_job_id=?,worker_url=?,desired_state='running',updated_at=? WHERE id=?",
                         (info["id"],getattr(resp2,"placed_on",None),now_utc_str(),row["id"]))
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "job": row}

    try:
        detail = resp.json().get("detail", "Runner rejected the update.")
    except Exception:
        detail = "Runner rejected the update."
    return {"ok": False, "error": detail}
