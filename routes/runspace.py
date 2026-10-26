"""RunSpace: one-shot code execution proxy + 24/7 always-on jobs
(start/stop/restart, live logs SSE, public URL access control)."""
from typing import Optional, List

from fastapi import APIRouter, Header, HTTPException, Request

from routes.deps import *  # shared kernel (config, helpers, models)


class JobCreateRequest(BaseModel):
    name: str
    language: str
    code: str
    repo_url: Optional[str] = None
    entry: Optional[str] = None
    env: Optional[dict] = None
    telegram_verification_id: Optional[str] = None


class JobUpdateRequest(BaseModel):
    name: Optional[str] = None
    language: Optional[str] = None
    code: Optional[str] = None
    repo_url: Optional[str] = None
    entry: Optional[str] = None
    env: Optional[dict] = None
    telegram_verification_id: Optional[str] = None
    save_only: bool = False


class EntryPinPayload(BaseModel):
    path: str


class GithubImportRequest(BaseModel):
    url: str


import asyncio
import json
import logging
import re
import time
from urllib.parse import urlparse

from fastapi.responses import StreamingResponse

from services import runner_client
from services import limits
from services import abuse_control
from services import telegram_detector
from services import secrets_store
from services import bot_templates
from services.runner_client import MAX_JOBS_PER_USER

logger = logging.getLogger("codenest.runspace")

# Log-stream safety valves. A stream that cannot end will pin one request (and
# a worker thread per poll in embedded mode) until the server is exhausted.
SSE_POLL_INTERVAL_S = 1.5
SSE_MAX_LIFETIME_S = float(os.getenv("SSE_MAX_LIFETIME_S", "900"))  # 15 min, then reconnect

# Cross-account limits are on by default. Operators can explicitly disable
# them, but the safe production default prevents account farms from multiplying
# the ordinary per-account allowance.
CLUSTER_LIMITS_ENABLED = os.getenv("CLUSTER_LIMITS_ENABLED", "1").strip().lower() in ("1", "true", "yes")

router = APIRouter()


class ExecuteCodeRequest(BaseModel):
    language: str
    code: str
    stdin: Optional[str] = None


class TelegramTokenVerify(BaseModel):
    token: str


class TelegramCodeAnalyze(BaseModel):
    code: str
    language: Optional[str] = "python"


@router.get("/api/telegram-bot/templates")
def telegram_bot_templates(authorization: Optional[str] = Header(None)):
    get_current_user_and_session(authorization)
    return {"templates": bot_templates.list_templates()}


@router.get("/api/telegram-bot/templates/{template_id}")
def telegram_bot_template(template_id: str, authorization: Optional[str] = Header(None)):
    get_current_user_and_session(authorization)
    item = bot_templates.get_template(template_id)
    if not item:
        raise HTTPException(status_code=404, detail="Template not found.")
    return item


@router.post("/api/telegram-bot/analyze")
def analyze_telegram_bot(payload: TelegramCodeAnalyze,
                         authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    rate_limit_user(user["id"], "telegram_analyze")
    if not (payload.code or "").strip():
        raise HTTPException(status_code=400, detail="Paste or upload the bot code first.")
    return telegram_detector.analyze_code(payload.code, payload.language or "python")


@router.post("/api/telegram-bot/verify")
def verify_telegram_bot(payload: TelegramTokenVerify,
                        authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    rate_limit_user(user["id"], "telegram_verify")
    token = (payload.token or "").strip()
    if not telegram_detector.TOKEN_RE.fullmatch(token):
        raise HTTPException(status_code=400, detail="That does not look like a Telegram bot token.")
    meta = telegram_detector.inspect_bot(token, timeout=2)
    if meta.get("check_status") == "invalid_token":
        raise HTTPException(status_code=400, detail="Telegram rejected this token. Copy a fresh token from @BotFather.")
    if meta.get("check_status") != "verified" or not meta.get("username"):
        raise HTTPException(status_code=503, detail="Telegram could not verify the bot right now. Try again shortly.")
    out = telegram_detector.public_fields(meta)
    out["telegram_verification_id"] = telegram_detector.issue_verification(user["id"], token, meta)
    out["expires_in"] = 900
    return out


@router.post("/api/execute")
def execute_code(payload: ExecuteCodeRequest, request: Request, authorization: Optional[str] = Header(None)):
    """Proxy code execution to the separate runner service.

    User → this endpoint (auth required) → runner service (shared secret).
    The user NEVER sees the runner URL or secret — those stay server-side.
    """
    # 1) User must be logged in.
    user, _ = get_current_user_and_session(authorization)

    # 2) Rate limit — per ACCOUNT (never per-IP: CGNAT-shared mobile IPs
    #    would let strangers burn each other's allowance).
    rate_limit_user(user["id"], "exec")

    # 3) Forward to the runner (embedded in-process, or remote when
    #    RUNNER_SERVICE_URL is set). Secret never leaves the server.
    try:
        response = runner_client._runner_http("POST", "/internal/execute", {
            "language": payload.language,
            "code": payload.code,
            "stdin": payload.stdin or "",
        })
    except HTTPException:
        raise
    except Exception:
        logger.error("Runner call failed unexpectedly")
        raise HTTPException(
            status_code=503,
            detail="Code execution service is temporarily unavailable. Please try again later.",
        )

    if response.status_code == 401:
        raise HTTPException(status_code=500, detail="Runner authentication failed. Contact admin.")
    if response.status_code == 403:
        raise HTTPException(status_code=500, detail="Runner secret mismatch. Contact admin.")
    if response.status_code != 200:
        detail = None
        try:
            detail = response.json().get("detail")
        except Exception:
            pass
        raise HTTPException(
            status_code=502,
            detail=detail or "Code execution service returned an error ({}).".format(response.status_code),
        )

    result = response.json()
    # Pass through stdout/stderr/exit_code/execution_time to the user.
    # The runner URL and secret are NEVER in this response.
    return result


# ================================
# ALWAYS-ON JOBS (24/7 background tasks — mini PythonAnywhere)
# ================================
# Job DEFINITIONS live in our DB (survive runner restarts); the PROCESSES run
# inside the runner service. Same secret, same proxy pattern as /api/execute.

def _get_own_job(job_id: int, user: dict) -> dict:
    """Fetch a job row owned by this user or 404."""
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user["id"])).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found.")
    return dict(row)


def _worker_of(row) -> str:
    """Which worker this job physically runs on.

    Every per-job call must go to THIS worker, not to whichever one happens to
    be first in the pool. NULL means the row predates the column, and those
    jobs all live on the primary worker — the fallback inside _runner_http.
    """
    try:
        return (dict(row).get("worker_url") or "") or None
    except Exception:
        return None


def _remember_worker(job_db_id: int, resp) -> None:
    """Persist the worker a create landed on, so later calls can find it."""
    placed = getattr(resp, "placed_on", None)
    if not placed or not job_db_id:
        return
    conn = get_db_connection()
    try:
        conn.execute("UPDATE jobs SET worker_url = ? WHERE id = ?", (placed, job_db_id))
        conn.commit()
    except Exception as exc:            # never fail a launch over bookkeeping
        logger.warning("could not record worker for job %s: %s", job_db_id, exc)
    finally:
        conn.close()


def _row_env(row, rescue: bool = True) -> dict:
    """Env vars saved for a job row (empty when unset / unparsable).

    Mirrors bot_ops._row_env, including the rescue: a row this site cannot
    decode is repaired from the runner's own copy instead of being read as
    empty. Empty is what the Env tab would then show — and what a Save would
    then write, silently deleting every other variable the app had.

    Also promotes a BOT_TOKEN found in the source file into the returned map
    when Env has none — Telegram-board deploys often skip the Env tab. This is
    in-memory only; the DB row is not rewritten.

    Pass rescue=False where this runs in a loop over many jobs (a list
    endpoint): the rescue is one HTTP call per broken row, and a list must not
    stall on a runner that is asleep.
    """
    try:
        raw = dict(row).get("env")
    except Exception:
        values, readable = {}, True
    else:
        values, readable = secrets_store.read_env(raw)
    if not readable and rescue:
        try:
            from services import env_rescue
            got, _outcome = env_rescue.rescue_job_env(row)
            if got:
                values = got
        except Exception:
            pass
    try:
        from services import bot_ops
        values = bot_ops.ensure_bot_token_in_env(row, values)
    except Exception:
        pass
    return values


