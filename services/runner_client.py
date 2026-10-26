"""Client for the RunSpace runner (job execution + management) — TWO modes:

  • EMBEDDED (default): RUNNER_SERVICE_URL unset → the runner lives INSIDE
    this process (single Render web service). Calls go through an in-process
    ASGI client (fastapi.testclient.TestClient) — identical request/response
    semantics, zero network, zero second service.

  • REMOTE: RUNNER_SERVICE_URL set → classic server-to-server HTTPS calls to
    the separate runner service (kept for anyone running the two-service
    layout; the runner/ folder still ships standalone).

Runner URL/secret stay server-side; the browser never sees them. Test drivers
monkeypatch runner_client._runner_http — call it module-attr style.
"""
import os
import logging
import time

import requests
from fastapi import HTTPException

logger = logging.getLogger("codenest-app")

# Per-ACCOUNT fairness limit, enforced by the main site. Distinct from the
# runner's MAX_BG_JOBS, which is a per-container RAM limit. Env-tunable so the
# ceiling can move with capacity without a code change.
MAX_JOBS_PER_USER = int(os.getenv("MAX_JOBS_PER_USER", "3"))

_registry_cache = {"at": 0.0, "nodes": []}


def invalidate_runner_registry():
    _registry_cache.update(at=0.0, nodes=[])
    _health_cache.clear() if "_health_cache" in globals() else None
    _fleet_cache.update(at=0.0, jobs=None) if "_fleet_cache" in globals() else None


