"""Admin console (owner-only, 404-stealth for everyone else).

Lean single-owner edition: the remote runner pool, IP/device blocks, cluster
and signup-burst views and the public abuse inbox were removed. The embedded
runner is the only runner. Moderation actions are session-authenticated and
audited."""
from typing import Optional, List
from urllib.parse import quote

from fastapi import APIRouter, Header, HTTPException, Request

from routes.deps import *  # shared kernel (config, helpers, models)


from fastapi.responses import HTMLResponse, JSONResponse, Response

from services import runner_client
from services import telegram_admin_ext
from services import secrets_store, env_rescue
from services.runner_client import MAX_JOBS_PER_USER

# "Active now" window. Long enough that someone reading logs still counts,
# short enough that it means present rather than "visited today".
ACTIVE_WINDOW_MIN = int(os.getenv("ADMIN_ACTIVE_WINDOW_MIN", "15"))

# How stale a worker-health reading the console will accept.
ADMIN_HEALTH_MAX_AGE_S = float(os.getenv("ADMIN_HEALTH_MAX_AGE_S", "8"))

router = APIRouter()


def require_admin(authorization):
    """404 for everyone else — the console's existence stays private.

    404 for the UNAUTHENTICATED case too. Letting a missing token answer 401
    while a valid non-admin token answers 404 is itself a signal: it tells a
    stranger the route is real and merely gated. Every non-admin caller now
    gets the identical response an unknown URL would give.
    """
    try:
        user, session = get_current_user_and_session(authorization)
    except HTTPException:
        raise HTTPException(status_code=404, detail="Not found.")
    if not ("is_admin" in user.keys() and user["is_admin"]):
        raise HTTPException(status_code=404, detail="Not found.")
    return user, session


def _admin_audit(conn, admin_id: int, action: str, target: str = "", details: str = ""):
    conn.execute(
        "INSERT INTO admin_audit_log (admin_id, action, target, details, created_at) VALUES (?,?,?,?,?)",
        (admin_id, action, target, details, now_utc_str()),
    )


def _stop_user_jobs_best_effort(user_id: int):
    """On suspend: tell the runner to stop every job the account deployed."""
    try:
        conn = get_db_connection()
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT runner_job_id,worker_url FROM jobs WHERE user_id=? AND runner_job_id IS NOT NULL",
                (user_id,),
            ).fetchall()]
            conn.execute("UPDATE jobs SET desired_state='stopped' WHERE user_id=?",(user_id,))
            conn.commit()
        finally:
            conn.close()
        for item in rows:
            try:
                runner_client._runner_http("POST", f"/internal/jobs/{item['runner_job_id']}/stop", worker=item.get("worker_url"))
            except Exception:
                pass
    except Exception:
        pass


class AdminSuspend(BaseModel):
    user_id: int
    suspended: bool
    code: Optional[str] = None


@router.get("/admin/panel-html", include_in_schema=False)
def admin_panel_html(authorization: Optional[str] = Header(None)):
    """The console's MARKUP, behind the same 404 gate as its data."""
    require_admin(authorization)
    from fastapi.responses import HTMLResponse
    from pathlib import Path
    frag = Path(__file__).resolve().parent.parent / "templates" / "admin_panel.html"
    if not frag.exists():
        raise HTTPException(status_code=404, detail="Not found.")
    return HTMLResponse(frag.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


@router.post("/admin/recover")
def admin_recover_now(authorization: Optional[str] = Header(None)):
    """Run one serialized recovery pass; code deploys remain a separate action."""
    admin, _ = require_admin(authorization)
    from services import job_recovery
    try:
        unresolved = job_recovery.recover_once()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Recovery failed: {type(exc).__name__}: {exc}")
    conn = get_db_connection()
    try:
        _admin_audit(conn, admin["id"], "recover_now", "fleet",
                     f"unresolved={unresolved}")
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "unresolved": unresolved,
            "message": f"Recovery done. Unresolved: {unresolved}."}


