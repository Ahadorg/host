"""Bots must come back on their own, however often the runner restarts.

THE FAILURE THIS GUARDS AGAINST
-------------------------------
A Render deploy (or a free-tier spin-down) wipes the runner's in-memory job
registry and, on the free tier, its filesystem too. Every bot that was supposed
to be running 24/7 is now gone, while the site's database still says
desired_state='running'. Before this, the ONLY thing that noticed was a single
recovery pass at SITE startup — so when the RUNNER redeployed on its own, nobody
re-created anything: the dashboard showed the bots as stopped, the owners' bots
were dead, and they stayed dead until someone pressed Restart by hand. That is
how a user's bot "just went away".

THREE PARTS
-----------
1. recover_once() — for every desired-running job the fleet no longer has,
   re-create it from the code and env stored in the database, then restore the
   last workspace snapshot (database.db, session.json, data/) so the bot comes
   back with its history, not empty.

2. start_reconciler() — run that same pass forever, every
   JOB_RECOVERY_INTERVAL_S (default 300), instead of once at boot. A runner that
   restarts ten times a day is repaired ten times a day, with nobody involved.

3. auto_deploy_sweep() — on the same thread but its own, longer clock
   (AUTO_DEPLOY_INTERVAL_S, default 600): every 👑 app that follows its branch is
   compared with the branch's current commit and redeployed in place when it has
   moved. "I pushed, why didn't it update?" stops being a question.

WHY IT CANNOT DUPLICATE A RUNNING BOT
-------------------------------------
The dangerous version of this loop is: the runner is asleep or mid-boot, the
fleet probe comes back empty, and the reconciler "recovers" every job — creating
a second copy of each. Two Telegram pollers on one token fight (409 conflict)
and the owner's bot dies anyway. So a job is only re-created when its own worker
ANSWERED the probe and simply does not have it. A worker that did not answer is
skipped this round and retried on the next one; "no information" never means
"the job is gone".
"""
import asyncio
import logging
import os
import threading
import time

from database import get_db_connection
from services import runner_client, secrets_store

logger = logging.getLogger("codenest-job-recovery")

RECOVERY_INTERVAL_S = int(os.getenv("JOB_RECOVERY_INTERVAL_S", "300") or "0")

_reconciler_started = False
_reconciler_lock = threading.Lock()
_recovery_once_lock = threading.Lock()


def _record_recovery_action(row: dict, action: str, details: str, worker: str = "") -> None:
    """Best-effort operational trail; never shares deploy/analytics counters."""
    try:
        from services import runner_admin
        runner_url = (worker or row.get("worker_url") or "").strip()
        if not runner_url:
            runner_url = runner_client.runner_cfg()[0] or "embedded"
        runner_admin.record_action(runner_url, action, details,
                                  job_id=row.get("id"), job_name=row.get("name") or "")
    except Exception as exc:
        logger.debug("Recovery action history unavailable (%s)", type(exc).__name__)


def _wanted_rows():
    """Every job the site still wants running.

    Includes repo_url / repo_entry / repo_commit: a GitHub-imported app stores
    NO inline code (the runner clones it), so recovery that only re-POSTs
    `code` arrives at the runner as "Code is empty" and the bot never comes
    back. That is exactly the loop in production logs after every restart.
    """
    conn = get_db_connection()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id,user_id,name,language,code,env,runner_job_id,worker_url,"
            "telegram_bot_detected,repo_url,repo_entry,repo_commit "
            "FROM jobs "
            "WHERE desired_state='running' AND runner_job_id IS NOT NULL "
            "ORDER BY id"
        ).fetchall()]
    finally:
        conn.close()


def _remember(job_id, runner_id, worker):
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE jobs SET runner_job_id=?,worker_url=? WHERE id=?",
            (runner_id, worker, job_id),
        )
        conn.commit()
    finally:
        conn.close()