def _account_flags(user_id: int) -> dict:
    """What this account is allowed, for the dashboard to show.

    The 👑 flag travels with the one call the page already makes (/api/jobs), so
    no extra request and no second source of truth. Before this a queen grant
    was invisible on the website: the chat bot knew, the runner knew, and the
    page the owner actually looks at did not — which is how "no memory ceiling"
    stays an abstraction instead of something you can see.
    """
    try:
        from services import bot_ops
        return bot_ops.account_privileges(user_id)
    except Exception:
        return {"is_queen": False, "job_limit": MAX_JOBS_PER_USER,
                "mem_limit_mb": None, "zip_max_mb": None, "zip_max_files": None}


def _job_limit_for(user_id: int) -> int:
    """How many apps this account may have RUNNING.

    bot_ops owns the rule (global default + the per-user override an admin sets
    with /admin limit). This endpoint used to check the global constant, so an
    override granted in chat applied to bots created from chat and quietly NOT
    to the same person's bots created from the browser — the website told them
    they were full at 3 while the bot happily made a 4th.
    """
    try:
        from services import bot_ops
        return bot_ops.effective_job_limit(user_id)
    except Exception:
        return MAX_JOBS_PER_USER


def _mem_limit_for(user_id: int):
    """The 👑 (users.mem_unlimited) flag in the runner's terms: 0 means "no
    per-job memory ceiling", None means "runner default".

    bot_ops owns the rule and the Telegram bot already sent it on every
    deploy — the web editor sent nothing at all, so a 👑 granted by an admin
    applied to bots created from chat and quietly NOT to the same person's
    bots created from the browser. One shared helper is the fix; two copies of
    the rule is how they would drift apart again.
    """
    try:
        from services import bot_ops
        return bot_ops.mem_limit_for(user_id)
    except Exception:
        return None


def _restore_then_restart(job_id: int, info: dict, worker: str = None) -> dict:
    """After a COLD start, push the last snapshot into the fresh workspace.

    A cold start means the runner lost the directory (a deploy on the free
    tier). The job is already running at this point, against an empty dir — a
    bot that opened database.db has just created a blank one. So we restore the
    saved data files and restart the process in place so it re-opens them.

    Entirely best-effort: if there is no snapshot, or the runner refuses, the
    job keeps running exactly as it did before this feature existed.
    """
    rid = info.get("id")
    if not rid:
        return info
    try:
        from services import snapshots
        res = snapshots.restore_snapshot(job_id, rid, overwrite=True, worker=worker)
        if res.get("restored"):
            r = runner_client._runner_http("POST", f"/internal/jobs/{rid}/restart", worker=worker)
            if r.status_code == 200:
                fresh = r.json()
                fresh["restored_files"] = res["restored"]
                return fresh
            info["restored_files"] = res["restored"]
    except Exception as exc:  # never block a start on a restore problem
        logging.getLogger(__name__).warning(
            "snapshot restore after cold start failed (job %s): %s", job_id, exc)
    return info


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_ENV_BLOCKED = {
    "PATH", "PYTHONPATH", "PORT", "HOME", "LD_PRELOAD", "LD_LIBRARY_PATH",
    "PYTHONSTARTUP", "PYTHONHOME", "BASH_ENV", "ENV", "SHELL", "IFS",
    "RUNNER_SERVICE_SECRET", "DATABASE_URL",
}


def _telegram_columns(meta):
    return (1 if meta.get("detected") else 0, meta.get("username"),
            meta.get("bot_id"), meta.get("check_status"), meta.get("verified_at"))


def _reject_duplicate_bot_token(fingerprint, exclude_job_id=None):
    """One Telegram token may have only one deployed poller on CodeNest."""
    conn = get_db_connection()
    try:
        sql = ("SELECT id,runner_job_id FROM jobs WHERE telegram_token_fingerprint=? "
               "AND runner_job_id IS NOT NULL")
        params = [fingerprint]
        if exclude_job_id is not None:
            sql += " AND id != ?"
            params.append(exclude_job_id)
        rows = [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    finally:
        conn.close()
    if not rows:
        return
    live = limits.running_runner_ids()
    # Fail closed when the runner cannot answer: launching a duplicate is more
    # damaging than asking the owner to stop/delete the existing deployment.
    if not live or any(r.get("runner_job_id") in live for r in rows):
        raise HTTPException(
            status_code=409,
            detail="This Telegram token is already attached to another deployed bot. Stop or delete that bot before reusing the token.",
        )


def _create_revision(conn, user_id, job_id, language, code, action="deploy", status="building"):
    row = conn.execute("SELECT COALESCE(MAX(version),0)+1 AS v FROM bot_revisions WHERE job_id=?",
                       (job_id,)).fetchone()
    version = int(dict(row)["v"])
    cur = conn.execute(
        "INSERT INTO bot_revisions (job_id,user_id,version,action,language,code,status,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (job_id, user_id, version, action, language, code, status, now_utc_str()),
    )
    return cur.lastrowid, version


def _finish_revision(revision_id, status, error=""):
    safe_error = telegram_detector.TOKEN_RE.sub("[redacted]", str(error or ""))[:500]
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE bot_revisions SET status=?,error=?,promoted_at=? WHERE id=?",
            (status, safe_error or None, now_utc_str() if status == "healthy" else None,
             revision_id),
        )
        conn.commit()
    finally:
        conn.close()


def _record_deploy_event(conn, user_id, job_id, action, job_name, meta, created_at):
    conn.execute(
        "INSERT INTO job_deploy_events (user_id,job_id,action,job_name,"
        "telegram_bot_detected,telegram_bot_username,telegram_bot_id,"
        "telegram_check_status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (user_id, job_id, action, job_name, 1 if meta.get("detected") else 0,
         meta.get("username"), meta.get("bot_id"), meta.get("check_status"), created_at),
    )


def _attach_telegram_public(row):
    row.pop("telegram_token_fingerprint", None)
    row.update(telegram_detector.public_fields({
        "detected": bool(row.get("telegram_bot_detected")),
        "username": row.get("telegram_bot_username"),
        "bot_id": row.get("telegram_bot_id"),
        "check_status": row.get("telegram_check_status"),
        "verified_at": row.get("telegram_verified_at"),
    }))
    return row


_SECRET_MASK = "••••••••"
_SECRET_KEY_RE = re.compile(r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY)", re.I)


def _public_env(values):
    return {k: (_SECRET_MASK if _SECRET_KEY_RE.search(k) else v)
            for k, v in dict(values or {}).items()}


def _restore_masked_env(values, existing):
    out = dict(values or {})
    for key, value in list(out.items()):
        if value == _SECRET_MASK and key in existing:
            out[key] = existing[key]
    return out