@router.get("/admin/telegram-diagnostic")
def admin_telegram_diagnostic(authorization: Optional[str] = Header(None)):
    """Is the server holding the same bot the Mini App is opened from?"""
    require_admin(authorization)
    from services import miniapp_auth
    shape = miniapp_auth.token_shape()
    who = miniapp_auth.whoami()
    out = {"configured_bot_id": shape.get("bot_id"),
           "token_looks_valid": shape.get("looks_valid"),
           "telegram_says": who}
    if who.get("ok"):
        out["bot_username"] = who.get("username")
        out["open_this_bot"] = f"https://t.me/{who.get('username')}"
        out["next_step"] = (
            f"Open the Mini App from @{who.get('username')} — that is the bot "
            f"this server can verify. Opening it from any other bot gives "
            f"bad_hash.")
    else:
        out["next_step"] = (
            "Telegram did not accept BOT_TOKEN. Copy it again "
            "from @BotFather for the bot whose Mini App you are opening.")
    out["TELEGRAM_BOT_USERNAME_env"] = os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@") or None
    if out.get("bot_username") and out["TELEGRAM_BOT_USERNAME_env"] \
            and out["bot_username"].lower() != out["TELEGRAM_BOT_USERNAME_env"].lower():
        out["warning"] = (
            f"TELEGRAM_BOT_USERNAME is @{out['TELEGRAM_BOT_USERNAME_env']} but "
            f"the token belongs to @{out['bot_username']}. These must be the "
            f"same bot.")
    return out


@router.get("/admin/bot-usage")
def admin_bot_usage(days: int = 30, authorization: Optional[str] = Header(None)):
    """Aggregated Telegram activity, including chats without an account."""
    require_admin(authorization)
    from services import bot_analytics
    return bot_analytics.usage(days)


@router.get("/admin/bot-usage.csv")
def admin_bot_usage_csv(days: int = 30,
                        authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    from services import bot_analytics
    body = bot_analytics.usage_csv(days)
    return Response(body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="telegram-bot-usage-{max(1, min(days, 365))}d.csv"',
                             "Cache-Control": "no-store"})


