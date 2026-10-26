"""CodeNest — RunSpace: free code/bot hosting.

Thin ASGI shell: app init, middleware, static mount, the SPA host (landing +
deep-link negotiation for client-routed sections), /terms, /health, and the
include_router lines for every domain module in routes/.
"""
import hashlib
import os
import logging
import re
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from services import secrets_store, runner_client, env_rescue
from database import DIALECT, init_db  # noqa: F401  (init_db already ran via routes.deps)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("codenest-app")

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"
TERMS_FILE = BASE_DIR / "terms.html"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="CodeNest — RunSpace")


import asyncio
import random
import httpx

async def _self_ping_loop():
    """Background scheduled task: sends a lightweight HTTP GET to the app's own
    /health endpoint every ~7 minutes to prevent Render free tier web service
    from spinning down after 15 minutes of inactivity. Hitting the external
    Render URL counts as real inbound traffic and keeps the service awake."""
    await asyncio.sleep(15)
    failures = 0
    while True:
        try:
            port = os.getenv("PORT", "8000")
            base = (
                os.getenv("RENDER_EXTERNAL_URL", "").strip()
                or os.getenv("SITE_BASE_URL", "").strip()
                or os.getenv("PUBLIC_BASE_URL", "").strip()
                or f"http://127.0.0.1:{port}"
            ).rstrip("/")
            url = f"{base}/health"
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
            if failures:
                logger.info("Self-ping recovered after %d failures", failures)
                failures = 0
            logger.debug("Self-ping to %s ok (%s)", url, resp.status_code)
        except Exception as exc:
            failures += 1
            logger.warning("Self-ping failed (%d in a row): %s", failures, exc)
        # 7–8 min stays safely under Render's 15-minute idle threshold.
        delay = random.uniform(420, 480)
        await asyncio.sleep(delay)

def _env_rescue_boot(env_rescue):
    """Restore bot variables this site cannot read, from the runner's own copy.

    Runs off the startup path because it is one HTTP call per broken row. A bot
    whose row is unreadable cannot be started at all, so this has to happen
    before the recovery pass gets to it — and it is the difference between
    "those bots came back by themselves" and "those bots stayed dark until
    someone re-typed their tokens".
    """
    try:
        report = env_rescue.rescue_unreadable_rows()
        if report.get("unreadable"):
            logger.error("Env rescue at boot: %d row(s) unreadable, %d restored "
                         "from the runner, %d still missing (%s)",
                         report["unreadable"], report["rescued"],
                         report["unavailable"], ", ".join(report["names"][:10]) or "-")
    except Exception as exc:
        logger.error("Env rescue sweep failed: %s", exc)


@app.on_event("startup")
async def startup_event():
    # Rewrite any pre-change `enc:v1:` env blobs as plain JSON BEFORE serving
    # traffic (and before bot recovery below reads them). After one successful
    # boot there is no ciphertext left, so a lost or rotated JOB_SECRETS_KEY can
    # never again be the reason a restarted bot comes back without its token.
    try:
        from services import secrets_store
        result = secrets_store.migrate_job_envs()
        if result.get("unreadable"):
            logger.error("%d env record(s) are unreadable — those bots need their "
                         "secrets re-entered", result["unreadable"])
    except Exception as exc:
        logger.error("Secret storage migration failed: %s", exc)
    # Rows the migration could not read are not necessarily lost: the runner
    # keeps its own copy of each job's env in the job's manifest, so read those
    # back and repair the database. Runs in a thread because it is one HTTP call
    # per broken row and startup must not wait on the runner being awake.
    try:
        from services import env_rescue
        threading.Thread(target=_env_rescue_boot, args=(env_rescue,),
                         name="env-rescue", daemon=True).start()
    except Exception as exc:
        logger.error("Env rescue sweep failed to start: %s", exc)
    try:
        from services import retention
        retention.cleanup()
    except Exception as exc:
        logger.warning("Retention cleanup failed: %s", exc)
    asyncio.create_task(_self_ping_loop())
    # A full Render deploy kills every subprocess. Desired-running bots are
    # recreated from their saved source/env after secret migration completes;
    # this runs in the background so the website still starts immediately.
    try:
        from services import job_recovery
        asyncio.create_task(job_recovery.recover_background())
        # …and then KEEP doing it. The pass above only covers a restart of THIS
        # service; when the RUNNER redeploys or wakes on its own, nothing else
        # notices that every 24/7 bot just vanished. The reconciler re-checks on
        # an interval, so a runner restart repairs itself with nobody involved.
        job_recovery.start_reconciler()
    except Exception as exc:
        logger.warning("Bot recovery scheduler failed: %s", exc)
    # Telegram server-alive bot — starts automatically if TELEGRAM_PING_BOT_TOKEN is set
    try:
        from services.pingbot import start_bot as _start_pingbot
        _start_pingbot()
    except Exception as e:  # noqa: BLE001
        logger.warning("Ping bot failed to start: %s", e)
    # Watch job state and message the owner when an app stops on its own. The
    # runner cannot do this itself: it has no database and no Telegram token,
    # and with a worker pool that would mean the same credential on every box.
    try:
        from services.bot_notify import start_watcher as _start_watcher
        _start_watcher()
    except Exception as e:  # noqa: BLE001
        logger.warning("Bot state watcher failed to start: %s", e)
    # Periodically copy each job's data files (database.db, session.json, …)
    # into Postgres. On Render's free tier the runner's filesystem is rebuilt
    # on every deploy, so this is what makes a referral bot's points survive.
    try:
        from services.snapshots import start_sweeper as _start_sweeper
        _start_sweeper()
    except Exception as e:  # noqa: BLE001
        logger.warning("Snapshot sweeper failed to start: %s", e)