def _clean_env_map(raw) -> dict:
    """Validate env vars here too — never trust the browser to have done it."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        k = str(k).strip()
        if not _ENV_KEY_RE.match(k) or k.upper() in _ENV_BLOCKED:
            continue
        out[k] = ("" if v is None else str(v))[:4096]
        if len(out) >= 40:
            break
    return out


@router.post("/api/jobs")
def create_job(payload: JobCreateRequest, request: Request, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    rate_limit_user(user["id"], "exec")

    # Device/IP are still RECORDED (cheap, and useful for future abuse work),
    # but cross-account cluster limiting is OFF by default: the current scope is
    # a simple per-account cap. The fingerprint/IP cluster implementation lives
    # in services/limits.py and is re-enabled with CLUSTER_LIMITS_ENABLED=1.
    fp = normalise_fingerprint(request.headers.get("X-Fingerprint", "")[:4000])
    ip = client_ip(request)
    abuse_control.enforce(fingerprint=fp, ip=ip, action="job")

    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE users SET fingerprint = COALESCE(NULLIF(?, ''), fingerprint), last_ip = ? WHERE id = ?",
            (fp, ip, user["id"]),
        )
        conn.commit()
        if CLUSTER_LIMITS_ENABLED:
            limits.check_job_quota(conn, user["id"], fp, ip)
    finally:
        conn.close()

    name = (payload.name or "").strip()[:60]
    repo_url = (payload.repo_url or "").strip()
    entry = (payload.entry or "").strip()
    if not name and repo_url:
        m = re.search(r"github\.com/[^/]+/([^/]+)", repo_url)
        name = (m.group(1) if m else "repo").replace(".git","")[:60]
    if not name:
        raise HTTPException(status_code=422, detail="Give the job a name.")
    if not (payload.code or "").strip() and not repo_url:
        raise HTTPException(status_code=422, detail="Provide code or a repo URL.")

    # Per-user name uniqueness (case-insensitive)
    conn = get_db_connection()
    try:
        dup = conn.execute(
            "SELECT id FROM jobs WHERE user_id = ? AND LOWER(name) = LOWER(?)",
            (user["id"], name),
        ).fetchone()
        if dup:
            conn.close()
            raise HTTPException(status_code=409, detail=f"You already have a job named \u201c{name}\u201d \u2014 choose a different name.")
        # Per-account cap on CONCURRENT jobs (§2). Counting every row ever
        # created would permanently lock a user out after 3 lifetime jobs even
        # if all of them were stopped, so only jobs the runner reports as alive
        # count. If the runner is unreachable we fall back to the row count
        # rather than letting the cap disappear entirely.
        rows = conn.execute(
            "SELECT runner_job_id,desired_state FROM jobs WHERE user_id = ?", (user["id"],)
        ).fetchall()
        live_ids = limits.running_runner_ids()
        if live_ids:
            active = sum(1 for r in rows if dict(r).get("runner_job_id") in live_ids)
        else:
            active = sum(1 for r in rows if dict(r).get("desired_state") != "stopped")
        limit = _job_limit_for(user["id"])
        if active >= limit:
            conn.close()
            raise HTTPException(
                status_code=429,
                detail=(f"You already have {active} of {limit} apps running — "
                        f"stop one before adding another. A stopped app stays "
                        f"yours and frees its slot."),
            )
    except HTTPException:
        raise

    env_map = _clean_env_map(payload.env)
    verified_token = str(env_map.get("BOT_TOKEN") or "").strip()
    bot_meta = telegram_detector.validate_verification(
        user["id"], payload.telegram_verification_id, verified_token)
    if not bot_meta:
        raise HTTPException(status_code=400,
                            detail="Verify your Telegram bot token first, then add the bot.")
    token_fingerprint = telegram_detector.token_fingerprint(verified_token)
    _reject_duplicate_bot_token(token_fingerprint)
    code_analysis = telegram_detector.analyze_code(
        payload.code or "", payload.language or "python")
    canonical_code, env_map = telegram_detector.secure_bot_source(
        payload.code or "", env_map, verified_token, payload.language or "python")
    body = {
        "language": payload.language or "python",
        "code": canonical_code,
        "name": f"u{user['id']}-{name}",
        "env": env_map,
        "mem_limit_mb": _mem_limit_for(user["id"]),
    }
    if repo_url:
        body["repo_url"] = repo_url
        if entry: body["entry"] = entry
    resp = runner_client._runner_http("POST", "/internal/jobs", body)
    if resp.status_code == 201:
        info = resp.json()
    elif resp.status_code in (401, 403):
        raise HTTPException(status_code=500, detail="Runner secret mismatch. Contact admin.")
    else:
        try:
            detail = resp.json().get("detail", "Runner rejected the job.")
        except Exception:
            detail = "Runner rejected the job."
        raise HTTPException(status_code=resp.status_code if 400 <= resp.status_code < 500 else 502, detail=detail)

    now = now_utc_str()
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO jobs (user_id, name, language, code, runner_job_id, env,
                telegram_bot_detected,telegram_bot_username,telegram_bot_id,
                telegram_check_status,telegram_verified_at,telegram_token_fingerprint,
                telegram_framework,telegram_update_mode,telegram_token_source,
                repo_url,repo_entry,repo_commit,created_at,updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user["id"], name, payload.language, canonical_code, info["id"],
             secrets_store.pack_env(env_map), *_telegram_columns(bot_meta), token_fingerprint,
             code_analysis["framework"], code_analysis["update_mode"],
             code_analysis["token_source"],
             # Which repo this was built from and the commit that was checked
             # out. The runner reports both; storing them is what lets the chat
             # offer ⬆️ "deploy latest" for a job created on the WEBSITE too, so
             # there is one answer to "what is running?" whichever door it came
             # through.
             (repo_url or None), (info.get("repo_entry") or entry or None),
             (info.get("repo_commit") or None), now, now),
        )
        revision_id, version = _create_revision(
            conn, user["id"], cursor.lastrowid, payload.language, canonical_code,
            action="deploy", status="healthy")
        conn.execute("UPDATE bot_revisions SET promoted_at=? WHERE id=?", (now, revision_id))
        _record_deploy_event(conn, user["id"], cursor.lastrowid, "run", name, bot_meta, now)
        conn.commit()
        telegram_detector.consume_verification(payload.telegram_verification_id)
        info["job_db_id"] = cursor.lastrowid
        info["revision"] = version
        info.update(telegram_detector.public_fields(bot_meta))
        info.update({"telegram_framework": code_analysis["framework"],
                     "telegram_update_mode": code_analysis["update_mode"],
                     "telegram_token_source": code_analysis["token_source"]})
        # Remember WHICH worker accepted this job. Every later restart / stop /
        # log call reads it back, so a job on worker-B is never addressed to
        # worker-A once a second worker exists.
        _remember_worker(cursor.lastrowid, resp)
        info.update(runner_client._job_web_fields(info, getattr(resp, "placed_on", None)))  # web / web_url
        return info
    finally:
        conn.close()


@router.get("/api/jobs")
def list_jobs(authorization: Optional[str] = Header(None)):
    """Return saved bot metadata enriched with fleet health (cached, fast).

    BUG FIX: previously returned status=\"running\" for every desired_state!=stopped
    without checking the fleet, so a stopped/offline runner still showed a green
    \"running\" badge. Now fleet_jobs() (memoised 3s, parallel probe) is consulted:
    stopped stays stopped, a runner_job_id present in the fleet reports its real
    status, and a desired-running job missing from the fleet is shown as offline
    (recovering) rather than a lie. This is still a single cached fleet call, not
    N per-job probes, so the 2-minute spinner does not return.
    """
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY id DESC", (user["id"],)
        ).fetchall()
    finally:
        conn.close()

    try:
        live = runner_client.fleet_jobs()
    except Exception:
        live = {}
    jobs = []
    for stored in rows:
        row = dict(stored)
        desired = row.get("desired_state")
        rid = row.get("runner_job_id")
        if desired == "stopped":
            row["status"] = "stopped"
            row["status_stale"] = False
        elif rid and rid in live:
            row["status"] = (live[rid].get("status") or "running")
            row["status_stale"] = False
        elif desired != "stopped" and rid:
            # Runner answered (fleet non-None) but job not there → truly gone
            # or on a worker that did not answer this refresh. Show offline,
            # let job_recovery or a manual Restart bring it back; do not claim
            # it is running when no runner reports it.
            row["status"] = "offline"
            row["status_stale"] = True
        else:
            row["status"] = "stopped" if desired == "stopped" else "offline"
            row["status_stale"] = True
        row["env"] = _public_env(_row_env(row, rescue=False))
        _attach_telegram_public(row)
        row.pop("code", None)
        jobs.append(row)
    flags = _account_flags(user["id"])
    out = {"jobs": jobs, "runner": "background",
           # The limit this ACCOUNT has (an admin can raise it per user), not the
           # global default — the dashboard and the cap check now quote the same
           # number, which is what "5/3 running slots" was really about.
           "max_per_user": flags.get("job_limit") or MAX_JOBS_PER_USER}
    out.update(flags)
    return out


@router.get("/api/jobs/{job_id}")
def get_job(job_id: int, authorization: Optional[str] = Header(None)):
    """Return saved code + accurate live status (direct probe, not stale cache).

    BUG FIX: previously returned \"running\" for any desired!=stopped without
    checking the runner, so a stopped/offline runner still showed green. Now we
    probe the job directly (GET /internal/jobs/{rid} on its worker) — this is a
    single HTTP (or in-process) call, not the memoised fleet cache, so
    `ra._jobs.clear()` is seen immediately and \"registry loss alone would
    report offline\" holds. Fleet cache (3s) would still lie for up to 3s.
    When the job is live we also merge web/port/mem fields so the web-gateway
    tests see `web`/`port` without a second call.
    """
    user, _ = get_current_user_and_session(authorization)
    row = dict(_get_own_job(job_id, user))
    desired = row.get("desired_state")
    rid = row.get("runner_job_id")
    worker = _worker_of(row)

    # Desired stopped is authoritative — even if runner still has a record,
    # dashboard must show stopped (reaper will kill the process shortly).
    if desired == "stopped":
        row["status"] = "stopped"
        row["status_stale"] = False
        row["env"] = _public_env(_row_env(row))
        _attach_telegram_public(row)
        return row

    live_info = None
    live_unreachable = False
    if rid:
        try:
            resp = runner_client._runner_http("GET", f"/internal/jobs/{rid}", worker=worker)
            if resp.status_code == 200:
                live_info = resp.json() or {}
            elif resp.status_code == 404:
                live_info = None
            else:
                live_info = None
        except Exception:
            live_unreachable = True
            live_info = None

    if live_info is not None:
        row["status"] = (live_info.get("status") or "running")
        row["status_stale"] = False
        # Expose live fields the dashboard and tests expect
        for k in ("web", "web_slug", "port", "web_public", "access_key",
                  "mem_mb", "peak_mem_mb", "uptime_s", "restarts",
                  "last_exit_reason", "last_exit_code", "oom", "logs"):
            if k in live_info:
                row[k] = live_info[k]
        try:
            row.update(runner_client._job_web_fields(live_info, worker))
        except Exception:
            pass
    else:
        # No live record — offline. For the editor fast-path (no runner_job_id
        # or runner unreachable) we must report stale True so the UI knows a
        # background refresh is needed and the fast test (`never_waits`) passes.
        # A definitive 404 (runner answered) is still offline but stale True is
        # harmless and keeps the editor instant.
        row["status"] = "offline"
        row["status_stale"] = True
    row["env"] = _public_env(_row_env(row))
    _attach_telegram_public(row)
    return row


@router.get("/api/jobs/{job_id}/telegram-health")
def telegram_job_health(job_id: int, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    rate_limit_user(user["id"], "telegram_health")
    row = dict(_get_own_job(job_id, user))
    token = str(_row_env(row).get("BOT_TOKEN") or "").strip()
    if not token:
        raise HTTPException(status_code=404, detail="No BOT_TOKEN in Env or source. Paste one in Env, or put BOT_TOKEN=... in the code.")
    health = telegram_detector.telegram_delivery_health(
        token, row.get("telegram_update_mode") or "unknown")
    health.update({"process_status": "offline", "runtime_conflict": False,
                   "checked_at": now_utc_str()})
    rid = row.get("runner_job_id")
    if rid:
        try:
            resp = runner_client._runner_http("GET", f"/internal/jobs/{rid}", worker=_worker_of(row))
            if resp.status_code == 200:
                live = resp.json() or {}
                health["process_status"] = live.get("status") or "unknown"
                logs = str(live.get("logs") or "").lower()
                conflict = ("terminated by other getupdates" in logs or
                            "terminated by other getupdates request" in logs or
                            ("409 conflict" in logs and "getupdates" in logs))
                unauthorized = ("unauthorized" in logs and "telegram" in logs)
                health["runtime_conflict"] = conflict
                if conflict:
                    health["delivery_status"] = "duplicate_poller"
                elif unauthorized:
                    health["delivery_status"] = "invalid_token"
                elif (health["delivery_status"] == "telegram_ready" and
                      health["process_status"] == "running"):
                    health["delivery_status"] = "running_unconfirmed"
        except Exception:
            health["process_status"] = "unknown"
    return health


@router.get("/api/jobs/{job_id}/revisions")
def list_bot_revisions(job_id: int, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    _get_own_job(job_id, user)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id,version,action,language,status,error,created_at,promoted_at "
            "FROM bot_revisions WHERE job_id=? AND user_id=? ORDER BY version DESC LIMIT 50",
            (job_id, user["id"]),
        ).fetchall()
        current = conn.execute(
            "SELECT version FROM bot_revisions WHERE job_id=? AND status='healthy' "
            "AND promoted_at IS NOT NULL ORDER BY promoted_at DESC,id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        return {"revisions": [dict(r) for r in rows],
                "current_revision": dict(current)["version"] if current else None}
    finally:
        conn.close()


@router.post("/api/jobs/{job_id}/revisions/{revision_id}/rollback")
def rollback_bot_revision(job_id: int, revision_id: int,
                          authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    row = dict(_get_own_job(job_id, user))
    conn = get_db_connection()
    try:
        rev = conn.execute(
            "SELECT id,version,language,code,status FROM bot_revisions "
            "WHERE id=? AND job_id=? AND user_id=?",
            (revision_id, job_id, user["id"]),
        ).fetchone()
    finally:
        conn.close()
    if not rev:
        raise HTTPException(status_code=404, detail="Revision not found.")
    rev = dict(rev)
    if rev.get("status") != "healthy":
        raise HTTPException(status_code=409, detail="Only a previously healthy revision can be restored.")
    rid = row.get("runner_job_id")
    if not rid:
        raise HTTPException(status_code=409, detail="Restart this bot once before rolling it back.")
    env = _row_env(row)
    body = {"name": row["name"], "language": rev["language"],
            "code": rev["code"], "env": env,
            "mem_limit_mb": _mem_limit_for(user["id"])}
    try:
        resp = runner_client._runner_http("PATCH", f"/internal/jobs/{rid}", body,
                                          worker=_worker_of(row))
    except Exception:
        raise HTTPException(status_code=503, detail="Runner is unreachable; the current revision was kept.")
    new_worker = _worker_of(row)
    new_runner_id = rid
    if resp.status_code == 200:
        info = resp.json()
    elif resp.status_code == 404:
        resp = runner_client._runner_http("POST", "/internal/jobs", {
            **body, "name": f"u{user['id']}-{row['name']}"})
        if resp.status_code != 201:
            raise HTTPException(status_code=502, detail="Runner rejected rollback; the current revision was kept.")
        info = resp.json()
        new_runner_id = info["id"]
        new_worker = getattr(resp, "placed_on", None)
        info = _restore_then_restart(job_id, info, new_worker)
    else:
        raise HTTPException(status_code=502, detail="Runner rejected rollback; the current revision was kept.")

    analysis = telegram_detector.analyze_code(rev["code"], rev["language"])
    now = now_utc_str()
    meta = {"detected": bool(row.get("telegram_bot_detected")),
            "username": row.get("telegram_bot_username"),
            "bot_id": row.get("telegram_bot_id"),
            "check_status": row.get("telegram_check_status"),
            "verified_at": row.get("telegram_verified_at")}
    conn = get_db_connection()
    try:
        new_revision_id, version = _create_revision(
            conn, user["id"], job_id, rev["language"], rev["code"],
            action=f"rollback_to_v{rev['version']}", status="healthy")
        conn.execute("UPDATE bot_revisions SET promoted_at=? WHERE id=?", (now, new_revision_id))
        conn.execute(
            "UPDATE jobs SET language=?,code=?,runner_job_id=?,worker_url=?,desired_state='running',"
            "telegram_framework=?,telegram_update_mode=?,telegram_token_source=?,updated_at=? WHERE id=?",
            (rev["language"], rev["code"], new_runner_id, new_worker,
             analysis["framework"], analysis["update_mode"], analysis["token_source"],
             now, job_id),
        )
        _record_deploy_event(conn, user["id"], job_id, "rollback", row["name"], meta, now)
        conn.commit()
    finally:
        conn.close()
    info.update({"job_db_id": job_id, "revision": version,
                 "rolled_back_from": rev["version"]})
    info.update(telegram_detector.public_fields(meta))
    info.update(runner_client._job_web_fields(info, new_worker))
    return info


# ── FILE BROWSER ───────────────────────────────────────────────────────────
# Thin pass-throughs to the runner. Ownership is checked here via
# _get_own_job(); the runner only ever sees its own job id, so one user can
# never read another's workspace.

@router.get("/api/jobs/{job_id}/files")
def job_files(job_id: int, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    row = _get_own_job(job_id, user)
    rid = row.get("runner_job_id")
    if not rid:
        return {"files": [], "entry": None, "truncated": False}
    resp = runner_client._runner_http("GET", f"/internal/jobs/{rid}/files",
                                      worker=_worker_of(row))
    if resp.status_code == 404:
        return {"files": [], "entry": None, "truncated": False,
                "note": "runner restarted — press Restart to relaunch"}
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Could not list files.")
    return resp.json()


@router.get("/api/jobs/{job_id}/file")
def job_file_read(job_id: int, path: str, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    row = _get_own_job(job_id, user)
    rid = row.get("runner_job_id")
    if not rid:
        raise HTTPException(status_code=409, detail="Job is not running.")
    from urllib.parse import quote
    resp = runner_client._runner_http(
        "GET", f"/internal/jobs/{rid}/file?path={quote(path, safe='')}",
        worker=_worker_of(row))
    if resp.status_code != 200:
        # Forward the runner's own explanation (too big / binary / missing)
        # rather than flattening every case into one unhelpful 502.
        try:
            detail = resp.json().get("detail") or "Could not read that file."
        except Exception:
            detail = "Could not read that file."
        raise HTTPException(status_code=resp.status_code, detail=detail)
    return resp.json()


@router.post("/api/jobs/{job_id}/entry")
def job_set_entry(job_id: int, payload: EntryPinPayload,
                  authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    row = _get_own_job(job_id, user)
    rid = row.get("runner_job_id")
    if not rid:
        raise HTTPException(status_code=409, detail="Start the job once first.")
    resp = runner_client._runner_http(
        "POST", f"/internal/jobs/{rid}/entry", json_body={"path": payload.path},
        worker=_worker_of(row))
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail") or "Could not set the entry point."
        except Exception:
            detail = "Could not set the entry point."
        raise HTTPException(status_code=resp.status_code, detail=detail)
    return resp.json()


@router.get("/api/jobs/{job_id}/logs")
def job_logs(job_id: int, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    row = _get_own_job(job_id, user)
    rid = row.get("runner_job_id")
    if not rid:
        return {"status": "offline", "logs": "(never started)"}
    resp = runner_client._runner_http("GET", f"/internal/jobs/{rid}", worker=_worker_of(row))
    if resp.status_code == 404:
        return {"status": "offline", "logs": "(runner restarted — press ▶ Restart to relaunch)"}
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Could not fetch logs from runner.")
    info = resp.json()
    return {"status": info.get("status"), "logs": info.get("logs", ""), "uptime_s": info.get("uptime_s", 0), "restarts": info.get("restarts", 0)}


@router.get("/api/jobs/{job_id}/logs/stream")
async def job_logs_stream(job_id: int, request: Request, token: Optional[str] = None):
    """Server-Sent Events: push a job's logs to the dashboard in real time.

    EventSource can't send Authorization headers, so the session token comes
    as a ?token= query param; we validate it against the sessions table the
    same way get_current_user_and_session does.
    """
    token = (token or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    conn = get_db_connection()
    try:
        # Match deps.get_current_user_and_session: lazy migrate + honour expiry
        from routes.deps import _ensure_expires_column
        _ensure_expires_column(conn)
        session_row = conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
        if not session_row:
            raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
        try:
            from datetime import datetime, timezone, timedelta
            exp = session_row["expires_at"] if "expires_at" in session_row.keys() else None
            if exp:
                exp_dt = datetime.strptime(exp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > exp_dt:
                    conn.execute("DELETE FROM sessions WHERE id = ?", (session_row["id"],))
                    conn.commit()
                    raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
        except HTTPException:
            raise
        except Exception:
            pass
        conn.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (now_utc_str(), session_row["id"]))
        # Sliding expiry mirror (same logic as get_current_user_and_session)
        try:
            from datetime import datetime, timezone, timedelta as _td
            from routes.deps import SESSION_TTL_DAYS
            exp_str = session_row["expires_at"] if "expires_at" in session_row.keys() else None
            if exp_str:
                exp_d = datetime.strptime(exp_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                now_d = datetime.now(timezone.utc)
                if exp_d - now_d < _td(days=SESSION_TTL_DAYS - 1):
                    new_exp = (now_d + _td(days=SESSION_TTL_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
                    conn.execute("UPDATE sessions SET expires_at = ? WHERE id = ?", (new_exp, session_row["id"]))
        except Exception:
            pass
        conn.commit()
        user_row = conn.execute("SELECT * FROM users WHERE id = ?", (session_row["user_id"],)).fetchone()
        if not user_row:
            raise HTTPException(status_code=401, detail="Account not found.")
        if "is_suspended" in user_row.keys() and user_row["is_suspended"]:
            raise HTTPException(status_code=401, detail="This account is suspended.")
    finally:
        conn.close()

    row = _get_own_job(job_id, user_row)
    rid = row.get("runner_job_id")

    async def gen():
        """Push status+logs until the client goes away.

        This loop MUST be able to end. Without a disconnect check it ran
        forever: every job a user opened pinned one request (and, in embedded
        mode, a worker thread per poll) until the server ran out of capacity
        and the whole RunSpace UI froze. Closing a browser tab does not raise
        inside the generator, so we poll request.is_disconnected() and also
        cap the total lifetime — the client reconnects automatically, which is
        exactly what EventSource is designed to do.
        """
        last = None
        started = time.monotonic()
        try:
            while True:
                if await request.is_disconnected():
                    break
                if time.monotonic() - started > SSE_MAX_LIFETIME_S:
                    # Tell the client to reconnect, then close this one cleanly.
                    yield "event: reconnect\ndata: {}\n\n"
                    break

                info = None
                if rid:
                    try:
                        resp = await asyncio.to_thread(_runner_http, "GET", f"/internal/jobs/{rid}")
                        if resp.status_code == 200:
                            info = resp.json()
                    except Exception:
                        info = None
                payload = {
                    "status": (info or {}).get("status", "offline"),
                    "logs": (info or {}).get("logs", "(Waking up your RunSpace... this can take up to a minute on the free tier)"),
                    "uptime_s": (info or {}).get("uptime_s", 0),
                    "restarts": (info or {}).get("restarts", 0),
                }
                blob = json.dumps(payload, ensure_ascii=False)
                if blob != last:
                    last = blob
                    yield f"data: {blob}\n\n"
                else:
                    # Comment frame doubles as a keep-alive AND as the write
                    # that surfaces a dead peer to the transport.
                    yield ": ping\n\n"
                await asyncio.sleep(SSE_POLL_INTERVAL_S)
        except asyncio.CancelledError:  # client aborted mid-write
            raise
        except Exception as exc:  # noqa: BLE001 — never leak a stack trace into the stream
            logger.warning("log stream for job %s ended: %s", job_id, exc)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/jobs/{job_id}/files")
def list_job_files(job_id: int, authorization: Optional[str] = Header(None)):
    """List candidate downloadable files from the job workspace (best-effort).
    Walks the runner job dir and returns regular files with sizes. Used by the
    drawer "Download database" button to offer the most-likely DB file first.
    """
    import os
    from pathlib import Path
    user, _ = get_current_user_and_session(authorization)
    row = _get_own_job(job_id, user)
    rid = row.get("runner_job_id")
    out = []
    if rid:
        try:
            resp = runner_client._runner_http("GET", f"/internal/jobs/{rid}", worker=_worker_of(row))
            if resp.status_code == 200:
                info = resp.json()
                jdir = info.get("dir") or ""
                if jdir and os.path.isdir(jdir):
                    # Skip system/hidden dirs (pylibs, .git, node_modules)
                    skip_dirs = {"pylibs", ".git", "node_modules", "__pycache__", ".venv", "venv"}
                    root = Path(jdir)
                    for p in root.rglob("*"):
                        try:
                            if not p.is_file(): continue
                            rel = p.relative_to(root)
                            if any(part in skip_dirs for part in rel.parts): continue
                            size = p.stat().st_size
                            if size > 32 * 1024 * 1024: continue  # cap at 32MB
                            out.append({"path": str(rel).replace(os.sep, "/"), "size": size})
                        except Exception:
                            pass
        except Exception:
            pass
    # Sort databases (.db/.sqlite/.sqlite3) first, then by path
    out.sort(key=lambda f: (0 if f["path"].lower().endswith((".db",".sqlite",".sqlite3",".json")) else 1, f["path"]))
    return {"files": out}


@router.get("/api/jobs/{job_id}/files/{file_path:path}")
def download_job_file(job_id: int, file_path: str, authorization: Optional[str] = Header(None)):
    """Download a single file from the job workspace."""
    import os
    import mimetypes
    from pathlib import Path
    from fastapi.responses import FileResponse
    user, _ = get_current_user_and_session(authorization)
    row = _get_own_job(job_id, user)
    rid = row.get("runner_job_id")
    if not rid:
        raise HTTPException(status_code=404, detail="Job not running.")
    resp = runner_client._runner_http("GET", f"/internal/jobs/{rid}", worker=_worker_of(row))
    if resp.status_code != 200:
        raise HTTPException(status_code=404, detail="Job workspace unavailable.")
    info = resp.json()
    jdir = info.get("dir") or ""
    # Prevent path traversal
    target = (Path(jdir) / file_path).resolve()
    jroot = Path(jdir).resolve()
    if not str(target).startswith(str(jroot) + os.sep) and target != jroot:
        raise HTTPException(status_code=400, detail="Invalid file path.")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    ctype, _ = mimetypes.guess_type(str(target))
    return FileResponse(str(target), filename=target.name,
                        media_type=ctype or "application/octet-stream")


@router.get("/api/jobs/{job_id}/snapshot")
def get_job_snapshot_status(job_id: int, authorization: Optional[str] = Header(None)):
    """When was this job's data last backed up, and how big is it?"""
    user, _ = get_current_user_and_session(authorization)
    _get_own_job(job_id, user)
    from services import snapshots
    meta = snapshots.snapshot_meta(job_id)
    return {
        "enabled": snapshots.SNAPSHOTS_ENABLED,
        "snapshot": meta,
        "interval_s": snapshots.SNAPSHOT_INTERVAL_S,
    }