@router.get("/admin/overview")
def admin_overview_route(authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    conn = get_db_connection()
    try:
        users = dict(conn.execute("SELECT COUNT(*) AS c FROM users").fetchone())["c"]
        suspended = dict(conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_suspended=1").fetchone())["c"]
        verified = dict(conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_verified=1").fetchone())["c"]
        jobs_total = dict(conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone())["c"]
        deployed = dict(conn.execute("SELECT COUNT(*) AS c FROM jobs WHERE runner_job_id IS NOT NULL").fetchone())["c"]
        threshold = (now_utc() - timedelta(days=13)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS c "
            "FROM users WHERE created_at >= ? GROUP BY day ORDER BY day",
            (threshold,),
        ).fetchall()
        series = [{"day": dict(r)["day"], "count": dict(r)["c"]} for r in rows]

        def _since(days):
            cutoff = (now_utc() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            r = conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE created_at >= ?", (cutoff,)
            ).fetchone()
            return dict(r)["c"]

        active_cut = (now_utc() - timedelta(minutes=ACTIVE_WINDOW_MIN)).strftime("%Y-%m-%d %H:%M:%S")
        active_users = dict(conn.execute(
            "SELECT COUNT(DISTINCT user_id) AS c FROM sessions WHERE last_seen >= ?",
            (active_cut,),
        ).fetchone())["c"]

        tg_linked = dict(conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE telegram_id IS NOT NULL"
        ).fetchone())["c"]

        out = {
            "users": users, "suspended": suspended, "verified": verified,
            "jobs_total": jobs_total, "jobs_deployed": deployed,
            "jobs_max_per_user": MAX_JOBS_PER_USER,
            "capacity_max": users * MAX_JOBS_PER_USER,
            "signups_daily": series,
            "signups_24h": _since(1),
            "signups_7d": _since(7),
            "signups_30d": _since(30),
            "active_users": active_users,
            "active_window_min": ACTIVE_WINDOW_MIN,
            "telegram_linked": tg_linked,
            "bot_secrets_storage": "plain-text",
            "bot_secrets_legacy_rows": secrets_store.legacy_rows(),
            "bot_secrets_unreadable_rows": env_rescue.unreadable_count(),
            "bot_secrets_rescued_at_boot": env_rescue.LAST_SWEEP.get("rescued", 0),
            "runner_isolation": "embedded" if runner_client.embedded_mode() else "remote",
        }
    finally:
        conn.close()
    # Fleet capacity, reported as MEMORY rather than slots.
    workers = []
    used_mb = safe_mb = total_mb = 0.0
    running_total = 0
    try:
        health = runner_client.worker_health(max_age_s=ADMIN_HEALTH_MAX_AGE_S) or {}
        for url, h in health.items():
            workers.append({
                "url": url,
                "online": bool(h.get("online")),
                "jobs": h.get("jobs", 0),
                "mem_mb": h.get("mem_mb", 0.0),
                "safe_mb": h.get("safe_mb", 0),
                "total_mb": h.get("total_mb", 0),
                "full": bool(h.get("full")),
            })
            if h.get("online"):
                used_mb += float(h.get("mem_mb") or 0)
                safe_mb += float(h.get("safe_mb") or 0)
                total_mb += float(h.get("total_mb") or 0)
                running_total += int(h.get("jobs") or 0)
    except Exception:
        pass
    if not workers:
        # Embedded single-service mode: ask the in-process runner directly.
        try:
            r = runner_client._runner_http("GET", "/health")
            h = r.json() if r is not None else {}
            used_mb = float(h.get("mem_mb") or 0)
            safe_mb = float(h.get("safe_mb") or 0)
            total_mb = float(h.get("total_mb") or 0)
            running_total = int(h.get("jobs") or 0)
            workers.append({"url": "embedded", "online": True,
                            "jobs": running_total, "mem_mb": used_mb,
                            "safe_mb": safe_mb, "total_mb": total_mb,
                            "full": bool(h.get("full"))})
        except Exception:
            pass
    try:
        jl = list(runner_client.fleet_jobs().values())
        by = {}
        for j in jl:
            by[j.get("status") or "unknown"] = by.get(j.get("status") or "unknown", 0) + 1
        out["jobs_by_status"] = by
        out["jobs_running"] = by.get("running", 0)
    except Exception:
        pass

    if safe_mb:
        out["mem_used_mb"] = round(used_mb, 1)
        out["mem_safe_mb"] = round(safe_mb)
        out["mem_total_mb"] = round(total_mb)
        out["mem_pct"] = round(min(used_mb / safe_mb, 1.0) * 100)
        out["runner_running"] = running_total
        out["workers"] = workers
        out["workers_online"] = sum(1 for w in workers if w["online"])
    return out


@router.get("/admin/users")
def admin_users_route(authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT u.id, u.username, u.email, u.is_verified, u.is_suspended, u.is_admin,
                   u.created_at, u.telegram_id, u.telegram_name,
                   (SELECT COUNT(*) FROM jobs j WHERE j.user_id = u.id) AS job_count
            FROM users u ORDER BY u.id DESC LIMIT 200
            """
        ).fetchall()
        return {"users": [dict(r) for r in rows]}
    finally:
        conn.close()


@router.get("/admin/users/{user_id}")
def admin_user_detail_route(user_id: int, authorization: Optional[str] = Header(None)):
    """One account in full: profile, jobs, sessions, security events."""
    require_admin(authorization)
    conn = get_db_connection()
    try:
        u = conn.execute(
            "SELECT id, username, email, is_verified, is_suspended, is_admin, "
            "       created_at, updated_at, telegram_id, telegram_name, "
            "       fingerprint, last_ip "
            "FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not u:
            raise HTTPException(status_code=404, detail="Not found.")
        user = dict(u)
        user["auth_method"] = "telegram" if user.get("telegram_id") else "email"
        user["auth_method_inferred"] = True

        jobs = [dict(r) for r in conn.execute(
            "SELECT id, name, language, runner_job_id, worker_url, created_at, updated_at "
            "FROM jobs WHERE user_id = ? ORDER BY id DESC", (user_id,)
        ).fetchall()]

        sessions = [dict(r) for r in conn.execute(
            "SELECT id, ip_address, device_info, fingerprint, created_at, last_seen, expires_at "
            "FROM sessions WHERE user_id = ? ORDER BY id DESC LIMIT 50", (user_id,)
        ).fetchall()]

        events = []
        try:
            events = [dict(r) for r in conn.execute(
                "SELECT action, details, ip_address, created_at FROM activity_log "
                "WHERE user_id = ? ORDER BY id DESC LIMIT 50", (user_id,)
            ).fetchall()]
        except Exception:
            pass
    finally:
        conn.close()

    live = runner_client.fleet_jobs()
    total_mem = 0.0
    for j in jobs:
        info = live.get(j.get("runner_job_id")) or {}
        j["live_status"] = info.get("status")
        j["mem_mb"] = info.get("mem_mb")
        j["peak_mem_mb"] = info.get("peak_mem_mb")
        j["uptime_s"] = info.get("uptime_s")
        j["libs"] = info.get("libs") or []
        total_mem += float(info.get("mem_mb") or 0)

    fps = {(sdict.get("fingerprint") or "").strip()
           for sdict in sessions if (sdict.get("fingerprint") or "").strip()}
    ips = {(sdict.get("ip_address") or "").strip()
           for sdict in sessions if (sdict.get("ip_address") or "").strip()}

    return {
        "user": user,
        "jobs": jobs,
        "jobs_running": sum(1 for j in jobs if j.get("live_status") == "running"),
        "mem_used_mb": round(total_mem, 1),
        "sessions": sessions,
        "devices": len(fps),
        "networks": len(ips),
        "events": events,
        "linked_accounts": [],
        "linked_note": "",
    }


@router.get("/admin/telegram-jobs")
def admin_telegram_jobs(authorization: Optional[str] = Header(None)):
    """Detected Telegram bots and immutable run/update/restart history.

    Raw tokens and source code are deliberately excluded.
    """
    require_admin(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT j.id,j.name,j.runner_job_id,j.telegram_bot_username,j.telegram_bot_id,"
            "j.telegram_check_status,j.telegram_verified_at,j.telegram_framework,"
            "j.telegram_update_mode,j.telegram_token_source,j.updated_at,u.id AS user_id,"
            "u.username AS owner FROM jobs j JOIN users u ON u.id=j.user_id "
            "WHERE j.telegram_bot_detected=1 ORDER BY j.updated_at DESC LIMIT 300"
        ).fetchall()
        events = conn.execute(
            "SELECT e.id,e.action,e.job_id,e.job_name,e.telegram_bot_detected,"
            "e.telegram_bot_username,e.telegram_bot_id,e.telegram_check_status,"
            "e.created_at,u.id AS user_id,u.username AS owner "
            "FROM job_deploy_events e JOIN users u ON u.id=e.user_id "
            "ORDER BY e.id DESC LIMIT 300"
        ).fetchall()
        bots = [dict(r) for r in rows]
        history = [dict(r) for r in events]
    finally:
        conn.close()
    live = runner_client.fleet_jobs()
    for bot in bots:
        info = live.get(bot.get("runner_job_id")) or {}
        bot["status"] = info.get("status") or "offline"
        bot["uptime_s"] = info.get("uptime_s")
        username = bot.get("telegram_bot_username")
        bot["telegram_bot_url"] = f"https://t.me/{username}" if username else None
    for event in history:
        username = event.get("telegram_bot_username")
        event["telegram_bot_url"] = f"https://t.me/{username}" if username else None
    return {"bots": bots, "events": history,
            "detected": len(bots),
            "running": sum(1 for b in bots if b.get("status") == "running")}


@router.get("/admin/jobs")
def admin_jobs_route(authorization: Optional[str] = Header(None)):
    """Job METADATA only (+ live status/uptime from the runner, best-effort) —
    never the code."""
    require_admin(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT j.id, j.name, j.language, j.created_at, j.runner_job_id,
                   j.worker_url, j.user_id, j.telegram_bot_detected,
                   j.telegram_bot_username, j.telegram_bot_id,
                   j.telegram_check_status, j.telegram_verified_at,
                   j.telegram_framework,j.telegram_update_mode,j.telegram_token_source,
                   u.username AS owner, u.telegram_id AS owner_telegram,
                   u.telegram_name AS owner_telegram_name,
                   u.is_suspended AS owner_suspended
            FROM jobs j JOIN users u ON u.id = j.user_id
            ORDER BY j.id DESC LIMIT 300
            """
        ).fetchall()
        jobs = [dict(r) for r in rows]
    finally:
        conn.close()
    live = runner_client.fleet_jobs()
    for row in jobs:
        info = live.get(row.get("runner_job_id")) or {}
        row["live_status"] = info.get("status")
        row["uptime_s"] = info.get("uptime_s")
        row["web_slug"] = info.get("web_slug")
        row["mem_mb"] = info.get("mem_mb")
        row["peak_mem_mb"] = info.get("peak_mem_mb")
        row["cpu_pct"] = info.get("cpu_pct")
        row["restarts"] = info.get("restarts")
        row["last_exit_reason"] = info.get("last_exit_reason")
        row["libs"] = info.get("libs") or []
        row["worker"] = row.get("worker_url") or "primary"
        row["source"] = "telegram" if row.get("owner_telegram") else "website"
        row["source_inferred"] = True
        row["owner_telegram_name"] = row.get("owner_telegram_name")
        bot_username = row.get("telegram_bot_username")
        row["telegram_bot_url"] = f"https://t.me/{bot_username}" if bot_username else None
        row.pop("owner_telegram", None)
    return {"jobs": jobs}


@router.get("/admin/jobs/{job_id}/settings")
def admin_reveal_job_settings(job_id: int, authorization: Optional[str] = Header(None)):
    """Explicit owner-admin reveal of a saved job's source and environment values."""
    admin, _ = require_admin(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT id,user_id,name,language,code,env,desired_state,created_at,updated_at,"
            "repo_url,repo_entry,repo_commit,auto_deploy FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Job not found.")
        item = dict(row)
    finally:
        conn.close()

    env_values, readable = secrets_store.read_env(item.get("env"))
    if not readable:
        try:
            env_values, outcome = env_rescue.rescue_job_env(item)
            readable = outcome in (env_rescue.FINE, env_rescue.LEGACY_OK, env_rescue.RESCUED)
        except Exception:
            logger.exception("admin settings reveal could not restore env for job %s", job_id)
            env_values, readable = {}, False

    conn = get_db_connection()
    try:
        _admin_audit(conn, admin["id"], "admin_job_settings_reveal", f"job:{job_id}",
                     "Admin explicitly requested saved source/settings; secret values omitted from the audit log.")
        conn.commit()
    finally:
        conn.close()
    item.pop("env", None)
    item.pop("user_id", None)
    return JSONResponse({"job": item, "env": env_values or {}, "env_readable": bool(readable)},
                        headers={"Cache-Control": "no-store, private", "Pragma": "no-cache"})


@router.post("/admin/jobs/{job_id}/restart")
def admin_restart_job_route(job_id: int, authorization: Optional[str] = Header(None)):
    """Admin restart through the job's assigned runner; missing ids cold-start safely."""
    admin, _ = require_admin(authorization)
    row = telegram_admin_ext.admin_find_job(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Job not found.")
    result = telegram_admin_ext.admin_restart_job(job_id)
    if not result.get("ok"):
        raise HTTPException(status_code=503, detail=result.get("error") or "The job could not be restarted.")
    worker = (row.get("worker_url") or "primary")
    conn = get_db_connection()
    try:
        _admin_audit(conn, admin["id"], "admin_job_restart", f"job:{job_id}",
                     f"name={row.get('name') or ''}; runner={worker}")
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "job_id": job_id, "name": row.get("name"),
            "message": f"Restart requested for {row.get('name') or 'job'}; its workspace is preserved."}


@router.post("/admin/jobs/{job_id}/stop")
def admin_stop_job_route(job_id: int, authorization: Optional[str] = Header(None)):
    """Admin stop through the job's assigned runner, preserving its workspace."""
    admin, _ = require_admin(authorization)
    row = telegram_admin_ext.admin_find_job(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Job not found.")
    result = telegram_admin_ext.admin_stop_job(job_id)
    if not result.get("ok"):
        raise HTTPException(status_code=503, detail=result.get("error") or "The job could not be stopped.")
    conn = get_db_connection()
    try:
        _admin_audit(conn, admin["id"], "admin_job_stop", f"job:{job_id}",
                     f"name={row.get('name') or ''}; runner={row.get('worker_url') or 'primary'}")
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "job_id": job_id, "name": row.get("name"),
            "message": f"{row.get('name') or 'Job'} stopped. Its files are preserved."}


@router.get("/admin/jobs/{job_id}")
def admin_job_detail_route(job_id: int, authorization: Optional[str] = Header(None)):
    """One app, in full — everything that explains why it is behaving that way.

    Logs are included; the SOURCE CODE is not returned.
    """
    require_admin(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute(
            """
            SELECT j.id, j.name, j.language, j.created_at, j.updated_at,
                   j.runner_job_id, j.worker_url, j.user_id,
                   j.telegram_bot_detected,j.telegram_bot_username,j.telegram_bot_id,
                   j.telegram_check_status,j.telegram_verified_at,j.telegram_framework,
                   j.telegram_update_mode,j.telegram_token_source,
                   u.username AS owner, u.email AS owner_email,
                   u.telegram_id AS owner_telegram,
                   u.telegram_name AS owner_telegram_name,
                   u.is_suspended AS owner_suspended
            FROM jobs j JOIN users u ON u.id = j.user_id
            WHERE j.id = ?
            """,
            (job_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Not found.")
        job = dict(row)
        job["owner_job_count"] = dict(conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE user_id = ?", (job["user_id"],)
        ).fetchone())["c"]
        job["revisions"] = [dict(r) for r in conn.execute(
            "SELECT version,action,status,error,created_at,promoted_at FROM bot_revisions "
            "WHERE job_id=? ORDER BY version DESC LIMIT 20", (job_id,)
        ).fetchall()]
    finally:
        conn.close()

    job["source"] = "telegram" if job.get("owner_telegram") else "website"
    job["source_inferred"] = True
    bot_username = job.get("telegram_bot_username")
    job["telegram_bot_url"] = f"https://t.me/{bot_username}" if bot_username else None
    job.pop("owner_telegram", None)
    worker = job.get("worker_url") or None
    if worker and worker != "embedded":
        runner_url = worker.rstrip("/")
    elif worker == "embedded":
        runner_url = (os.getenv("SITE_BASE_URL", "").strip()
                      or os.getenv("SITE_BASE", "").strip()
                      or os.getenv("PUBLIC_BASE_URL", "").strip()
                      or os.getenv("RENDER_EXTERNAL_URL", "").strip()).rstrip("/")
    else:
        pool = runner_client.runner_pool()
        runner_url = pool[0].rstrip("/") if pool else runner_client.public_base_url().rstrip("/")
    job["worker"] = runner_url or ("embedded engine" if worker == "embedded" else "primary")
    job["runner_url"] = runner_url
    job["runner_health_url"] = f"{runner_url}/health" if runner_url else None

    live, logs, reachable = {}, "", False
    try:
        rid = job.get("runner_job_id")
        if rid:
            resp = runner_client._runner_http(
                "GET", f"/internal/jobs/{rid}", worker=worker)
            if resp is not None and resp.status_code == 200:
                live = resp.json() or {}
                reachable = True
            elif resp is not None and resp.status_code == 404:
                reachable = True
    except Exception as exc:
        logger.warning("admin job detail: runner unreachable for %s (%s)", job_id, exc)

    logs = live.pop("logs", "") or ""
    for k in ("status", "uptime_s", "restarts", "mem_mb", "peak_mem_mb",
              "cpu_pct", "port", "web", "web_slug", "web_public",
              "last_exit_reason", "started_at"):
        job[k] = live.get(k)
    job["libs"] = live.get("libs") or []
    if live.get("web_slug") and live.get("web_public", True) and runner_url:
        job["web_url"] = f"{runner_url}/live/{quote(str(live['web_slug']).strip('/'), safe='')}/"
    else:
        job["web_url"] = None
    # Env VALUES hold bot tokens. Only the key names are ever returned.
    job["env_keys"] = live.get("env_keys") or []
    if not reachable:
        job["status"] = "unknown"
        job["status_stale"] = True
    elif not job.get("status"):
        job["status"] = "offline" if job.get("runner_job_id") else "stopped"

    return {
        "job": job,
        "logs": "\n".join(logs.splitlines()[-200:]),
        "log_truncated": len(logs.splitlines()) > 200,
        "runner_reachable": reachable,
    }


@router.get("/admin/libraries")
def admin_libraries_route(authorization: Optional[str] = Header(None)):
    """Every package installed across every job, by memory then frequency."""
    require_admin(authorization)

    HEAVY = {
        "tensorflow", "torch", "pytorch", "jax", "keras", "transformers",
        "opencv-python", "opencv-contrib-python", "scipy", "pandas", "numpy",
        "scikit-learn", "sklearn", "matplotlib", "playwright", "selenium",
    }
    WATCH = {
        "pycryptodome", "ecdsa", "web3", "eth-account", "bitcoinlib",
        "paramiko", "pyngrok", "requests-html",
    }

    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT j.id, j.name, j.runner_job_id, u.username AS owner "
            "FROM jobs j JOIN users u ON u.id = j.user_id"
        ).fetchall()
        meta = {dict(r)["runner_job_id"]: dict(r) for r in rows if dict(r)["runner_job_id"]}
    finally:
        conn.close()

    live = runner_client.fleet_jobs()

    counts = {}
    for rid, j in live.items():
        m = meta.get(rid) or {}
        mem = float(j.get("mem_mb") or 0.0)
        for lib in (j.get("libs") or []):
            e = counts.setdefault(lib, {"library": lib, "count": 0,
                                        "mem_mb": 0.0, "jobs": []})
            e["count"] += 1
            e["mem_mb"] += mem
            e["jobs"].append({
                "job_id": m.get("id"),
                "name": m.get("name") or j.get("name"),
                "owner": m.get("owner"),
                "mem_mb": round(mem, 1),
                "status": j.get("status"),
            })

    out = sorted(counts.values(), key=lambda e: (-e["mem_mb"], -e["count"], e["library"]))
    for e in out:
        name = e["library"].lower()
        e["heavy"] = name in HEAVY
        e["watch"] = name in WATCH
        e["mem_mb"] = round(e["mem_mb"], 1)
        e["jobs"].sort(key=lambda x: -(x["mem_mb"] or 0))
    total_jobs = len(live) or 1
    for e in out:
        e["pct_of_jobs"] = round(e["count"] / total_jobs * 100)
    return {
        "libraries": out,
        "jobs_sampled": len(live),
        "mem_attributed": True,
        "note": "Counts cover jobs currently known to the runner.",
    }


@router.post("/admin/users/set-suspended")
def admin_set_suspended(payload: AdminSuspend, authorization: Optional[str] = Header(None)):
    admin, _ = require_admin(authorization)
    if payload.user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="You cannot suspend your own account.")
    conn = get_db_connection()
    try:
        target = conn.execute("SELECT id, username FROM users WHERE id=?", (payload.user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found.")
        conn.execute(
            "UPDATE users SET is_suspended=?, updated_at=? WHERE id=?",
            (1 if payload.suspended else 0, now_utc_str(), payload.user_id),
        )
        if payload.suspended:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (payload.user_id,))
        _admin_audit(conn, admin["id"], "suspend" if payload.suspended else "reactivate", target["username"], "")
        conn.commit()
    finally:
        conn.close()
    if payload.suspended:
        _stop_user_jobs_best_effort(payload.user_id)
    return {"message": ("Account suspended. Their sessions are closed and jobs are stopping."
                        if payload.suspended else "Account reactivated.")}


@router.get("/admin/audit-log")
def admin_audit_route(authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT a.id, a.action, a.target, a.details, a.created_at, u.username AS admin_name
            FROM admin_audit_log a LEFT JOIN users u ON u.id = a.admin_id
            ORDER BY a.id DESC LIMIT 100
            """
        ).fetchall()
        return {"audit": [dict(r) for r in rows]}
    finally:
        conn.close()