def _answered_workers(wanted: list) -> dict:
    """For each worker these jobs belong to: did it ANSWER just now?

    Answered = any HTTP status below 500 came back, including a 404 or a 401:
    those all prove a live process is on the other end whose job list can be
    trusted. A timeout, a connection error, or a generic 502/503 from a runner
    still booting proves nothing, so its jobs wait. Render's explicit
    "Service Suspended" page is different: the owner stopped that service, so
    recovery can safely place those missing jobs on another online runner.

    Only workers that actually own a wanted job are probed, and the key is the
    row's own worker_url (None = "the default worker", which _runner_http
    resolves to the embedded runner or the primary one). That keeps this off
    runner_pool()/the database: recovery must not depend on a lookup that can
    itself fail while the runner is down.
    """
    keys = {row.get("worker_url") or None for row in wanted}
    out = {}
    for key in keys:
        try:
            resp = runner_client._runner_http("GET", "/health", worker=key)
            status = getattr(resp, "status_code", 0) or 0
            suspended = runner_client._is_owner_suspended(resp)
            out[key] = bool(0 < status < 500 or suspended)
            if suspended:
                logger.warning("Recovery: runner %s is owner-suspended; missing jobs "
                               "will be placed on an online runner", key or "default")
        except Exception as exc:
            logger.info("Recovery: worker %s did not answer (%s) — its jobs are "
                        "left alone until it does", key or "default", exc)
            out[key] = False
    return out


def _recovery_body(row: dict, env: dict) -> dict | None:
    """Build the POST /internal/jobs payload that brings ONE bot back.

    Three shapes, same rails the original create used:

      1. Inline code  — paste / /code path. `code` is non-empty.
      2. Repo import  — `/import` / website GitHub. `code` is intentionally
         empty; `repo_url` (+ optional entry) is what the runner clones.
      3. Neither      — nothing to start. Return None so the caller logs and
         skips instead of spamming "Code is empty" every five minutes.

    Forgetting shape (2) is what made every GitHub-deployed bot die on a
    runner restart and stay dead: recovery re-POSTed empty code, the runner
    correctly refused, and the next sweep did the same thing forever.
    """
    from services import bot_ops
    code = (row.get("code") or "").strip()
    repo_url = (row.get("repo_url") or "").strip()
    entry = (row.get("repo_entry") or "").strip()

    if not code and not repo_url:
        return None

    body = {
        "language": row.get("language") or "python",
        # Repo jobs MUST send empty code: the runner clones instead. Sending a
        # placeholder string would write a fake main.py over the clone.
        "code": code if code else "",
        "name": f"u{row['user_id']}-{row['name']}",
        "env": env,
        # A 👑 user must come back unlimited too: recovery is a fresh create,
        # and a create without this silently re-applies the default ceiling.
        "mem_limit_mb": bot_ops.mem_limit_for(row["user_id"]),
    }
    if repo_url:
        body["repo_url"] = repo_url
        if entry:
            body["entry"] = entry
        # Remember which commit we last knew about so auto-deploy can still
        # tell "branch moved" from "just recovered at the same SHA".
        # The runner will report the commit it actually built; bot_ops paths
        # update the row. Recovery itself does not need to send it.
    return body


def recover_once():
    """Run one serialized recovery pass; overlapping callers never double-start jobs."""
    with _recovery_once_lock:
        return _recover_once_serial()