@router.post("/api/jobs/{job_id}/snapshot")
def create_job_snapshot(job_id: int, authorization: Optional[str] = Header(None)):
    """Back up this job's data files right now (the "Backup data" button)."""
    user, _ = get_current_user_and_session(authorization)
    row = _get_own_job(job_id, user)
    rid = row.get("runner_job_id")
    if not rid:
        raise HTTPException(status_code=409, detail="Job is not deployed yet.")
    from services import snapshots
    res = snapshots.save_snapshot(job_id, rid, worker=_worker_of(row))
    if not res.get("saved"):
        reason = res.get("reason") or "nothing to back up"
        # "no data files" is a normal state for a bot that hasn't written
        # anything yet — say so plainly instead of returning a scary error.
        raise HTTPException(status_code=409, detail=f"No backup taken — {reason}.")
    return res


@router.post("/api/jobs/{job_id}/snapshot/restore")
def restore_job_snapshot(job_id: int, authorization: Optional[str] = Header(None)):
    """Force-restore the stored data over the live workspace, then restart.

    Destructive on purpose (overwrite=True) — this is the "my bot wiped its own
    database, put yesterday's copy back" button, so the caller means it.
    """
    user, _ = get_current_user_and_session(authorization)
    row = _get_own_job(job_id, user)
    rid = row.get("runner_job_id")
    if not rid:
        raise HTTPException(status_code=409, detail="Job is not deployed yet.")
    from services import snapshots
    if not snapshots.load_snapshot(job_id):
        raise HTTPException(status_code=404, detail="No backup stored for this job.")
    res = snapshots.restore_snapshot(job_id, rid, overwrite=True, worker=_worker_of(row))
    if not res.get("restored"):
        raise HTTPException(status_code=502,
                            detail=f"Restore failed — {res.get('reason') or 'unknown error'}.")
    try:
        runner_client._runner_http("POST", f"/internal/jobs/{rid}/restart", worker=_worker_of(row))
    except Exception as exc:
        logger.warning("restart after restore failed (job %s): %s", job_id, exc)
    return res