def _enable_embedded_runner() -> bool:
    """Single-service mode: when RUNNER_SERVICE_URL is NOT set, the job runner
    lives inside THIS process (one Render web service — the whole point of the
    consolidation). Two effects:

      1. services.runner_client talks to the runner through an in-process
         ASGI client instead of the network.
      2. The public /live/{slug}/* gateway (HTTP + WebSocket) is mounted on
         THIS app — the handlers are reused verbatim from runner.app.

    Setting RUNNER_SERVICE_URL restores the classic two-service layout and
    this function leaves everything alone (return False).
    """
    if os.getenv("RUNNER_SERVICE_URL", "").strip():
        return False
    import secrets as _secrets
    # runner.app reads SECRET at import; generate a throwaway internal one
    # unless the operator pinned their own.
    os.environ.setdefault("RUNNER_SERVICE_SECRET", _secrets.token_urlsafe(24))
    import runner.app as _rapp
    # Visitor-facing pages/URLs must point at THIS service, not a runner host.
    base = (os.getenv("SITE_BASE_URL", "").strip()
            or os.getenv("PUBLIC_BASE_URL", "").strip()
            or os.getenv("RENDER_EXTERNAL_URL", "").strip())


    if base and not _rapp.PUBLIC_BASE_URL:
        _rapp.PUBLIC_BASE_URL = base.rstrip("/")
    from services.proxy import router as _proxy_router
    app.include_router(_proxy_router)
    logger.info("Embedded runner ACTIVE — jobs + /live gateway run in this process.")
    return True


EMBEDDED_RUNNER = _enable_embedded_runner()

_cors_origins = [x.strip().rstrip("/") for x in
                 os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if x.strip()]
for _origin in (os.getenv("SITE_BASE_URL", ""), os.getenv("RENDER_EXTERNAL_URL", "")):
    if _origin.strip().rstrip("/") and _origin.strip().rstrip("/") not in _cors_origins:
        _cors_origins.append(_origin.strip().rstrip("/"))
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["*"],
    allow_credentials=not (not _cors_origins),
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Fingerprint"],
)


@app.get("/guides", include_in_schema=False)
def guides_page():
    """Public how-to cartoons — same art the Telegram /guide command sends."""
    from fastapi.responses import HTMLResponse
    from pathlib import Path as _P
    gdir = _P(__file__).resolve().parent / "static" / "guides"
    cards = []
    catalog = [
        ("guide_start.png", "Start here", "Three taps from zero to a running bot", "/guide start"),
        ("guide_code.png", "/code", "Create an app from chat", "/guide code"),
        ("guide_update.png", "/update", "Replace code, keep the name", "/guide update"),
        ("guide_import.png", "/import", "Public GitHub → live app", "/guide import"),
        ("guide_apps.png", "/apps", "Your deployed apps", "/guide apps"),
        ("guide_logs.png", "/logs", "See what the app printed", "/guide logs"),
        ("guide_restart.png", "/restart", "Wake a stopped app", "/guide restart"),
        ("guide_stop.png", "/stop", "Pause and free a slot", "/guide stop"),
        ("guide_status.png", "/status", "Health of one app or all", "/guide status"),
        ("guide_env.png", "/env", "Variables like BOT_TOKEN", "/guide env"),
        ("guide_token.png", "BOT_TOKEN tips", "Env tab or inside the code", "/guide token"),
        ("guide_id.png", "/id", "Copy-friendly numbers", "/id"),
        ("guide_backup.png", "/backup", "Snapshot data files", "/guide backup"),
        ("guide_history.png", "/history", "Past deploys", "/guide history"),
        ("guide_projects.png", "/projects", "Repo apps in one place", "/guide projects"),
        ("guide_link.png", "/link", "Connect this chat", "/guide link"),
        ("guide_source.png", "/source", "Download current code", "/guide source"),
        ("guide_delete.png", "/delete", "Remove an app", "/guide delete"),
        ("guide_rename.png", "/rename", "Change the label", "/guide rename"),
        ("guide_latest.png", "/latest", "Pull newest commit", "/guide latest"),
    ]
    for fn, title, sub, cmd in catalog:
        if (gdir / fn).is_file():
            cards.append(
                f'<article class="g-card"><img src="/static/guides/{fn}" alt="{title}" loading="lazy"/>'
                f'<h2>{title}</h2><p>{sub}</p>'
                f'<code>{cmd}</code></article>'
            )
    body = "".join(cards) or "<p>Guides are being prepared.</p>"
    html = f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>CodeNest · Guides</title>