def managed_runner_nodes(refresh=False) -> list:
    """Enabled DB-managed runners with server-side credentials loaded from the DB."""
    now = time.time()
    if not refresh and now - _registry_cache["at"] < 10:
        return list(_registry_cache["nodes"])
    try:
        from database import get_db_connection
        from services import secrets_store
        conn = get_db_connection()
        try:
            rows = conn.execute(
                "SELECT id,label,url,encrypted_secret,created_at,updated_at "
                "FROM runner_nodes WHERE enabled=1 ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        nodes = []
        for row in rows:
            item = dict(row)
            secret = secrets_store.unpack_env(item.pop("encrypted_secret", None)).get("secret", "")
            if secret:
                item["secret"] = secret
                item["url"] = item["url"].rstrip("/")
                nodes.append(item)
        _registry_cache.update(at=now, nodes=nodes)
        return list(nodes)
    except Exception as exc:
        logger.debug("managed runner registry unavailable: %s", exc)
        return list(_registry_cache["nodes"])


# ── Queen runner pin — exclusive 512MB for queen users (no env var) ──
def get_queen_pin(user_id: int) -> dict | None:
    """Return queen's pinned runner (if any): {runner_url, exclusive}."""
    try:
        from database import get_db_connection
        conn = get_db_connection()
        try:
            row = conn.execute("SELECT runner_url, exclusive FROM queen_runner_pins WHERE user_id=?", (int(user_id),)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception:
        return None

def set_queen_pin(user_id: int, runner_url: str, exclusive: int = 1) -> bool:
    """Pin a queen's jobs to one runner. exclusive=1 means other users won't be placed there."""
    runner_url = (runner_url or "").strip().rstrip("/")
    if not runner_url:
        return False
    # must be in pool (managed or env) to avoid pinning to ghost
    if runner_url not in runner_pool():
        # still allow if it's a known managed node URL (maybe disabled?), but check DB
        try:
            from database import get_db_connection
            conn2 = get_db_connection()
            try:
                r = conn2.execute("SELECT 1 FROM runner_nodes WHERE url=?", (runner_url,)).fetchone()
                if not r:
                    return False
            finally:
                conn2.close()
        except Exception:
            return False
    try:
        from database import get_db_connection
        conn = get_db_connection()
        try:
            now = __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())
            conn.execute(
                "INSERT INTO queen_runner_pins (user_id, runner_url, exclusive, created_at, updated_at) "
                "VALUES (?,?,?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET runner_url=excluded.runner_url, exclusive=excluded.exclusive, updated_at=excluded.updated_at",
                (int(user_id), runner_url, int(exclusive), now, now),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        return False

def clear_queen_pin(user_id: int) -> bool:
    try:
        from database import get_db_connection
        conn = get_db_connection()
        try:
            cur = conn.execute("DELETE FROM queen_runner_pins WHERE user_id=?", (int(user_id),))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception:
        return False

def list_exclusive_runners() -> dict:
    """Map url -> owner_user_id for exclusive pins."""
    try:
        from database import get_db_connection
        conn = get_db_connection()
        try:
            rows = conn.execute("SELECT user_id, runner_url FROM queen_runner_pins WHERE exclusive=1").fetchall()
            return {str(r["runner_url"]).rstrip("/"): int(r["user_id"]) for r in rows}
        finally:
            conn.close()
    except Exception:
        return {}

def choose_runner_for_user(user_id: int | None = None) -> str | None:
    """Best runner for this user, respecting queen pins and free memory."""
    pool = runner_pool()
    if not pool:
        return None
    # queen's own pin first, even if runner looks busy — queen asked for it
    if user_id is not None:
        pin = get_queen_pin(int(user_id))
        if pin and pin.get("runner_url"):
            url = str(pin["runner_url"]).rstrip("/")
            if url in pool:
                # verify it is online (or at least not suspended); if offline we still try but fallback to others if 503
                # we return pinned url directly — placement will try it first
                return url
    # otherwise use placement order for this user
    order = _placement_order(user_id=user_id)
    return order[0] if order else None

def _secret_for_runner(url):
    clean = (url or "").rstrip("/")
    for node in managed_runner_nodes():
        if node["url"] == clean:
            return node["secret"]
    # Disabled means no NEW placement, not abandoning jobs already on it.
    try:
        from database import get_db_connection
        from services import secrets_store
        conn = get_db_connection()
        try:
            row = conn.execute("SELECT encrypted_secret FROM runner_nodes WHERE url=?", (clean,)).fetchone()
        finally:
            conn.close()
        if row:
            return secrets_store.unpack_env(dict(row)["encrypted_secret"]).get("secret", "")
    except Exception:
        pass
    return os.getenv("RUNNER_SERVICE_SECRET", "").strip()


def runner_pool() -> list:
    """Every runner this site can place jobs on, in preference order.

    Capacity scales by ADDING SERVICES, not by raising a limit past the RAM a
    container actually has. Each Render free instance is 512MB and holds
    ~MAX_BG_JOBS bots; a second URL doubles the ceiling, a third triples it.

        RUNNER_SERVICE_URL   = https://runner-a.onrender.com
        RUNNER_SERVICE_URLS  = https://runner-b.onrender.com,https://runner-c...

    Both are read so existing single-runner deployments keep working
    untouched. Order is stable and duplicates are dropped, so a job lands on
    the first runner with room rather than bouncing between them.
    """
    urls = []
    primary = os.getenv("RUNNER_SERVICE_URL", "").strip().rstrip("/")
    if primary:
        urls.append(primary)
    for raw in os.getenv("RUNNER_SERVICE_URLS", "").replace(" ", ",").split(","):
        u = raw.strip().rstrip("/")
        if u and u not in urls:
            urls.append(u)
    for node in managed_runner_nodes():
        u = node["url"]
        if u and u not in urls:
            urls.append(u)
    return urls


def runner_cfg():
    """The PRIMARY runner (url, secret). Kept for callers that address one
    runner; placement across the pool goes through _runner_http."""
    pool = runner_pool()
    url = pool[0] if pool else ""
    secret = _secret_for_runner(url) if url else os.getenv("RUNNER_SERVICE_SECRET", "").strip()
    return url, secret


def embedded_mode() -> bool:
    """Single-service deployment → the runner runs in-process."""
    return not runner_pool()


def public_base_url() -> str:
    """Where /live/* is publicly reachable.
    Embedded → this main service. Remote → the runner service."""
    pool = runner_pool()
    if pool:
        return pool[0]
    base = (
        os.getenv("SITE_BASE_URL", "").strip()
        or os.getenv("PUBLIC_BASE_URL", "").strip()
        or os.getenv("RENDER_EXTERNAL_URL", "").strip()
    )
    if not base:  # local dev fallback
        base = "http://127.0.0.1:{}".format(os.getenv("PORT", "8000"))
    return base.rstrip("/")


_tc = None


def _embedded_client():
    """In-process ASGI client bound to the runner app. Created lazily so the
    app module itself can decide activation order (secret generation first)."""
    global _tc
    if _tc is None:
        from fastapi.testclient import TestClient
        import runner.app as rapp
        _tc = TestClient(rapp.app, raise_server_exceptions=False)
    return _tc


# ---------------------------------------------------------------------------
# WORKER REGISTRY  —  cached health, least-loaded placement
# ---------------------------------------------------------------------------
# The pool is the union of environment-configured URLs and DB-managed runner
# nodes. Registry/health caches keep placement fast while
# allowing an owner to add capacity without redeploying the main site.
_HEALTH_TTL_S = 20  # hard-coded inside code (no env var needed) — faster refresh for 6-runner fleet
_health_cache = {}          # url -> {"at": ts, "free": int, "load": float, "online": bool}


def _probe_worker(url: str) -> dict:
    """Ask one worker how loaded it is. Never raises."""
    try:
        r = requests.get(url + "/health", timeout=3)
        if r.status_code != 200:
            return {"online": False, "free": 0, "load": 1.0}
        d = r.json()
        return {
            "online": True,
            "free": int(d.get("free", 0)),
            "load": float(d.get("load", 1.0)),
            "jobs": int(d.get("jobs", 0)),
            "capacity": int(d.get("capacity", 0)),
            "mem_mb": float(d.get("mem_mb", 0.0)),
            # BUG THIS FIXES: the three MEMORY fields were dropped here, so the
            # admin overview — which sums safe_mb/total_mb across the pool to
            # decide whether to show the capacity panel at all — always summed
            # zero and hid the panel. It only ever looked right in embedded
            # mode, where the overview bypasses this cache and calls /health
            # directly. On the real two-service deployment the whole memory
            # section was silently blank.
            "safe_mb": float(d.get("safe_mb", 0.0)),
            "total_mb": float(d.get("total_mb", 0.0)),
            "free_mb": float(d.get("free_mb", 0.0)),
            "full": bool(d.get("full", False)),
        }
    except Exception:
        # Offline, asleep, or too old to expose /health load fields.
        return {"online": False, "free": 0, "load": 1.0,
                "jobs": 0, "mem_mb": 0.0, "safe_mb": 0.0,
                "total_mb": 0.0, "free_mb": 0.0, "full": False}


def worker_health(refresh: bool = False, max_age_s: float = None) -> dict:
    """Cached health for every worker in the pool — now parallel.

    max_age_s lets a caller say how stale an answer it can live with, instead
    of the all-or-nothing choice between the 45s placement cache and a forced
    round-trip to every worker. The admin overview polls every 10s and used
    refresh=True, so it re-probed the whole pool on every tick — the console
    generating load on the box it is watching. It now accepts a few seconds of
    staleness, which is invisible at a 10s refresh.

    Parallel probing: the old loop probed runners sequentially (6 × 3s timeout
    = 18s cold start) and blocked the Telegram callback thread the whole time,
    making inline buttons look dead. We now probe needed runners concurrently.
    """
    import time
    import concurrent.futures as _cf
    now = time.time()
    ttl = _HEALTH_TTL_S if max_age_s is None else max_age_s
    pool = runner_pool()
    out = {}
    to_probe = []
    for url in pool:
        cached = _health_cache.get(url)
        if not refresh and cached and now - cached["at"] < ttl:
            out[url] = cached
        else:
            to_probe.append(url)
    if to_probe:
        # probe in parallel; timeout per-worker is 3s so whole fleet ~3s not N*3
        with _cf.ThreadPoolExecutor(max_workers=max(1, len(to_probe))) as ex:
            futs = {ex.submit(_probe_worker, u): u for u in to_probe}
            for fut in _cf.as_completed(futs):
                u = futs[fut]
                try:
                    info = fut.result()
                except Exception:
                    info = {"online": False, "free": 0, "load": 1.0, "jobs": 0, "mem_mb": 0.0, "safe_mb": 0.0, "total_mb": 0.0, "free_mb": 0.0, "full": False}
                info["at"] = now
                _health_cache[u] = info
                out[u] = info
    return out


def _placement_order(user_id: int | None = None) -> list:
    """Workers to try for a NEW job, least-loaded first (now by free MB).

    Queen pins: a queen's exclusive runner is first for that queen, last for everyone else.
    Falls back to pool order when nothing has been probed yet, so a cold start
    still places jobs instead of refusing them.
    """
    pool = runner_pool()
    if len(pool) < 2:
        return pool
    health = worker_health()
    exclusive_map = list_exclusive_runners()
    # own pin (if any) should rank before everything
    own_pin_url = None
    if user_id is not None:
        try:
            pin = get_queen_pin(int(user_id))
            if pin:
                own_pin_url = str(pin.get("runner_url") or "").rstrip("/")
        except Exception:
            pass
    def key(u):
        h = health.get(u) or {}
        online = bool(h.get("online"))
        # Use free_mb when available, fallback to free slots * 30 (health may be 0 when older mock lacks field)
        free_mb = h.get("free_mb")
        if free_mb is None or (free_mb == 0 and h.get("free") is not None):
            # if probe returned 0 free_mb but has free slots, derive estimate (old runners / test mocks)
            try:
                free_mb = float(h.get("free", 0)) * 30.0
            except Exception:
                free_mb = 0.0
        is_own = (u == own_pin_url) if own_pin_url else False
        is_exclusive_other = (u in exclusive_map and exclusive_map[u] != (int(user_id) if user_id is not None else -1))
        if is_own:
            cat = 0  # queen's own exclusive — always first
        elif not online:
            cat = 3  # offline last
        elif is_exclusive_other:
            cat = 2  # exclusive for someone else — avoid
        else:
            cat = 1  # normal
        # within same category, most free_mb first
        return (cat, -float(free_mb), pool.index(u))
    return sorted(pool, key=key)


# A single dashboard refresh calls four routes that each want the fleet's job
# list. Unmemoised on a 3-worker pool that was 9 identical HTTP round-trips per
# refresh, every 10s — the monitoring console becoming a real source of load on
# the box it exists to watch. The window is deliberately shorter than the poll
# interval, so consecutive refreshes still see fresh data; it only collapses
# the burst WITHIN one refresh.
FLEET_CACHE_MS = int(os.getenv("FLEET_CACHE_MS", "3000"))
_fleet_cache = {"at": 0.0, "jobs": None}


def _has_embedded_assignments():
    try:
        from database import get_db_connection
        conn = get_db_connection()
        try:
            row = conn.execute("SELECT 1 FROM jobs WHERE worker_url='embedded' AND runner_job_id IS NOT NULL LIMIT 1").fetchone()
            return bool(row)
        finally:
            conn.close()
    except Exception:
        return False


def fleet_jobs(refresh: bool = False) -> dict:
    """Every live job on EVERY worker, keyed by runner job id.

    BUG THIS FIXES: `_runner_http("GET", "/internal/jobs")` with no worker=
    falls back to pool[:1], because that fallback was written for calls that
    address ONE job created before the worker_url column existed. But a
    fleet-wide READ has no single worker to address, so the admin console, the
    library aggregation and the abuse/limit checks all saw only worker #1.
    With two workers the dashboard reported half the running jobs as dead —
    exactly the failure mode the console exists to catch.

    Each entry is tagged with the worker that answered, so callers can show
    where a job actually lives. Best-effort per worker: one sleeping runner
    must not blank out the others.
    """
    import time
    now = time.time()
    if (not refresh and _fleet_cache["jobs"] is not None
            and (now - _fleet_cache["at"]) * 1000 < FLEET_CACHE_MS):
        return _fleet_cache["jobs"]

    out = {}
    pool = runner_pool()
    if not pool:                                   # embedded single service
        try:
            resp = _runner_http("GET", "/internal/jobs")
            for j in ((resp.json() or {}).get("jobs") or []):
                j["worker"] = "embedded"
                out[j.get("id")] = j
        except Exception as exc:
            logger.warning("fleet_jobs: embedded runner unreachable (%s)", exc)
        _fleet_cache.update(at=now, jobs=out)
        return out
    # Parallel fetch — 6 runners sequential at 20s each = 2min block; parallel ~20s max
    import concurrent.futures as _cf2
    def _fetch(base):
        try:
            resp = _runner_http("GET", "/internal/jobs", worker=base)
            if resp is None or resp.status_code != 200:
                return []
            return [(j.get("id"), {**j, "worker": base}) for j in ((resp.json() or {}).get("jobs") or [])]
        except Exception as exc:
            logger.warning("fleet_jobs: %s unreachable (%s)", base, exc)
            return []
    # Don't wait for offline runners forever: if health cache says offline, skip fast probe?
    # But health may be stale; instead fetch all in parallel with timeout via _runner_http's own 20s.
    with _cf2.ThreadPoolExecutor(max_workers=max(1, len(pool))) as ex:
        futs = {ex.submit(_fetch, base): base for base in pool}
        for fut in _cf2.as_completed(futs):
            for jid, j in fut.result():
                out[jid] = j
    if _has_embedded_assignments():
        try:
            resp = _runner_http("GET", "/internal/jobs", worker="embedded")
            if resp is not None and resp.status_code == 200:
                for j in ((resp.json() or {}).get("jobs") or []):
                    j["worker"] = "embedded"
                    out[j.get("id")] = j
        except Exception as exc:
            logger.warning("fleet_jobs: embedded assignments unreachable (%s)", exc)
    _fleet_cache.update(at=now, jobs=out)
    return out


# A Render free runner that is asleep or mid-deploy answers with a bare 502/503/504
# from Render's proxy: no JSON body, no X-Runner-Full header. That is "not up yet",
# not a rejection, so we wait and retry instead of failing the user's app.
_WAKE_RETRIES = int(os.getenv("RUNNER_WAKE_RETRIES", "2"))
_WAKE_WAIT_S = float(os.getenv("RUNNER_WAKE_WAIT_S", "4"))


def _is_owner_suspended(resp) -> bool:
    """Render's explicit owner-suspension page means this worker is gone, not waking."""
    body = getattr(resp, "text", "") or ""
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    text = str(body).lower()
    return "service suspended" in text or "suspended by its owner" in text


def _looks_like_wakeup(resp) -> bool:
    """True for a proxy-level 502/503/504 from a runner that is still booting.

    A real answer from our own runner carries a JSON "detail" (e.g. "Runner
    secret not configured.") or X-Runner-Full. Render's owner-suspension page
    is also a definitive response, not a temporary wake-up, and must not be
    retried as if the worker were merely starting."""
    if _is_owner_suspended(resp):
        return False
    if getattr(resp, "status_code", None) not in (502, 503, 504):
        return False
    try:
        if (resp.headers or {}).get("X-Runner-Full"):
            return False
    except Exception:
        pass
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("detail"):
            return False
    except Exception:
        pass
    return True


def _runner_http(method: str, path: str, json_body=None, worker: str = None, user_id: int | None = None):
    """Call the runner (embedded or remote) with the shared secret; map every
    transport failure to a clean HTTPException the frontend can display."""
    # Anything that is not a read CHANGES the fleet, so the memoised job list
    # is stale the instant it returns. Without this, stopping a job would keep
    # showing it as running for up to FLEET_CACHE_MS — a monitoring panel
    # contradicting an action the admin just took is worse than a slow one.
    if method.upper() != "GET":
        _fleet_cache["jobs"] = None

    if (embedded_mode() and not worker) or worker == "embedded":
        secret = os.getenv("RUNNER_SERVICE_SECRET", "").strip()
        if not secret:
            raise HTTPException(status_code=503, detail="Runner is not configured.")
        try:
            return _embedded_client().request(
                method, path, json=json_body,
                headers={"Authorization": "Bearer " + secret},
                timeout=130,
            )
        except HTTPException:
            raise
        except Exception as exc:  # in-process — a failure here means the runner itself blew up
            logger.exception("embedded runner call failed: %s %s", method, path)
            raise HTTPException(status_code=503, detail=f"Job engine error — please try again. ({type(exc).__name__})")

    pool = runner_pool()
    if worker and not pool:
        pool = [worker.rstrip("/")]
    if not pool:
        raise HTTPException(status_code=503, detail="Jobs are not configured. Add a runner in Admin or set RUNNER_SERVICE_URL.")

    def _call(base):
        runner_secret = _secret_for_runner(base)
        if not runner_secret:
            raise requests.ConnectionError("runner credential missing")
        return requests.request(
            method, base + path,
            json=json_body,
            headers={"Authorization": "Bearer " + runner_secret},
            timeout=20,
        )

    # Creating a job is the only PLACEMENT decision — every other call targets
    # a job that already lives on ONE specific worker and must go there.
    #
    # BUG THIS FIXES: `targets = pool[:1]` sent every follow-up call to the
    # FIRST worker regardless of where the job actually ran. With one worker
    # that is harmless; with two, a restart for a job on worker-B hit worker-A,
    # got "job not found", and the site reported a healthy bot as dead.
    # Callers now pass worker= from the jobs table.
    creating = method.upper() == "POST" and path == "/internal/jobs"
    if creating:
        # Least-loaded first (by free MB). Pass user_id so queen pins/exclusive are respected.
        # For queen heavy jobs, this picks the emptiest of the allowed runners (often 490 free).
        targets = _placement_order(user_id=user_id)
    elif worker:
        # Honour the recorded worker even if it has since been dropped from
        # the pool — the job is still there, and refusing to talk to it would
        # orphan a running bot.
        targets = [worker.rstrip("/")]
    else:
        # No recorded worker: a job created before this column existed. Those
        # all live on the primary.
        targets = pool[:1]

    # Auto-infer user_id from job name "u<id>-name" when caller didn't pass it.
    if creating and user_id is None and isinstance(json_body, dict):
        try:
            import re as _re
            nm = str(json_body.get("name") or "")
            m = _re.match(r"^u(\d+)-", nm)
            if m:
                user_id = int(m.group(1))
                # re-compute targets queen-aware (pin + exclusive + free_mb)
                targets = _placement_order(user_id=user_id)
        except Exception:
            pass
    last_exc = None
    last_full = None
    last_wake = None
    last_suspended = None
    for i, base in enumerate(targets):
        try:
            resp = _call(base)
            for _n in range(_WAKE_RETRIES):
                if not _looks_like_wakeup(resp):
                    break
                logger.info("runner %s is waking (HTTP %s), retry %d/%d",
                            base, resp.status_code, _n + 1, _WAKE_RETRIES)
                time.sleep(_WAKE_WAIT_S * (_n + 1))
                resp = _call(base)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            continue                     # asleep or unreachable — try the next
        if _is_owner_suspended(resp):
            # Render explicitly says the owner suspended this service. It has
            # no live job process to duplicate, so skip it during placement and
            # let recovery put missing jobs on a healthy runner.
            last_suspended = resp
            logger.warning("runner %s is suspended by its owner", base)
            if not creating and path.rstrip("/") == "/health":
                return resp  # recovery needs the page text to distinguish this
            continue
        if _looks_like_wakeup(resp):
            # Still booting after the retries: say so plainly, and let a
            # create try the next runner instead of reporting a rejected app.
            last_wake = resp
            logger.warning("runner %s still unavailable (HTTP %s)", base, resp.status_code)
            continue
        # 503 + X-Runner-Full means that container is at its RAM ceiling.
        # Roll to the next runner instead of telling the user the site is full.
        if creating and resp.status_code == 503 and resp.headers.get("X-Runner-Full"):
            last_full = resp
            logger.info("runner %s full, trying next of %d", base, len(targets))
            continue
        if creating and i:
            logger.info("job placed on overflow runner %s", base)
        if creating:
            # Stamp the winning worker onto the response so create_job() can
            # persist it. An attribute keeps requests.Response duck-typing
            # intact for every existing caller and test double.
            try:
                resp.placed_on = base
            except Exception:
                pass
        return resp

    if last_full is not None:
        return last_full                 # every runner full — report it honestly
    if last_wake is not None:
        raise HTTPException(status_code=503, detail="Waking up your RunSpace... this can take up to a minute on the free tier.")
    if last_suspended is not None:
        raise HTTPException(status_code=503,
                            detail="Runner is suspended by its owner; use another online runner.")
    if isinstance(last_exc, requests.Timeout):
        raise HTTPException(status_code=504, detail="Waking up your RunSpace... this can take up to a minute on the free tier.")
    raise HTTPException(status_code=503, detail="Waking up your RunSpace... this can take up to a minute on the free tier.")


def _job_web_fields(info: dict, worker: str = None) -> dict:
    """Translate a runner job view into frontend web fields (public URL etc.).

    In embedded mode the /live gateway is served by THIS main service, so the
    public URL is <site-base>/live/{slug}/; in remote mode it is served by the
    worker the job actually runs on.

    BUG THIS FIXES: this used runner_cfg()[0], i.e. always the PRIMARY worker.
    A job placed on worker-B was handed worker-A's public URL, so its live page
    404'd even though the bot was running fine.
    """
    slug = (info or {}).get("web_slug")
    if embedded_mode() or worker == "embedded":
        base = (
            os.getenv("SITE_BASE_URL", "").strip()
            or os.getenv("PUBLIC_BASE_URL", "").strip()
            or os.getenv("RENDER_EXTERNAL_URL", "").strip()
            or "http://127.0.0.1:{}".format(os.getenv("PORT", "8000"))
        ).rstrip("/")
    else:
        base = (worker or "").rstrip("/") or runner_cfg()[0]
    if not slug or not base:
        return {}
    out = {
        "web": bool(info.get("web")),
        "web_public": bool(info.get("web_public", True)),
        "web_url": f"{base}/live/{slug}/",
    }
    key = info.get("access_key")
    if not out["web_public"] and key:
        out["web_private_url"] = out["web_url"] + "?key=" + key
    return out