@router.get("/api/jobs/{job_id}/download")
def download_job_workspace(job_id: int, authorization: Optional[str] = Header(None)):
    """Download the whole workspace (database + data files) as one .tar.gz.

    Serves the live workspace when the runner can reach it, and falls back to
    the last stored snapshot when it cannot — so a download still works after a
    deploy has wiped the container, which is exactly when a user panics and
    wants their data.
    """
    import base64 as _b64
    from fastapi.responses import Response as _Response
    user, _ = get_current_user_and_session(authorization)
    row = _get_own_job(job_id, user)
    rid = row.get("runner_job_id")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", (row.get("name") or "job")).strip("-") or "job"

    b64 = None
    if rid:
        try:
            resp = runner_client._runner_http("GET", f"/internal/jobs/{rid}/snapshot", worker=_worker_of(row))
            if resp.status_code == 200:
                data = resp.json()
                if not data.get("empty"):
                    b64 = data.get("tarball_b64")
        except Exception as exc:
            logger.info("live workspace unavailable for download (job %s): %s", job_id, exc)
    if not b64:
        from services import snapshots
        snap = snapshots.load_snapshot(job_id)
        if snap:
            b64 = snap.get("tarball_b64")
    if not b64:
        raise HTTPException(status_code=404,
                            detail="No data files yet — this job hasn't written a database.")
    try:
        raw = _b64.b64decode(b64)
    except Exception:
        raise HTTPException(status_code=500, detail="Stored backup is corrupt.")
    return _Response(
        content=raw,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{safe}-data.tar.gz"'},
    )