def _recover_once_serial():
    """Recreate missing desired-running jobs. Returns unresolved count."""
    rows = _wanted_rows()
    if not rows:
        return 0
    try:
        live = runner_client.fleet_jobs(refresh=True)
    except Exception:
        live = {}
    live_ids = set(live)
    missing = [row for row in rows if row.get("runner_job_id") not in live_ids]
    if not missing:
        return 0
    answered = _answered_workers(missing)
    unresolved = 0
    recovered = 0
    skipped_unreachable = 0
    for row in missing:
        if answered.get(row.get("worker_url") or None) is False:
            # The worker that owns this job never answered, so "missing from the
            # fleet" is our blindness, not its absence. Recreating it now would
            # put a second copy of the same bot — and the same Telegram token —
            # on the box. Wait for the next pass.
            skipped_unreachable += 1
            continue
        env, readable = secrets_store.read_env(row.get("env"))
        if not readable:
            # This site cannot decode its own row, but the runner keeps a copy of
            # the env in the job's manifest — read that back and repair the row
            # before concluding the bot is beyond help. Without this, a bot whose
            # row went unreadable stayed down until a human re-typed the token.
            from services import env_rescue
            env, outcome = env_rescue.rescue_job_env(row)
            if not env:
                logger.error("Recovery cannot read bot %s's stored variables and "
                             "the runner had no copy to restore (outcome=%s)",
                             row["id"], outcome)
        # Never start a Telegram bot without its token: it would crash-loop and
        # make the UI say “processing” while doing no useful work. With secrets
        # stored as plain text this now only happens when the row genuinely has
        # no token (or is unreadable everywhere, which is logged above).
        from services import bot_ops
        # Token may live in the source file itself (no Env tab). Promote it so
        # the runner gets BOT_TOKEN and we stop refusing a bot that can run.
        env = bot_ops.ensure_bot_token_in_env(row, env)
        if row.get("telegram_bot_detected") and not env.get("BOT_TOKEN"):
            # Truly nothing — not env, not code. Skip once; don't crash-loop.
            logger.error("Recovery skipped bot %s: no BOT_TOKEN in env or source",
                         row["id"])
            _record_recovery_action(row, "Automatic recovery blocked",
                                    "The saved environment and source contain no BOT_TOKEN.")
            unresolved += 1
            continue
        body = _recovery_body(row, env)
        if body is None:
            # Nothing the runner can start: no inline code AND no repo to clone.
            # Starting it would only produce the "Code is empty" 400 that used
            # to spam the logs on every restart.
            logger.error(
                "Recovery skipped bot %s (%s): no source stored and no repo_url "
                "to re-clone — owner must /update or re-import",
                row["id"], row.get("name"))
            _record_recovery_action(row, "Automatic recovery blocked",
                                    "No source code or repository URL is saved for this job.")
            unresolved += 1
            continue
        try:
            response = runner_client._runner_http("POST", "/internal/jobs", body)
            if response.status_code != 201:
                try:
                    detail = response.json().get("detail", response.text[:200])
                except Exception:
                    detail = response.text[:200]
                logger.error("Recovery rejected for bot %s: runner returned %s — %s",
                             row["id"], response.status_code, detail)
                _record_recovery_action(row, "Automatic recovery failed",
                                        f"Runner rejected the saved job with HTTP {response.status_code}.")
                unresolved += 1
                continue
            info = response.json()
            placed = getattr(response, "placed_on", None)
            _remember(row["id"], info["id"], placed)
            snapshot_restored = False
            try:
                from services import snapshots
                restored = snapshots.restore_snapshot(
                    row["id"], info["id"], overwrite=True, worker=placed)
                snapshot_restored = bool(restored.get("restored"))
                if snapshot_restored:
                    runner_client._runner_http(
                        "POST", f"/internal/jobs/{info['id']}/restart", worker=placed)
            except Exception as exc:
                logger.warning("Recovery snapshot failed for bot %s: %s", row["id"], exc)
            _record_recovery_action(
                row, "Automatic recovery succeeded",
                f"Restored as runner job {info.get('id')}; "
                f"workspace {'snapshot restored' if snapshot_restored else 'left as saved on runner'}.",
                placed or "",
            )
            recovered += 1
        except Exception as exc:
            logger.warning("Recovery start failed for bot %s: %s", row["id"], exc)
            _record_recovery_action(row, "Automatic recovery failed",
                                    f"Recovery raised {type(exc).__name__}; will retry on the next sweep.")
            unresolved += 1
    if recovered:
        logger.info("Recovered %d desired-running bot(s)", recovered)
    if skipped_unreachable:
        logger.info("Recovery deferred %d bot(s) until their worker answers",
                    skipped_unreachable)
    return unresolved