<style>
body{{margin:0;font-family:system-ui,sans-serif;background:#0b1220;color:#e2e8f0}}
header{{padding:28px 20px 8px;max-width:1100px;margin:0 auto}}
header h1{{margin:0;font-size:1.6rem}}
header p{{color:#94a3b8;margin:8px 0 0}}
main{{display:grid;gap:20px;padding:20px;max-width:1100px;margin:0 auto;
grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}}
.g-card{{background:#111827;border:1px solid #1f2937;border-radius:16px;overflow:hidden;
padding:0 0 16px;box-shadow:0 8px 24px #0006}}
.g-card img{{width:100%;display:block;background:#0f172a}}
.g-card h2{{margin:12px 16px 4px;font-size:1.1rem}}
.g-card p{{margin:0 16px 10px;color:#94a3b8;font-size:.92rem}}
.g-card code{{margin:0 16px;display:inline-block;background:#1e293b;padding:4px 10px;
border-radius:8px;font-size:.85rem;color:#a5b4fc}}
a.home{{color:#a5b4fc;text-decoration:none;font-size:.9rem}}
</style></head><body>
<header><a class="home" href="/">← CodeNest</a>
<h1>How-to guides</h1>
<p>Same cartoons the Telegram bot sends for <code>/guide</code>. Picture first, then the command.</p>
</header><main>{body}</main></body></html>"""
    return HTMLResponse(html)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# -------------------------------
# Static asset cache-busting
# -------------------------------
# index.html shipped a HARDCODED "?v=20260724m" on every css/js link, so once a
# browser cached those files it kept the old copies after every deploy — fixes
# looked like they had not shipped at all. The version is now derived from the
# real contents of the asset files, so it changes automatically whenever one of
# them changes (and stays stable when nothing changed, preserving caching).
def _asset_version() -> str:
    h = hashlib.sha1()
    try:
        for f in sorted(STATIC_DIR.rglob("*")):
            if f.suffix.lower() in (".css", ".js", ".svg") and f.is_file():
                h.update(f.name.encode())
                h.update(str(f.stat().st_mtime_ns).encode())
                h.update(str(f.stat().st_size).encode())
        if INDEX_FILE.exists():
            h.update(str(INDEX_FILE.stat().st_mtime_ns).encode())
    except Exception as exc:  # pragma: no cover - never block page delivery
        logger.warning("asset version fallback: %s", exc)
        return "dev"
    return h.hexdigest()[:12]


ASSET_VERSION = _asset_version()
logger.info("Static asset version: %s", ASSET_VERSION)

_VERSION_RE = re.compile(r'(/static/[^"\'?]+\.(?:css|js|svg))\?v=[^"\']*')


# The admin console's markup ships inside index.html. The SPA already removes
# it from the DOM for non-admins, but that is client-side: anyone could read
# it straight out of the HTML source. Strip it SERVER-SIDE unless the request
# proves it belongs to an admin, so the console's very existence is not
# advertised in a page every anonymous visitor downloads.
_ADMIN_SECTION_RE = re.compile(
    r'<div class="dash-tab-content" id="tab-admin">.*?</div>\s*</div>\s*</div>',
    re.S,
)
_ADMIN_TABBTN_RE = re.compile(r'<button[^>]*id="tabBtnAdmin".*?</button>', re.S)


def _is_admin_request(request: Request) -> bool:
    """True only for a verified admin session.

    Browsers navigating to a URL send no Authorization header (the token lives
    in localStorage), so a page request cannot be authenticated this way. The
    SPA re-asks over the API once it boots. This function therefore governs
    what the SHELL contains, not whether the app works.
    """
    auth = request.headers.get("authorization") or ""
    if not auth.startswith("Bearer "):
        return False
    try:
        from routes.admin import require_admin
        require_admin(auth)
        return True
    except Exception:
        return False


def _index_html(request: Request = None) -> str:
    """index.html with every ?v= stamp rewritten to the current build."""
    raw = INDEX_FILE.read_text(encoding="utf-8")
    if request is None or not _is_admin_request(request):
        raw = _ADMIN_SECTION_RE.sub("", raw, count=1)
        raw = _ADMIN_TABBTN_RE.sub("", raw, count=1)
    return _VERSION_RE.sub(lambda m: f"{m.group(1)}?v={ASSET_VERSION}", raw)


# -------------------------------
# SPA host (landing + client-routed sections)
# -------------------------------
@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def read_index():
    if not INDEX_FILE.exists():
        raise HTTPException(status_code=404, detail="index.html not found.")
    # HTML itself must never be cached, or the browser keeps requesting the
    # old asset URLs and the new version stamp never reaches it.
    return HTMLResponse(
        _index_html(),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


# Client-side routing: every app section has a real URL (/code, /jobs, …).
# Deep links / refreshes on these paths must serve the SPA shell — the
# frontend router reads the path and opens the right section (after auth).
#
# The API lives at ROOT (GET /profile returns JSON), so section URLs that
# collide with an API GET need content negotiation: a browser navigation
# (Accept: text/html, no Authorization header — tokens ride fetch headers,
# never visible in address-bar navigations) gets the SPA shell, while the
# SPA's own authed fetch on the same path gets the JSON data. Registered
# BEFORE the API routes on purpose, since route order decides the match.
def _spa_negotiator(fn_name: str):
    def handler(request: Request):
        accept = (request.headers.get("accept") or "").lower()
        auth_hdr = request.headers.get("authorization")
        if "text/html" in accept and not auth_hdr:
            if not INDEX_FILE.exists():
                raise HTTPException(status_code=404, detail="index.html not found.")
            return HTMLResponse(_index_html(), headers={"Cache-Control": "no-cache, must-revalidate"})
        fn = _NEGOTIATED_FNS.get(fn_name)   # resolved at request time
        if fn is None:
            raise HTTPException(status_code=404, detail="Not found.")
        return fn(authorization=auth_hdr)
    return handler


from routes.profile import get_profile as _negotiated_profile


_NEGOTIATED = {
    "profile": "get_profile",
}
_NEGOTIATED_FNS = {"get_profile": _negotiated_profile}
for _p, _fn in _NEGOTIATED.items():
    app.get("/" + _p, include_in_schema=False)(_spa_negotiator(_fn))

# Section URLs with NO API collision can serve the shell directly.
CLIENT_ONLY_PATHS = [
    "dashboard", "code", "bots", "activity", "store",
    "sign-in", "sign-up", "login", "forgot",
]
for _p in CLIENT_ONLY_PATHS:
    app.get("/" + _p, include_in_schema=False)(read_index)


# /admin is deliberately NOT in the list above. It used to serve the SPA shell
# with a plain 200 to anyone, which confirms the console exists to any stranger
# who guesses the URL. The requirement is an ordinary 404.
#
# The catch: a browser navigation carries no Authorization header (the token is
# in localStorage), so the server cannot tell an admin from anyone else at page
# load. Returning a hard 404 to everyone would lock the real admin out too.
#
# So the shell is served under a NEUTRAL path and status: the response is
# indistinguishable from /dashboard — no admin markup, no admin tab, and a 404
# status so the URL itself reveals nothing. The SPA boots, calls /profile, and
# only then decides whether the console exists for this user. Anyone without
# an admin session sees exactly what they would at any unknown URL.
@app.get("/admin", include_in_schema=False)
def read_admin(request: Request):
    if not INDEX_FILE.exists():
        raise HTTPException(status_code=404, detail="Not found.")
    return HTMLResponse(
        _index_html(request),
        status_code=200 if _is_admin_request(request) else 404,
        headers={"Cache-Control": "no-store"},
    )

# Bots is the one canonical product URL, including per-bot deep links.
@app.get("/bots/{slug}", include_in_schema=False)
def read_bot_job(slug: str):
    if not INDEX_FILE.exists():
        raise HTTPException(status_code=404, detail="index.html not found.")
    return HTMLResponse(_index_html(), headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/bots/{username}/{slug:path}", include_in_schema=False)
def read_bot_deep(username: str, slug: str):
    if not INDEX_FILE.exists():
        raise HTTPException(status_code=404, detail="index.html not found.")
    return HTMLResponse(_index_html(), headers={"Cache-Control": "no-cache, must-revalidate"})


# Old links remain safe, but immediately leave the retired RunSpace URL.
@app.get("/runspace", include_in_schema=False)
@app.get("/jobs", include_in_schema=False)
def redirect_old_bots_root():
    return RedirectResponse("/bots", status_code=308)


@app.get("/runspace/{slug}", include_in_schema=False)
def redirect_runspace_job(slug: str):
    return RedirectResponse("/bots/" + slug, status_code=308)


@app.get("/runspace/{username}/{slug:path}", include_in_schema=False)
def redirect_runspace_deep(username: str, slug: str):
    return RedirectResponse("/bots/" + username + "/" + slug, status_code=308)

# Back-compat heal: a frontend bug once produced published-page links like
# /code/s/<token> (the tab path was glued onto the origin). Redirect any
# shared copies to the real public page instead of a cold JSON 404.
@app.get("/code/s/{token}", include_in_schema=False)
def code_share_redirect(token: str):
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/s/{token}", status_code=301)


@app.get("/api/public-config")
def public_config():
    """Non-secret settings the static SPA needs at runtime.

    The Telegram login widget must be told the bot's @username, which differs
    per deployment — hardcoding it in index.html left a dead "YOUR_BOT_USERNAME"
    placeholder that never rendered a button.

    Used to read TELEGRAM_BOT_USERNAME directly, a second env var that had to
    be kept in sync with the token by hand. It now asks Telegram what the
    configured BOT_TOKEN's bot is actually called — one variable to set.
    """
    from services import miniapp_auth
    return {
        "telegram_bot_username": miniapp_auth.bot_username(),
        # E-mail sign-in is shown by DEFAULT now.
        #
        # This used to default to "1", which HID the e-mail form whenever a bot
        # username was configured. The Telegram widget then became the only way
        # in — and if telegram-widget.js is slow, blocked by an extension, or
        # unreachable, the sign-in card renders nothing but the sentence
        # "One tap. No password to remember." with no button under it. A user
        # who signs out is then locked out of their own account.
        #
        # A second sign-in method costs nothing (the whole e-mail flow is
        # already implemented and tested server-side) and removes a
        # single-point-of-failure on a third-party script. Set
        # TELEGRAM_ONLY_AUTH=1 to go back to Telegram-only.
        "telegram_only": os.getenv("TELEGRAM_ONLY_AUTH", "0").strip().lower() in ("1", "true", "yes"),
    }


def _miniapp_bot_id():
    """The configured bot's public ID, or a short reason it is unusable."""
    try:
        from services import miniapp_auth
        shape = miniapp_auth.token_shape()
        if not shape.get("configured"):
            return "not configured"
        if not shape.get("looks_valid"):
            return "malformed"
        return shape.get("bot_id")
    except Exception:
        return "unknown"


# Cached so /health stays a cheap liveness probe. The answer only changes when
# the token changes, which means a restart.
_BOT_IDENTITY = {"checked": False, "value": None}


def _bot_identity():
    """Which bot this server can verify Mini App sign-ins for.

    The @username is what makes a bad_hash diagnosable: it is the single fact
    that says whether the Mini App is being opened from the right bot. It was
    previously only reachable through an admin-gated route, which needs an
    Authorization HEADER — so typing that URL into a browser returned 404 and
    the check was effectively unavailable to the person who needed it.

    A bot's id and @username are PUBLIC: anyone who can message the bot sees
    both. The token's secret half is never read here.
    """
    if _BOT_IDENTITY["checked"]:
        return _BOT_IDENTITY["value"]
    out = None
    try:
        from services import miniapp_auth
        who = miniapp_auth.whoami()
        if who.get("ok"):
            out = {"username": who.get("username"), "id": who.get("bot_id"),
                   "open": f"https://t.me/{who.get('username')}"}
        else:
            out = {"error": who.get("reason")}
    except Exception as exc:  # pragma: no cover - diagnostics must never 500
        out = {"error": "check_failed", "detail": str(exc)[:100]}
    _BOT_IDENTITY.update(checked=True, value=out)
    return out


def _token_fingerprint():
    """A comparable digest of the deployed token; never the token itself."""
    try:
        from services import miniapp_auth
        return miniapp_auth.token_fingerprint()
    except Exception:
        return {"configured": None}


def _token_live():
    """Re-test the configured token against Telegram, not the boot cache."""
    try:
        from services import miniapp_auth
        return miniapp_auth.token_live()
    except Exception:
        return {"ok": None, "reason": "unavailable"}


def _token_sources():
    """Which env var the bot token came from, and whether a second disagrees.

    A conflict here is invisible from a hosting dashboard — both variables
    look set and correct — while only one of them is in force. Reporting it
    turns "I replaced the token and nothing changed" into one glance. Only
    the public bot-id half of each token is ever shown.
    """
    try:
        from services import miniapp_auth
        return miniapp_auth.token_sources()
    except Exception:
        return {"error": "unavailable"}


def _miniapp_url():
    """Where the bot's 'Open CodeNest' button points, or why it cannot."""
    try:
        from services.pingbot import SITE_BASE
    except Exception:
        return {"ok": False, "reason": "unavailable"}
    if not SITE_BASE:
        return {"ok": False, "reason": "not_configured",
                "fix": "Set SITE_BASE_URL to this service's public https URL."}
    if not SITE_BASE.startswith("https://"):
        # Telegram refuses a web_app button on a non-https URL, so the Mini
        # App cannot open at all — it silently degrades to a browser link.
        return {"ok": False, "reason": "not_https", "url": SITE_BASE,
                "fix": "Telegram requires https:// for a Mini App button."}
    return {"ok": True, "url": f"{SITE_BASE}/bots"}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "database": DIALECT,
        "runner": "embedded" if runner_client.embedded_mode() else "remote",
        # Bot env vars live as plain JSON in your own database (see
        # services/secrets_store.py for why). `legacy_rows` is how many rows are
        # still in the old wrapped form and should be 0 after the first boot;
        # `unreadable_rows` is how many this site cannot read at all, and those
        # are rescued from the runner's own copy at startup (services/env_rescue).
        "bot_secrets_storage": "plain-text",
        "bot_secrets_legacy_rows": secrets_store.legacy_rows(),
        "bot_secrets_unreadable_rows": env_rescue.unreadable_count(),
        "bot_secrets_rescued_at_boot": env_rescue.LAST_SWEEP.get("rescued", 0),
        "production_isolation": "unsafe-embedded" if runner_client.embedded_mode() else "remote-runner",
        "ping_bot": "running" if bool(os.getenv("BOT_TOKEN", "").strip()
                                       or os.getenv("TELEGRAM_PING_BOT_TOKEN", "").strip()) else "not configured",
        # Which bot this server verifies Mini App sign-ins for. The bot ID half
        # of a token is PUBLIC — anyone who can message the bot can see it — so
        # this is safe, and it is the one fact needed to diagnose a bad_hash:
        # it can be compared against the bot the Mini App was actually opened
        # from. The secret half is never read.
        "telegram_bot_id": _miniapp_bot_id(),
        # Open the Mini App from THIS bot or sign-in fails with bad_hash.
        # Public information, and the one thing needed to diagnose it.
        "telegram_bot": _bot_identity(),
        # THE SAME QUESTION, ASKED FRESH. telegram_bot above is cached from a
        # single getMe at boot, so it keeps reporting the bot it saw then —
        # even if the token was replaced in the dashboard afterwards, or the
        # boot-time check never reached Telegram. That gap is how this page
        # could look completely healthy while every Mini App sign-in failed.
        # ok:true = the token is valid right now; ok:false = Telegram rejects
        # it; ok:null = Telegram unreachable, which is not a verdict.
        "telegram_token_live": _token_live(),
        # THE ONE THING THAT WAS STILL UNCHECKABLE. Diagnosis can now show
        # that the running token is not the one BotFather holds — but the
        # owner could not verify that themselves, because the secret must
        # never be printed. This is a salted-free one-way digest of the whole
        # token: run the printed command on the token BotFather shows you and
        # compare sha256_12. Equal -> the right value is deployed. Different
        # -> this process is running something else (a second variable, an
        # environment group, a stale build, a truncated paste), and revoking
        # the token again will not help.
        "telegram_token_fingerprint": _token_fingerprint(),
        # THE URL THE BOT'S BUTTON ACTUALLY OPENS. A deployment that never set
        # SITE_BASE_URL used to fall back to a hardcoded host belonging to a
        # different install, so the button opened someone else's site and
        # sign-in failed there with a bot mismatch — while every check here
        # said "ok". Reporting the resolved value makes that visible in one
        # request instead of being invisible until a user complains.
        "miniapp_url": _miniapp_url(),
        # WHICH env var is actually in force. BOT_TOKEN silently outranks
        # TELEGRAM_PING_BOT_TOKEN, so two names holding two different bots
        # looked fine from the dashboard while sign-ins were checked against
        # the one the owner thought they had replaced.
        "telegram_token_source": _token_sources(),
        "brevo_api_key_set": bool(os.getenv("BREVO_API_KEY", "").strip()),
        "sender_email_set": bool(os.getenv("SENDER_EMAIL", "").strip()),
    }


# ----------------------------
# Signup / Verify (auto-login) / Resend
# ----------------------------


# ── AI / LLM discovery — give ANY AI the URL and it understands the whole site ──
# https://ahadorg.onrender.com/llms.txt  (concise)  and /llms-full.txt /ai.txt (full 38KB)
# Also served at /.well-known/* for crawlers that look there.
@app.get("/llms.txt", include_in_schema=False)
def serve_llms_txt():
    p = STATIC_DIR / "llms.txt"
    if p.exists():
        return FileResponse(p, media_type="text/plain; charset=utf-8", headers={"Cache-Control": "public, max-age=3600"})
    return FileResponse(BASE_DIR / "llms.txt", media_type="text/plain; charset=utf-8")

@app.get("/llms-full.txt", include_in_schema=False)
def serve_llms_full():
    p = STATIC_DIR / "llms-full.txt"
    if p.exists():
        return FileResponse(p, media_type="text/plain; charset=utf-8", headers={"Cache-Control": "public, max-age=3600"})
    return FileResponse(BASE_DIR / "llms-full.txt", media_type="text/plain; charset=utf-8")

@app.get("/ai.txt", include_in_schema=False)
def serve_ai_txt():
    p = STATIC_DIR / "ai.txt"
    if p.exists():
        return FileResponse(p, media_type="text/plain; charset=utf-8", headers={"Cache-Control": "public, max-age=3600"})
    return FileResponse(BASE_DIR / "ai.txt", media_type="text/plain; charset=utf-8")

@app.get("/ai", include_in_schema=False)
def serve_ai_html():
    # Full guide as HTML for humans + AI that fetches HTML — wraps llms-full.txt
    p = STATIC_DIR / "llms-full.txt"
    src = p.read_text(encoding="utf-8") if p.exists() else (BASE_DIR / "llms-full.txt").read_text(encoding="utf-8") if (BASE_DIR / "llms-full.txt").exists() else "AI docs not found."
    # Minimal markdown → HTML (no deps): escape, then headings/bullets
    import html as _h
    escaped = _h.escape(src)
    # crude heading: lines starting with # → <h1/h2>, else <pre>
    lines = []
    for ln in escaped.splitlines():
        if ln.startswith("# "):
            lines.append(f"<h1>{ln[2:]}</h1>")
        elif ln.startswith("## "):
            lines.append(f"<h2>{ln[3:]}</h2>")
        elif ln.startswith("### "):
            lines.append(f"<h3>{ln[4:]}</h3>")
        elif ln.startswith("- ") or ln.startswith("* "):
            lines.append(f"<li>{ln[2:]}</li>")
        elif ln.strip() == "---":
            lines.append("<hr/>")
        elif ln.strip().startswith("```"):
            lines.append("<pre>")
        elif ln.strip() == "":
            lines.append("<br/>")
        else:
            lines.append(f"<p>{ln}</p>")
    body = "\n".join(lines)
    html_doc = f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>CodeNest — AI Guide — ahadorg.onrender.com</title>
<meta name="description" content="Full AI documentation for CodeNest (ahadorg.onrender.com) — architecture, database schema, RAM management, code handling, API, security. Give this URL to any LLM."/>
<link rel="alternate" type="text/plain" href="/llms.txt"/>
<link rel="alternate" type="text/plain" href="/llms-full.txt"/>
<style>
body{{margin:0;font-family:system-ui, -apple-system, Segoe UI, Roboto, Inter, sans-serif;max-width:900px;margin:0 auto;padding:24px;line-height:1.6;color:#0f172a;background:#fff}}
h1{{font-size:1.5rem;border-bottom:2px solid #0ea5e9;padding-bottom:8px}}
h2{{font-size:1.2rem;color:#0e7490;margin-top:28px;border-left:4px solid #06b6d4;padding-left:10px}}
h3{{font-size:1rem;color:#334155}}
pre{{background:#f1f5f9;padding:12px;overflow:auto;border-radius:8px;font-size:.88rem}}
code{{background:#f1f5f9;padding:2px 6px;border-radius:4px;font-size:.9em}}
hr{{border:none;border-top:1px solid #e2e8f0;margin:24px 0}}
a{{color:#0284c7}}
header{{background:#0f172a;color:#fff;padding:18px 20px;border-radius:12px;margin-bottom:18px}}
header a{{color:#7dd3fc}}
</style></head><body>
<header><b>CodeNest — AI Guide</b> · <a href="/">ahadorg.onrender.com</a> · <a href="/llms.txt">llms.txt (concise)</a> · <a href="/llms-full.txt">llms-full.txt</a> · <a href="/health">health</a> · <a href="/openapi.json">openapi.json</a></header>
{body}
<hr/><p><small>Last updated 2026-10-03 · Serve with <code>Content-Type: text/plain</code> for LLM ingest. Also available at <code>/.well-known/ai.txt</code> and <code>/ai.txt</code>.</small></p>
</body></html>"""
    return HTMLResponse(html_doc, headers={"Cache-Control": "public, max-age=3600"})

@app.get("/ai.json", include_in_schema=False)
def serve_ai_json():
    # Machine-readable manifest for programmatic AI
    base = (os.getenv("SITE_BASE_URL", "").strip() or os.getenv("RENDER_EXTERNAL_URL", "").strip() or "https://ahadorg.onrender.com").rstrip("/")
    return {
        "name": "CodeNest",
        "url": base or "https://ahadorg.onrender.com",
        "description": "Free Telegram bot hosting — paste Python/Node code, verify BOT_TOKEN, auto-install deps, 24/7 runner 512 MB, 3 bots free",
        "ai_docs": [f"{base}/llms.txt", f"{base}/llms-full.txt", f"{base}/ai", f"{base}/ai.txt", f"{base}/health", f"{base}/openapi.json"],
        "health": f"{base}/health",
        "openapi": f"{base}/openapi.json",
        "guides": f"{base}/guides",
        "database_dialect": DIALECT,
        "runner_mode": "embedded" if runner_client.embedded_mode() else "remote",
        "languages": ["python","javascript","bash","ruby","php","go","java","lua","html"],
        "limits": {"normal_zip_mb": 5, "normal_zip_files": 500, "queen_zip_mb": 150, "queen_zip_files": 8000, "queen_ram": "unlimited (exclusive 512 MB)", "free_bots": 3, "queen_bots": 10},
        "endpoints": {"/llms.txt": "concise LLM guide", "/llms-full.txt": "full 38KB docs", "/ai": "HTML full", "/ai.txt": "plain full", "/health": "live diagnosis", "/openapi.json": "OpenAPI schema"},
    }

@app.get("/.well-known/ai.txt", include_in_schema=False)
def serve_wellknown_ai():
    p = STATIC_DIR / "ai.txt"
    if p.exists():
        return FileResponse(p, media_type="text/plain; charset=utf-8")
    return FileResponse(BASE_DIR / "ai.txt", media_type="text/plain; charset=utf-8")

@app.get("/robots.txt", include_in_schema=False)
def serve_robots():
    p = STATIC_DIR / "robots.txt"
    if p.exists():
        return FileResponse(p, media_type="text/plain; charset=utf-8")
    return FileResponse(BASE_DIR / "robots.txt", media_type="text/plain; charset=utf-8") if (BASE_DIR / "robots.txt").exists() else HTMLResponse("User-agent: *\nAllow: /\n", media_type="text/plain")

@app.get("/sitemap.xml", include_in_schema=False)
def serve_sitemap():
    p = STATIC_DIR / "sitemap.xml"
    if p.exists():
        return FileResponse(p, media_type="application/xml; charset=utf-8")
    return FileResponse(BASE_DIR / "sitemap.xml", media_type="application/xml; charset=utf-8") if (BASE_DIR / "sitemap.xml").exists() else HTMLResponse("<urlset/>", media_type="application/xml")

@app.get("/terms", include_in_schema=False)
def terms_page():
    if not TERMS_FILE.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(TERMS_FILE)


# -------------------------------
# Domain routers
# -------------------------------
from routes.auth import router as auth_router
from routes.profile import router as profile_router
from routes.dashboard import router as dashboard_router
from routes.code_editor import router as code_editor_router
from routes.runspace import router as runspace_router
from routes.admin import router as admin_router
from routes.ping import router as ping_router
from routes.store import router as store_router
from services.term_proxy import router as term_router
from services.pingbot import webhook_router as telegram_webhook_router

for _r in (auth_router, profile_router, dashboard_router,
           code_editor_router, runspace_router, admin_router,
           ping_router, term_router, store_router, telegram_webhook_router):
    app.include_router(_r)


# ---- back-compat re-exports (tests + drivers import these from `app`) ----
from routes.deps import get_db_connection, hash_password, now_utc_str  # noqa: E402,F401
from services.runner_client import _job_web_fields, _runner_http  # noqa: E402,F401