@router.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: int, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    row = _get_own_job(job_id, user)
    rid = row.get("runner_job_id")
    runner_error = None
    if rid:
        # Snapshot while the files are still there. A stopped job is the most
        # likely one to be sitting idle when the next deploy wipes the disk.
        try:
            from services import snapshots
            snapshots.save_snapshot(job_id, rid, worker=_worker_of(row))
        except Exception as exc:
            logger.warning("pre-stop snapshot failed for job %s: %s", job_id, exc)
        try:
            resp = runner_client._runner_http("POST", f"/internal/jobs/{rid}/stop", worker=_worker_of(row))
            if resp.status_code not in (200, 404):
                runner_error = f"Runner refused to stop the job (HTTP {resp.status_code})."
        except Exception as exc:
            # Runner offline / waking / suspended — do NOT fail the stop.
            # DB is the source of truth for desired_state; the reconciler/
            # reaper will kill the orphanned polling process when the worker
            # answers again. User sees \"stopped\" immediately instead of an
            # error that leaves the badge green forever.
            logger.warning("stop runner unreachable for job %s: %s — marking stopped anyway", job_id, exc)
            runner_error = str(exc)[:300]
    conn = get_db_connection()
    try:
        conn.execute("UPDATE jobs SET desired_state='stopped',updated_at=? WHERE id=?",
                     (now_utc_str(), job_id))
        conn.commit()
    finally:
        conn.close()
    out = {"status": "stopped"}
    if runner_error:
        out["runner_warning"] = runner_error
        out["note"] = "Marked stopped in database; runner will be stopped when it comes back online."
    return out


@router.post("/api/jobs/{job_id}/restart")
def restart_job(job_id: int, request: Request, authorization: Optional[str] = Header(None)):
    """Restart a job — IN-PLACE when the runner still knows about it (preserves
    workspace / database.db / session files). Cold-start (fresh worker slot)
    only as a fallback after a full runner restart."""
    user, _ = get_current_user_and_session(authorization)
    rate_limit_user(user["id"], "exec")
    row = _get_own_job(job_id, user)

    rid = row.get("runner_job_id")
    info = None

    if rid:
        # Fast path: in-place restart on the SAME job id/dir/port/slug.
        # This is what keeps referral-bot databases alive across restarts.
        resp = runner_client._runner_http("POST", f"/internal/jobs/{rid}/restart", worker=_worker_of(row))
        if resp.status_code == 200:
            info = resp.json()

    if info is None:
        # Cold-start fallback: the runner was rebuilt (a deploy) and lost both
        # its in-memory record AND — on the free tier — the workspace itself.
        # This is THE moment a referral bot used to lose its database.db, so we
        # create the job, restore the last snapshot into the fresh empty dir,
        # and only then let it run.
        resp = runner_client._runner_http("POST", "/internal/jobs", {
            "language": row["language"], "code": row["code"],
            "name": f"u{user['id']}-{row['name']}",
            # Replay saved env, otherwise a cold restart silently loses the
            # job's API keys / bot tokens and it crash-loops.
            "env": _row_env(row),
            "mem_limit_mb": _mem_limit_for(user["id"]),
        })
        if resp.status_code == 201:
            info = resp.json()
        else:
            try:
                detail = resp.json().get("detail", "Runner rejected the job.")
            except Exception:
                detail = "Runner rejected the job."
            raise HTTPException(status_code=502, detail=detail)
        conn = get_db_connection()
        try:
            conn.execute(
                "UPDATE jobs SET runner_job_id = ?, worker_url = ?, updated_at = ? WHERE id = ?",
                (info["id"], getattr(resp, "placed_on", None), now_utc_str(), job_id),
            )
            conn.commit()
        finally:
            conn.close()
        _restore_then_restart(job_id, info, getattr(resp, "placed_on", None))

    meta = {
        "detected": bool(row.get("telegram_bot_detected")),
        "username": row.get("telegram_bot_username"), "bot_id": row.get("telegram_bot_id"),
        "check_status": row.get("telegram_check_status"),
        "verified_at": row.get("telegram_verified_at"),
    }
    conn = get_db_connection()
    try:
        conn.execute("UPDATE jobs SET desired_state='running',updated_at=? WHERE id=?",
                     (now_utc_str(), job_id))
        _record_deploy_event(conn, user["id"], job_id, "restart", row["name"], meta, now_utc_str())
        conn.commit()
    finally:
        conn.close()
    info["job_db_id"] = job_id
    info.update(telegram_detector.public_fields(meta))
    info.update(runner_client._job_web_fields(info, _worker_of(row)))
    return info