def _db_runner_map() -> dict:
    """Runner job map from DB: runner_job_id -> {id, desired_state, worker_url, name}."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, runner_job_id, desired_state, worker_url, name FROM jobs "
            "WHERE runner_job_id IS NOT NULL"
        ).fetchall()
        return {str(r["runner_job_id"]): dict(r) for r in rows}
    except Exception:
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def reap_orphans() -> int:
    """Kill runner jobs that DB no longer wants.

    TWO cases that make \"deleted but still polling Telegram\" or \"stopped but
    still running\" happen:

      1. ORPHAN — fleet has a job id that no DB row points to any more (DELETE
         while its runner was offline, so the runner DELETE never arrived).
      2. STOPPED STALE — DB says desired_state='stopped' but fleet still shows
         status=running (STOP while offline, so the polling process never got
         killed).

    Both are deleted/stopped on the runner that actually holds them. Best-effort
    per job — one failing worker does not block the others. Returns how many
    were reaped.
    """
    try:
        live = runner_client.fleet_jobs(refresh=True)
    except Exception as exc:
        logger.debug("reaper: fleet unreachable (%s)", exc)
        return 0
    if not live:
        return 0
    db_map = _db_runner_map()
    reaped = 0
    for rid, info in list(live.items()):
        worker = info.get("worker")
        status = (info.get("status") or "").lower()
        db_row = db_map.get(str(rid))
        if db_row is None:
            # No DB row → orphaned by a delete that missed this worker
            logger.warning("reaper: orphan runner job %s on %s (no DB row) — deleting", rid, worker)
            try:
                runner_client._runner_http("DELETE", f"/internal/jobs/{rid}", worker=worker)
                reaped += 1
                _record_recovery_action({"id": rid, "name": info.get("name") or rid, "worker_url": worker},
                                        "Orphan reaped",
                                        f"Runner job {rid} had no DB row; deleted from {worker}.",
                                        worker or "")
            except Exception as exc:
                logger.warning("reaper: delete failed for orphan %s on %s: %s", rid, worker, exc)
            continue
        if db_row.get("desired_state") == "stopped" and status == "running":
            logger.warning("reaper: stale running job %s (DB stopped) on %s — stopping", rid, worker)
            try:
                # Prefer STOP (keeps workspace) but DELETE also ends polling
                runner_client._runner_http("POST", f"/internal/jobs/{rid}/stop", worker=worker)
                reaped += 1
                _record_recovery_action(db_row, "Stale running job stopped",
                                        f"DB says stopped but runner still running; stopped {rid} on {worker}.",
                                        worker or "")
            except Exception as exc:
                logger.warning("reaper: stop failed for stale %s on %s: %s", rid, worker, exc)
    if reaped:
        logger.info("reaper: cleaned %d orphan/stale runner job(s)", reaped)
        # Fleet cache is now stale (we just deleted jobs)
        try:
            runner_client._fleet_cache["jobs"] = None  # type: ignore
        except Exception:
            pass
    return reaped


AUTO_DEPLOY_INTERVAL_S = int(os.getenv("AUTO_DEPLOY_INTERVAL_S", "600") or "0")

_auto_deploy_lock = threading.Lock()
_auto_deploy_last = 0.0
_auto_deploy_last_result = {"checked": 0, "updated": 0, "unchanged": 0, "failed": 0,
                            "errors": []}


def auto_deploy_status() -> dict:
    """What the last sweep found — the admin 🩺 panel shows this verbatim."""
    return {"enabled": AUTO_DEPLOY_INTERVAL_S > 0,
            "interval_s": AUTO_DEPLOY_INTERVAL_S,
            "last_run_s_ago": int(time.time() - _auto_deploy_last) if _auto_deploy_last else None,
            "last": dict(_auto_deploy_last_result)}


def auto_deploy_sweep(force: bool = False) -> dict:
    """Redeploy every app that follows its branch and whose branch has moved.

    The half of "deploy on a new commit, like Render" that runs with nobody
    watching. The site stores which commit each app was built from, so this pass
    compares it with the branch's current head and redeploys IN PLACE — same job
    id, same folder (the bot's database and sessions survive), same public
    address, same env. Only apps whose 👑 owner switched auto-deploy on are
    touched, and only when the commit actually differs, so a sweep that finds
    nothing is one SELECT plus one cached GitHub lookup per DISTINCT repo and no
    runner traffic at all.

    "GitHub did not answer" is deliberately NOT treated as a change: redeploying
    on a guess would restart a healthy bot because of a rate limit.
    """
    global _auto_deploy_last, _auto_deploy_last_result
    if AUTO_DEPLOY_INTERVAL_S <= 0:
        return {"disabled": True}
    with _auto_deploy_lock:
        now = time.time()
        if not force and _auto_deploy_last and now - _auto_deploy_last < AUTO_DEPLOY_INTERVAL_S:
            return {"waiting": True}
        _auto_deploy_last = now

    out = {"checked": 0, "updated": 0, "unchanged": 0, "failed": 0, "errors": []}
    try:
        from services import bot_ops, github_repo
    except Exception as exc:                                   # noqa: BLE001
        logger.warning("Auto-deploy sweep unavailable: %s", exc)
        out["errors"].append(str(exc))
        _auto_deploy_last_result = out
        return out

    try:
        rows = bot_ops.auto_deploy_jobs()
    except Exception as exc:                                   # noqa: BLE001
        logger.warning("Auto-deploy sweep could not read its job list: %s", exc)
        out["errors"].append(str(exc))
        _auto_deploy_last_result = out
        return out

    for row in rows:
        name = row.get("name") or f"#{row.get('id')}"
        out["checked"] += 1
        url = (row.get("repo_url") or "").strip()
        owner, repo, branch = github_repo.parse_repo(url)
        if not owner:
            out["failed"] += 1
            out["errors"].append(f"{name}: repo url not understood")
            continue
        current = (row.get("repo_commit") or "").strip()
        try:
            head = github_repo.head_commit(owner, repo, branch)
        except Exception as exc:                               # noqa: BLE001
            head = ""
            out["errors"].append(f"{name}: {type(exc).__name__}")
        if not head:
            out["unchanged"] += 1
            continue
        if head == current:
            out["unchanged"] += 1
            continue
        try:
            res = bot_ops.update_from_repo(row["user_id"], name, url)
        except Exception as exc:                               # noqa: BLE001
            logger.warning("Auto-deploy of %s crashed: %s", name, exc)
            out["failed"] += 1
            out["errors"].append(f"{name}: {type(exc).__name__}")
            continue
        if res.get("ok"):
            out["updated"] += 1
            logger.info("Auto-deployed %s: %s -> %s", name, (current or "?")[:7],
                        (res.get("commit") or head)[:7])
        else:
            out["failed"] += 1
            out["errors"].append(f"{name}: {res.get('error')}")
            logger.warning("Auto-deploy of %s failed: %s", name, res.get("error"))

    out["errors"] = out["errors"][:5]
    _auto_deploy_last_result = out
    return out


async def recover_background():
    """Runner services may also be waking; retry without blocking web startup."""
    await asyncio.sleep(5)
    for attempt in range(3):
        unresolved = await asyncio.to_thread(recover_once)
        if not unresolved:
            return
        if attempt < 2:
            await asyncio.sleep(25)
    logger.warning("Bot recovery finished with %d unresolved bot(s)", unresolved)


def _reconcile_loop():
    # Let the startup pass (recover_background) and the runner's own job
    # re-adoption finish first, so the first sweep is not racing them.
    time.sleep(90)
    while True:
        try:
            # Repair any row this site cannot decode BEFORE trying to recover
            # bots from it. A bot that is still running on the runner is never
            # "missing", so the recovery pass below would not look at its env at
            # all — and its next restart would fail on an unreadable row. This
            # is a cheap database scan when nothing is wrong (the usual case).
            from services import env_rescue
            if env_rescue.unreadable_count():
                env_rescue.rescue_unreadable_rows()
        except Exception as exc:
            logger.warning("Env rescue sweep crashed: %s", exc)
        try:
            recover_once()
        except Exception as exc:  # a sweep must never kill its own thread
            logger.warning("Bot recovery sweep crashed: %s", exc)
        try:
            reap_orphans()
        except Exception as exc:
            logger.warning("Reaper sweep crashed: %s", exc)
        try:
            # AFTER recovery, never instead of it: a runner that just restarted
            # has to get its jobs back before any of them is redeployed. The
            # sweep keeps its own (longer) clock, so it runs at most once per
            # AUTO_DEPLOY_INTERVAL_S however often this loop turns.
            auto_deploy_sweep()
        except Exception as exc:
            logger.warning("Auto-deploy sweep crashed: %s", exc)
        time.sleep(RECOVERY_INTERVAL_S)


def start_reconciler():
    """Start the periodic recovery thread (idempotent).

    This is what makes "I restarted the runner" a non-event: whatever the runner
    lost, the site notices within RECOVERY_INTERVAL_S and puts it back with its
    data. Set JOB_RECOVERY_INTERVAL_S=0 to switch it off.
    """
    global _reconciler_started
    if RECOVERY_INTERVAL_S < 60:
        logger.info("Periodic bot recovery disabled (JOB_RECOVERY_INTERVAL_S=%s)",
                    RECOVERY_INTERVAL_S)
        return
    with _reconciler_lock:
        if _reconciler_started:
            return
        _reconciler_started = True
    threading.Thread(target=_reconcile_loop, name="job-recovery",
                     daemon=True).start()
    logger.info("Periodic bot recovery started (every %ds)", RECOVERY_INTERVAL_S)