class JobAccessToggle(BaseModel):
    public: bool = True


@router.patch("/api/jobs/{job_id}")
def update_job(job_id: int, payload: JobUpdateRequest, request: Request, authorization: Optional[str] = Header(None)):
    """Edit + redeploy a job IN PLACE — preserves the runner job id, its
    /live/{slug}/ URL, reserved port, and most importantly the bot's
    persistent workspace (SQLite DBs, session files, referral counts, …).
    Use this for bug fixes / feature adds: users' data NEVER gets wiped."""
    user, _ = get_current_user_and_session(authorization)
    row = _get_own_job(job_id, user)
    if payload.save_only:
        # Persist the editor to the DB without touching the running process.
        # Run / Save & Run still goes through the full in-place redeploy path.
        new_name = (payload.name or row["name"] or "").strip()[:60] or row["name"]
        new_lang = (payload.language or row["language"] or "python").strip()
        new_code = payload.code if payload.code is not None else row["code"]
        existing_env = _row_env(row)
        new_env = (_restore_masked_env(_clean_env_map(payload.env), existing_env)
                   if payload.env is not None else existing_env)
        token = str(new_env.get("BOT_TOKEN") or "").strip()
        if token:
            new_code, new_env = telegram_detector.secure_bot_source(
                new_code, new_env, token, new_lang)
        now = now_utc_str()
        if new_name != (row["name"] or ""):
            conn0 = get_db_connection()
            try:
                dup = conn0.execute(
                    "SELECT id FROM jobs WHERE user_id = ? AND id != ? AND LOWER(name) = LOWER(?)",
                    (user["id"], job_id, new_name),
                ).fetchone()
                if dup:
                    raise HTTPException(
                        status_code=409,
                        detail=f"You already have a job named \u201c{new_name}\u201d — choose a different name.",
                    )
            finally:
                conn0.close()
        conn = get_db_connection()
        try:
            conn.execute(
                "UPDATE jobs SET name=?,language=?,code=?,env=?,updated_at=? WHERE id=?",
                (new_name, new_lang, new_code, secrets_store.pack_env(new_env), now, job_id),
            )
            conn.commit()
        finally:
            conn.close()
        out = dict(row)
        out.update({"name": new_name, "language": new_lang, "code": new_code,
                    "saved": True, "save_only": True, "job_db_id": job_id})
        out["env"] = _public_env(new_env)
        _attach_telegram_public(out)
        return out

    rate_limit_user(user["id"], "exec")
    rid = row.get("runner_job_id")
    if not rid:
        raise HTTPException(status_code=409, detail="Job has no runner id — press Restart once, then retry edit.")

    # Build a candidate revision first. The jobs row remains the last healthy
    # source of truth until the runner accepts this candidate.
    new_name = (payload.name or row["name"]).strip()[:60]
    new_lang = (payload.language or row["language"]).strip()
    new_code = payload.code if payload.code is not None else row["code"]
    new_repo = (payload.repo_url or "").strip()
    new_entry = (payload.entry or "").strip()
    # env omitted from the request => keep what is already saved.
    existing_env = _row_env(row)
    new_env = (_restore_masked_env(_clean_env_map(payload.env), existing_env)
               if payload.env is not None else existing_env)
    verified_token = str(new_env.get("BOT_TOKEN") or "").strip()
    if payload.telegram_verification_id:
        bot_meta = telegram_detector.validate_verification(
            user["id"], payload.telegram_verification_id, verified_token)
    else:
        bot_meta = ({"detected": bool(row.get("telegram_bot_detected")),
                     "username": row.get("telegram_bot_username"),
                     "bot_id": row.get("telegram_bot_id"),
                     "check_status": row.get("telegram_check_status"),
                     "verified_at": row.get("telegram_verified_at")}
                    if verified_token and row.get("telegram_bot_detected") else None)
    if not bot_meta:
        raise HTTPException(status_code=400,
                            detail="Verify your Telegram bot token before saving this bot.")
    token_fingerprint = telegram_detector.token_fingerprint(verified_token)
    _reject_duplicate_bot_token(token_fingerprint, exclude_job_id=job_id)
    code_analysis = telegram_detector.analyze_code(new_code, new_lang)
    new_code, new_env = telegram_detector.secure_bot_source(
        new_code, new_env, verified_token, new_lang)
    now = now_utc_str()
    if new_name != (row["name"] or ""):
        conn0 = get_db_connection()
        try:
            dup = conn0.execute(
                "SELECT id FROM jobs WHERE user_id = ? AND id != ? AND LOWER(name) = LOWER(?)",
                (user["id"], job_id, new_name),
            ).fetchone()
            if dup:
                raise HTTPException(status_code=409, detail=f"You already have a job named \u201c{new_name}\u201d \u2014 choose a different name.")
        finally:
            conn0.close()
    # Create an immutable candidate revision, but DO NOT promote it or replace
    # the job's source until the runner accepts the deployment.
    conn = get_db_connection()
    try:
        revision_id, revision_version = _create_revision(
            conn, user["id"], job_id, new_lang, new_code, action="update")
        conn.commit()
    finally:
        conn.close()

    # Back the workspace up BEFORE touching the running job. An edit is the
    # most common moment to lose data (the runner may 404 below and force a
    # cold start into an empty dir), so this is the cheapest place to make the
    # data recoverable. Best-effort — a failed backup must not block the edit.
    try:
        from services import snapshots
        snapshots.save_snapshot(job_id, rid, worker=_worker_of(row))
    except Exception as exc:
        logger.warning("pre-update snapshot failed for job %s: %s", job_id, exc)

    # Forward to runner for in-place update (same dir, same slug, same port).
    patch_body = {"name": new_name, "language": new_lang, "code": new_code, "env": new_env,
                  # Re-sync 👑 on every redeploy, so a grant made after this
                  # bot's first deploy reaches the running process instead of
                  # waiting for the next cold start.
                  "mem_limit_mb": _mem_limit_for(user["id"])}
    if new_repo:
        patch_body["repo_url"] = new_repo
        if new_entry: patch_body["entry"] = new_entry
    new_runner_id = rid
    new_worker = _worker_of(row)
    try:
        resp = runner_client._runner_http("PATCH", f"/internal/jobs/{rid}", patch_body, worker=_worker_of(row))
    except Exception as exc:
        _finish_revision(revision_id, "failed", "Runner unreachable")
        raise
    if resp.status_code == 200:
        info = resp.json()
    elif resp.status_code == 404:
        # Runner restarted — fall back to cold-start Restart path.
        create_body = {"language": new_lang, "code": new_code,
                       "name": f"u{user['id']}-{new_name}", "env": new_env,
                       "mem_limit_mb": _mem_limit_for(user["id"])}
        if new_repo:
            create_body["repo_url"] = new_repo
            if new_entry: create_body["entry"] = new_entry
        resp2 = runner_client._runner_http("POST", "/internal/jobs", create_body)
        if resp2.status_code != 201:
            try:
                detail = resp2.json().get("detail", "Runner rejected the job.")
            except Exception:
                detail = "Runner rejected the job."
            _finish_revision(revision_id, "failed", detail)
            raise HTTPException(status_code=502, detail=detail)
        info = resp2.json()
        new_runner_id = info["id"]
        new_worker = getattr(resp2, "placed_on", None)
        # Same cold-start recovery as restart_job(): the new workspace is
        # empty, so replay the snapshot we just took (or an older one).
        info = _restore_then_restart(job_id, info, getattr(resp2, "placed_on", None))
    else:
        try:
            detail = resp.json().get("detail", "Runner rejected the update.")
        except Exception:
            detail = "Runner rejected the update."
        _finish_revision(revision_id, "failed", detail)
        raise HTTPException(status_code=502, detail=detail)

    # Runner accepted the candidate. Promote source/config atomically now.
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE jobs SET name=?,language=?,code=?,env=?,runner_job_id=?,worker_url=?,desired_state='running',"
            "telegram_bot_detected=?,telegram_bot_username=?,telegram_bot_id=?,"
            "telegram_check_status=?,telegram_verified_at=?,telegram_token_fingerprint=?,"
            "telegram_framework=?,telegram_update_mode=?,telegram_token_source=?,updated_at=? WHERE id=?",
            (new_name, new_lang, new_code, secrets_store.pack_env(new_env),
             new_runner_id, new_worker, *_telegram_columns(bot_meta), token_fingerprint,
             code_analysis["framework"], code_analysis["update_mode"],
             code_analysis["token_source"], now, job_id),
        )
        if new_repo:
            # Record which revision this redeploy built, and which file in it
            # runs. Without this the chat's ⬆️ "deploy latest" button keeps
            # offering an update the website already applied — one job, two
            # different answers about what is running.
            conn.execute(
                "UPDATE jobs SET repo_url=?, repo_entry=?, repo_commit=? WHERE id=?",
                (new_repo, info.get("repo_entry") or new_entry or None,
                 info.get("repo_commit") or None, job_id),
            )
        conn.execute("UPDATE bot_revisions SET status='healthy',error=NULL,promoted_at=? WHERE id=?",
                     (now, revision_id))
        _record_deploy_event(conn, user["id"], job_id, "update", new_name, bot_meta, now)
        conn.commit()
    finally:
        conn.close()

    if payload.telegram_verification_id:
        telegram_detector.consume_verification(payload.telegram_verification_id)
    info["job_db_id"] = job_id
    info["revision"] = revision_version
    info.update(runner_client._job_web_fields(info, new_worker))
    info.update(telegram_detector.public_fields(bot_meta))
    info.update({"telegram_framework": code_analysis["framework"],
                 "telegram_update_mode": code_analysis["update_mode"],
                 "telegram_token_source": code_analysis["token_source"]})
    return info


@router.delete("/api/jobs/{job_id}")
def delete_job(job_id: int, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    row = _get_own_job(job_id, user)
    rid = row.get("runner_job_id")
    if rid:
        # Hard delete on the runner too — wipes the persistent workspace.
        # If the worker is offline the DB row is still removed; the orphan
        # reaper (job_recovery._reap_orphans, every 5min) will DELETE the
        # lingering runner job when its worker answers again, so \"deleted but
        # still polling Telegram\" cannot persist.
        try:
            runner_client._runner_http("DELETE", f"/internal/jobs/{rid}", worker=_worker_of(row))
        except Exception as exc:
            logger.warning("delete: runner unreachable for job %s (%s) — DB row removed; reaper will clean runner", job_id, exc)
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        conn.commit()
    finally:
        conn.close()
    return {"message": "Job deleted."}


@router.post("/api/jobs/{job_id}/access")
def toggle_job_access(job_id: int, payload: JobAccessToggle, authorization: Optional[str] = Header(None)):
    """Public ⇄ Private toggle for a job's live web URL."""
    user, _ = get_current_user_and_session(authorization)
    row = _get_own_job(job_id, user)
    rid = row.get("runner_job_id")
    if not rid:
        raise HTTPException(status_code=409, detail="Job is not up on the runner — press Restart first.")
    resp = runner_client._runner_http("POST", f"/internal/jobs/{rid}/access", {"public": payload.public}, worker=_worker_of(row))
    if resp.status_code == 404:
        raise HTTPException(status_code=409, detail="Runner restarted — press Restart to relaunch, then retry.")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Runner refused the access change.")
    info = resp.json()
    info.update(runner_client._job_web_fields(info, _worker_of(row)))
    info["job_db_id"] = job_id
    return info





# ================================
# USER PREFERENCES
# ================================

# ================================
# GITHUB IMPORT — fetch a single file or repo tree into editor
# ================================

_GH_RAW_HOSTS = ("raw.githubusercontent.com",)
_GH_WEB_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)"
    r"(?:/(?:blob|tree)/(?P<ref>[^/]+)/(?P<path>.+))?/?$"
)

_LANG_GUESS = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript",
    ".ts": "javascript", ".sh": "bash", ".bash": "bash",
    ".zsh": "bash", ".rb": "ruby", ".php": "php",
    ".html": "htmlmixed", ".htm": "htmlmixed",
    ".css": "css", ".md": "markdown",
}


def _http_get_text(url: str, timeout: float = 12.0) -> tuple[int, str]:
    """Small blocking GET using stdlib — only called for raw.github content."""
    import urllib.request, urllib.error, ssl
    req = urllib.request.Request(url, headers={
        "User-Agent": "codenest-runspace/1.0",
        "Accept": "text/plain,application/vnd.github.raw+json,*/*",
    })
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try: body = e.read().decode("utf-8", errors="replace")
        except Exception: body = ""
        return e.code, body
    except Exception as e:
        return 0, str(e)


@router.post("/api/import/github")
def import_github(payload: GithubImportRequest, authorization: Optional[str] = Header(None)):
    """Fetch raw code from a GitHub URL (file or repo default-branch main file).
    Returns { name, language, code, source_url } for prefilling the editor.
    """
    user, _ = get_current_user_and_session(authorization)
    url = (payload.url or "").strip()
    if not url:
        raise HTTPException(422, "Paste a GitHub URL first.")
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise HTTPException(422, "URL must start with https://")
    host = (u.hostname or "").lower()

    code = ""
    name = ""
    language = "python"
    source_url = url

    # Case 1: already a raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>
    if host in _GH_RAW_HOSTS:
        parts = [p for p in u.path.split("/") if p]
        if len(parts) >= 4:
            owner, repo, ref = parts[0], parts[1], parts[2]
            path = "/".join(parts[3:])
            status, body = _http_get_text(url)
            if status != 200:
                raise HTTPException(400, f"GitHub fetch failed ({status}). Is the repo/file public?")
            code = body
            fname = parts[-1]
            name = repo
            ext = "." + fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            language = _LANG_GUESS.get(ext, "python")
        else:
            raise HTTPException(422, "Raw URL path looks off — give a full file link.")

    # Case 2: github.com web URL
    elif host == "github.com":
        m = _GH_WEB_RE.match(url.split("#")[0].split("?")[0])
        if not m:
            raise HTTPException(422, "That doesn't look like a GitHub file or repo URL.")
        owner = m.group("owner")
        repo  = m.group("repo")
        ref   = m.group("ref")
        path  = m.group("path")
        name = repo

        if not path:
            # Repo root — try to fetch README.md / main.py / app.py / index.js
            if not ref:
                # Detect default branch via GitHub API (best-effort)
                st, body = _http_get_text(f"https://api.github.com/repos/{owner}/{repo}", timeout=8)
                if st == 200:
                    try:
                        meta = json.loads(body)
                        ref = meta.get("default_branch") or "main"
                    except Exception:
                        ref = "main"
                else:
                    ref = "main"
            candidates = ["main.py", "app.py", "bot.py", "index.js", "server.js",
                          "index.php", "main.sh", "README.md"]
            fetched = None
            for cand in candidates:
                raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{cand}"
                st, body = _http_get_text(raw, timeout=8)
                if st == 200 and body.strip():
                    fetched = (cand, body, raw); break
            if not fetched:
                raise HTTPException(400, "No main.py/app.py/bot.py/index.js found in repo root. Link directly to a file instead.")
            fname, code, source_url = fetched
            ext = "." + fname.rsplit(".",1)[-1].lower()
            language = _LANG_GUESS.get(ext, "python")
        else:
            # Direct file URL like /owner/repo/blob/HEAD/main.py
            if not ref: ref = "main"
            raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
            st, body = _http_get_text(raw)
            if st != 200:
                raise HTTPException(400, f"Couldn't fetch file ({st}) — is the file public?")
            code = body
            source_url = raw
            fname = path.rsplit("/",1)[-1]
            ext = "." + fname.rsplit(".",1)[-1].lower() if "." in fname else ""
            language = _LANG_GUESS.get(ext, "python")
    else:
        raise HTTPException(422, "Only github.com URLs are supported for now.")

    # Cap at ~256KB to prevent dumping huge repos into the editor
    if len(code) > 256 * 1024:
        raise HTTPException(400, "File is too large (>256KB). Paste smaller files only.")

    # Slugify name slightly
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "github-import"
    return {"name": name[:60], "language": language, "code": code, "source_url": source_url}
