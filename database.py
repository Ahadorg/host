"""
database.py — Dual-dialect database layer (SQLite + PostgreSQL).

Why this exists
---------------
The app was born on SQLite. To make data *permanently* persistent we now also
support PostgreSQL (e.g. Supabase free tier). Set the env var below to choose:

    DATABASE_URL=postgresql://user:pass@host:5432/dbname   -> PostgreSQL
    (unset / sqlite) + DB_PATH=.../database.db              -> SQLite  (default)

Both code paths expose the *same* Python API so app.py never has to know which
engine it is talking to:

    from database import get_db_connection, init_db, IntegrityError

    conn = get_db_connection()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (1,)).fetchone()
    print(row["username"])          # dict-style access works on both engines
    conn.execute("INSERT INTO ... VALUES (?, ?)", (a, b))
    new_id = cursor.lastrowid       # works on PG too (via RETURNING id)
    conn.commit(); conn.close()

Dialect differences handled transparently:
    * placeholder style          ?  ->  %s
    * lastrowid on INSERT        implemented via "... RETURNING id" on PG
    * AUTOINCREMENT              ->  SERIAL
    * case-insensitive columns   COLLATE NOCASE  ->  CITEXT
    * upsert                     INSERT OR REPLACE  ->  INSERT ... ON CONFLICT
    * connection lifecycle       PG uses a thread-safe connection pool
"""

from __future__ import annotations

import logging
import os
import re as _re
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger("codenest-db")

# ---------------------------------------------------------------------------
# Dialect selection
# ---------------------------------------------------------------------------
_DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

if _DATABASE_URL.startswith("postgres://") or _DATABASE_URL.startswith("postgresql://"):
    DIALECT = "postgres"
else:
    DIALECT = "sqlite"

# SQLite on-disk path (only used when DIALECT == "sqlite")
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "database.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# SSL mode for PostgreSQL connections (Supabase & most managed Postgres need SSL).
# Users can force a value with PG_SSLMODE; we default to "prefer" so it works on
# managed (SSL) hosts as well as a local no-SSL dev server.
PG_SSLMODE = os.getenv("PG_SSLMODE", "prefer")

# ---------------------------------------------------------------------------
# DATABASE_URL sanity check
# ---------------------------------------------------------------------------
# A malformed DATABASE_URL makes psycopg2 fail deep inside libpq with a cryptic
# DNS-style error ("could not translate host name \"<garbage>\" to address").
# The most common cause: the password contains raw special characters ('#',
# '@', spaces) that were not percent-encoded, so the URL parser glues password
# fragments onto the hostname. Catch that HERE, at startup, and fail with an
# actionable message instead of a 50-line stack trace.

def _safe_ident(name: str) -> str:
    """SQL identifier allowlist — letters, digits, underscore only."""
    name = (name or "").strip()
    if not name or not all(c.isalnum() or c == "_" for c in name):
        raise ValueError(f"invalid SQL identifier: {name!r}")
    return name


def _validate_database_url(url: str) -> None:
    from urllib.parse import urlparse

    problems: list[str] = []

    # 1) Accidental whitespace/newlines from copy-pasting into dashboards.
    if any(ch.isspace() for ch in url):
        problems.append("contains whitespace (accidental space/newline while copy-pasting?)")

    # 2) A raw '#' truncates the URL at the fragment marker and silently eats
    #    the rest of the password. In a valid URL it only appears as %23.
    if "#" in url:
        problems.append("raw '#' found in the URL — encode it as %23 (password character)")

    # 3) More than one '@' means the password holds an unencoded '@'.
    if url.split("://", 1)[-1].count("@") > 1:
        problems.append("more than one '@' found — encode '@' inside the password as %40")

    # 4) Structural checks: scheme, hostname, port, password.
    try:
        parsed = urlparse(url)
        host = parsed.hostname            # may raise ValueError on bad port
        _ = parsed.port
        if not host:
            problems.append("no hostname found (expected e.g. 'xyz.pooler.supabase.com')")
        if parsed.password is None:
            problems.append("no password found (expected form: postgresql://user:PASSWORD@host:5432/db)")
    except ValueError as exc:
        problems.append(f"cannot be parsed ({exc})")

    if problems:
        bullet_list = "\n".join(f"    * {p}" for p in problems)
        raise RuntimeError(
            "\n\n"
            "==============================================================\n"
            "  DATABASE_URL looks malformed — refusing to start.\n"
            "--------------------------------------------------------------\n"
            f"{bullet_list}\n\n"
            "  Fix: copy the exact connection string from your provider\n"
            "  (Supabase -> Project Settings -> Database -> Session pooler)\n"
            "  and percent-encode special characters in the PASSWORD:\n"
            "      #  ->  %23        @  ->  %40        space  ->  %20\n"
            "  (পাসওয়ার্ডে #, @ বা স্পেস থাকলে অবশ্যই encode করতে হবে।)\n"
            "  Example:\n"
            "      postgresql://postgres.REF:ENCODED_PASSWORD@HOST:5432/postgres\n"
            "==============================================================\n"
        )

if DIALECT == "postgres":
    _validate_database_url(_DATABASE_URL)

logger.info("Database dialect: %s", DIALECT)

# ---------------------------------------------------------------------------
# Lazy driver import + exception aliases (so app.py imports a single name)
# ---------------------------------------------------------------------------
_psycopg2 = None
_pool: object | None = None
_pool_lock = threading.Lock()


def _load_psycopg2():
    """Import psycopg2 lazily so the SQLite-only path never requires it."""
    global _psycopg2
    if _psycopg2 is None:
        import psycopg2  # type: ignore
        from psycopg2.extras import RealDictCursor  # type: ignore
        from psycopg2 import pool as _pg_pool  # type: ignore
        from psycopg2 import errors as _pg_errors  # type: ignore
        _psycopg2 = {
            "connect": psycopg2.connect,
            "RealDictCursor": RealDictCursor,
            "pool": _pg_pool,
            "errors": _pg_errors,
            "IntegrityError": psycopg2.IntegrityError,
            "OperationalError": psycopg2.OperationalError,
        }
    return _psycopg2


# Exception aliases re-exported for app.py
if DIALECT == "postgres":
    _drv = _load_psycopg2()
    IntegrityError = _drv["IntegrityError"]
    OperationalError = _drv["OperationalError"]
else:
    IntegrityError = sqlite3.IntegrityError
    OperationalError = sqlite3.OperationalError


# ---------------------------------------------------------------------------
# SQL translation helpers
# ---------------------------------------------------------------------------
def _translate_sql(sql: str) -> str:
    """Translate a qmark-style (? placeholders) statement to the active dialect.

    PostgreSQL's psycopg2 driver uses %s placeholders. Literal '%' is not used
    anywhere in the app's SQL (no LIKE with %), so a straight swap is safe —
    EXCEPT inside `--` comments, where a plain English "?" ("is there a newer
    version?") used to become `%s` and then crash boot with
    `IndexError: tuple index out of range` the moment init_db ran CREATE TABLE
    jobs against Postgres. Comments keep their text; only real bind slots change.
    """
    if DIALECT != "postgres":
        return sql
    # Protect `-- …` comment tails so a rhetorical "?" cannot become a bind slot.
    parts = []
    for line in sql.splitlines(keepends=True):
        if "--" in line:
            code, dash, comment = line.partition("--")
            parts.append(code.replace("?", "%s") + dash + comment)
        else:
            parts.append(line.replace("?", "%s"))
    sql = "".join(parts)
    # SQLite's upsert spelling. The module docstring has always claimed this
    # was translated; it never was, and Postgres answers
    #   syntax error at or near "OR"
    # so runner/terminal.py's home-snapshot write failed on every call. It is
    # inside a bare try/except, so the failure was invisible.
    #
    # ON CONFLICT needs the conflicting column, which the statement does not
    # state. Every INSERT OR REPLACE in this app targets a table whose first
    # inserted column carries a single-column PRIMARY KEY or UNIQUE constraint
    # — the two things Postgres will accept as a conflict target. That is
    # asserted against the live schema in tests/test_pg_returning_id.py rather
    # than assumed, so a future upsert on a table shaped differently fails a
    # test here instead of at runtime on the deployed site.
    m = _re.match(
        r"(\s*)INSERT\s+OR\s+REPLACE\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)(.*)",
        sql, _re.I | _re.S)
    if m:
        lead, table, cols, rest = m.groups()
        names = [c.strip() for c in cols.split(",") if c.strip()]
        if names:
            key = names[0]
            updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in names[1:])
            sql = f"{lead}INSERT INTO {table} ({', '.join(names)}){rest}"
            sql = sql.rstrip().rstrip(";")
            sql += (f" ON CONFLICT ({key}) DO UPDATE SET {updates}"
                    if updates else f" ON CONFLICT ({key}) DO NOTHING")
    # INSERT OR IGNORE has the same problem, with a simpler answer.
    sql = _re.sub(r"^(\s*)INSERT\s+OR\s+IGNORE\s+INTO", r"\1INSERT INTO",
                  sql, flags=_re.I)
    return sql


def _translate_ddl(ddl: str) -> str:
    """Translate SQLite-specific CREATE TABLE syntax to PostgreSQL.

    Also neutralises `?` inside `--` comments. DDL goes through the same
    execute() path as queries, so a comment question mark would otherwise be
    rewritten to `%s` and crash init_db on Postgres with
    `IndexError: tuple index out of range` — that is what took the live site
    down after the repo_commit migration shipped.
    """
    if DIALECT != "postgres":
        return ddl
    # Case-insensitive uniqueness mirrors SQLite's "COLLATE NOCASE".
    ddl = ddl.replace("TEXT NOT NULL UNIQUE COLLATE NOCASE", "CITEXT NOT NULL UNIQUE")
    # Auto-increment integer PK -> SERIAL.
    ddl = ddl.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    fixed = []
    for line in ddl.splitlines(keepends=True):
        if "--" in line:
            code, dash, comment = line.partition("--")
            fixed.append(code + dash + comment.replace("?", "."))
        else:
            fixed.append(line)
    return "".join(fixed)


# ---------------------------------------------------------------------------
# Cursor / Connection wrappers — present one API for both engines
# ---------------------------------------------------------------------------
# Tables with NO surrogate `id` column — they are keyed by the id of whatever
# they hang off (user_id / job_id). Appending "RETURNING id" to an insert on
# these is a guaranteed UndefinedColumn error on Postgres.
#
# Derived from the schema at import time rather than hand-listed: a future
# table without an id would otherwise reintroduce exactly this bug, and the
# failure only shows up on Postgres, i.e. only in production.
_NO_ID_TABLES_CACHE = None


def _tables_without_id() -> set:
    # _SCHEMA_TABLES is defined further down this module, so this resolves on
    # first use rather than at import time.
    global _NO_ID_TABLES_CACHE
    if _NO_ID_TABLES_CACHE is None:
        out = set()
        for m in _re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n    \)",
                              "\n".join(_SCHEMA_TABLES), _re.S):
            if not _re.search(r"^\s*id\s", m.group(2), _re.M):
                out.add(m.group(1).lower())
        _NO_ID_TABLES_CACHE = out
    return _NO_ID_TABLES_CACHE


class _Cursor:
    """Wraps a sqlite3 or psycopg2 cursor.

    Notable behaviour for PostgreSQL:
      * qmark (?) placeholders are rewritten to %s.
      * For INSERT statements we append " RETURNING id" and read the new id so
        that `cursor.lastrowid` behaves like SQLite's.
    """

    def __init__(self, cur):
        self._cur = cur
        self._returning_id = None

    @staticmethod
    def _insert_target(sql: str) -> str:
        """The table an INSERT writes to, lowercased ('' if unparseable)."""
        m = _re.match(r"\s*INSERT\s+(?:OR\s+\w+\s+)?INTO\s+[\"']?([A-Za-z_][A-Za-z0-9_]*)",
                      sql, _re.I)
        return m.group(1).lower() if m else ""

    def execute(self, sql, params=()):
        sql_t = _translate_sql(sql)
        if DIALECT == "postgres":
            head = sql_t.lstrip()[:6].upper()
            if head == "INSERT" and self._insert_target(sql_t) not in _tables_without_id():
                # Append RETURNING id (strip a trailing semicolon if present).
                #
                # BUG THIS FIXES: this used to fire for EVERY insert, including
                # the three tables that have no id column at all — they are
                # keyed by user_id or job_id. Postgres answered
                #   psycopg2.errors.UndefinedColumn: column "id" does not exist
                # and the whole request 500'd. So every write to term_homes,
                # job_data_snapshots and telegram_link_codes failed in
                # production while passing locally, because SQLite never takes
                # this branch. The workspace-snapshot and terminal-home
                # features had therefore never once worked on the live site.
                body = sql_t.rstrip().rstrip(";")
                self._cur.execute(body + " RETURNING id", tuple(params))
                row = self._cur.fetchone()
                self._returning_id = row["id"] if row else None
                return self
            if head == "INSERT":
                # No id to return. lastrowid stays None, which is correct:
                # these tables are addressed by the key the caller already has.
                self._returning_id = None
        try:
            self._cur.execute(sql_t, tuple(params))
        except Exception as exc:
            # Every query in the app passes through HERE — log failures so a
            # leftover SQLite-only construct (or any DB error) is visible in
            # logs immediately instead of hiding behind a 500. Params are
            # intentionally NOT logged: they can carry code secrets.
            logger.error(
                "DB query failed [%s]: %s | SQL: %s",
                DIALECT, type(exc).__name__, " ".join(sql_t.split())[:500],
                exc_info=False,
            )
            raise
        return self

    def executemany(self, sql, seq_of_params):
        self._cur.executemany(_translate_sql(sql), seq_of_params)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def lastrowid(self):
        if DIALECT == "postgres":
            return self._returning_id
        return self._cur.lastrowid

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def description(self):
        return self._cur.description

    def close(self):
        self._cur.close()


class _Connection:
    """Wraps a sqlite3 or psycopg2 connection with the unified cursor API."""

    def __init__(self, conn):
        self._conn = conn
        self._closed = False

    def execute(self, sql, params=()):
        cur = _Cursor(self._conn.cursor())
        cur.execute(sql, params)
        return cur

    def cursor(self):
        return _Cursor(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if self._closed:
            return
        self._closed = True
        if DIALECT == "postgres":
            try:
                self._conn.rollback()  # clear any open txn before returning to pool
            except Exception:
                pass
            _return_to_pool(self._conn)
        else:
            try:
                self._conn.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def __del__(self):
        # Safety net: if caller forgot close() or leaked via exception, return
        # the raw connection to the pool on GC instead of holding it forever.
        # This is what finally stopped "pool exhausted" from becoming permanent.
        if not self._closed and DIALECT == "postgres" and getattr(self, "_conn", None) is not None:
            try:
                self.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# PostgreSQL connection pool
# ---------------------------------------------------------------------------
#
# WHY THE EXTRA MACHINERY BELOW (read: the "site hangs after a while" fix):
#   Render's free instances sleep after ~15 min idle, and Supabase's pooler —
#   plus every NAT between them — silently drops idle TCP sessions. A pooled
#   connection can therefore be dead WITHOUT psycopg2 knowing, and the first
#   query on it hangs for minutes (default TCP timeouts are ~2 HOURS).
#   Three defences, layered:
#     1. TCP keepalives + connect_timeout on every new connection, so a dead
#        socket is detected in ~80s instead of ~2h, and connects cap at 10s.
#     2. After a long idle gap the instance almost surely slept: rebuild the
#        whole pool BEFORE handing anything out — fresh connects are ~200ms.
#     3. Probe every checkout with SELECT 1; discard-and-retry dead conns.
_POOL_IDLE_RESET_S = 90.0   # more silence than this => do not trust the pool
_last_db_activity = 0.0     # monotonic timestamp of the last healthy checkout


def _get_pool():
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            drv = _load_psycopg2()
            # ThreadedConnectionPool is required for the webhook's 3 workers
            # + uvicorn threads. SimpleConnectionPool is NOT thread-safe —
            # that's how the live site hit "connection pool exhausted" after a
            # burst: two threads borrowed the same slot and never returned it.
            try:
                PoolCls = drv["pool"].ThreadedConnectionPool
            except AttributeError:
                PoolCls = drv["pool"].SimpleConnectionPool
            # sslmode can also live inside DATABASE_URL; only inject a default
            # when the user hasn't already specified one.
            dsn = _DATABASE_URL
            connect_kwargs = {
                # libpq knobs (psycopg2 passes these straight through)
                "connect_timeout": 10,
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 5,
            }
            if "application_name" not in dsn:
                connect_kwargs["application_name"] = "codenest"
            if "sslmode" not in _DATABASE_URL and PG_SSLMODE:
                connect_kwargs["sslmode"] = PG_SSLMODE
            # Supabase free pooler allows many; 8 was too small for 3 webhook
            # workers + web threads + recovery + broadcast + health probes.
            # 20 still fits Render's 512 MB.
            logger.info("Creating PostgreSQL connection pool (maxconn=20, Threaded)")
            _pool = PoolCls(
                minconn=2,
                maxconn=20,
                dsn=dsn,
                cursor_factory=drv["RealDictCursor"],
                **connect_kwargs,
            )
    return _pool


def _reset_pool():
    """Kill every pooled connection; the next _get_pool() builds a fresh pool."""
    global _pool
    with _pool_lock:
        old, _pool = _pool, None
    if old is not None:
        try:
            old.closeall()
        except Exception:
            pass


def _checkout_pg():
    """Take a LIVE connection out of the pool (see defences at the top)."""
    global _last_db_activity
    now = time.monotonic()
    if _last_db_activity and (now - _last_db_activity) > _POOL_IDLE_RESET_S:
        # Defence 2 — we almost certainly slept; don't even bother probing
        # connections that were frozen alongside the process.
        logger.info("PostgreSQL pool idle %.0fs — rebuilding (post-sleep safety)",
                    now - _last_db_activity)
        _reset_pool()
    pool = _get_pool()
    # Retry on PoolError (exhausted) with brief backoff — a burst of 3 webhook
    # workers + web threads can transiently exceed the 8-slot pool. Previously
    # this raised immediately and every caller spammed "connection pool exhausted"
    # every 0.8s until the site looked dead.
    last_pool_err = None
    for attempt in range(6):
        try:
            for _ in range(3):
                try:
                    raw = pool.getconn()
                except Exception as exc:
                    # PoolError or any pool exhaustion signal
                    last_pool_err = exc
                    raise
                try:
                    # Defence 3 — cheap liveness probe (round trip is ~ms, a dead
                    # conn costs a hang if we skip this)
                    cur = raw.cursor()
                    cur.execute("SELECT 1")
                    cur.close()
                    _last_db_activity = time.monotonic()
                    return raw
                except Exception as exc:
                    logger.warning("Discarding dead pooled PostgreSQL connection: %s",
                                   type(exc).__name__)
                    try:
                        pool.putconn(raw, close=True)   # close & drop from the pool
                    except Exception:
                        try:
                            raw.close()
                        except Exception:
                            pass
                    # try next pooled conn
                    continue
            # Everything we were offered was dead — nuke the pool and take a fresh one.
            _reset_pool()
            pool = _get_pool()
            raw = pool.getconn()
            _last_db_activity = time.monotonic()
            return raw
        except Exception as exc:
            # Handle PoolError / exhaustion with backoff, not instant spam
            err_name = type(exc).__name__
            if "PoolError" in err_name or "exhausted" in str(exc).lower():
                last_pool_err = exc
                # Log only first time, then backoff
                if attempt == 0:
                    logger.warning("PostgreSQL pool exhausted (attempt %d/6) — backing off 0.2s", attempt + 1)
                else:
                    logger.debug("PostgreSQL pool still exhausted (attempt %d/6)", attempt + 1)
                time.sleep(0.15 + 0.05 * attempt)
                # Also try to reset if still failing after a few attempts
                if attempt >= 3:
                    try:
                        _reset_pool()
                        pool = _get_pool()
                    except Exception:
                        pass
                continue
            raise
    # Still failed after retries — raise the last PoolError with context
    if last_pool_err is not None:
        raise last_pool_err
    raise RuntimeError("PostgreSQL pool checkout failed after retries")


def _return_to_pool(conn):
    try:
        _get_pool().putconn(conn)
    except Exception:
        # If returning fails (e.g. pool closed) make sure we don't leak.
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public: get a connection
# ---------------------------------------------------------------------------
def get_db_connection() -> _Connection:
    if DIALECT == "postgres":
        return _Connection(_checkout_pg())

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return _Connection(conn)


# ---------------------------------------------------------------------------
# Schema definitions
# ---------------------------------------------------------------------------
_SCHEMA_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        email TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password TEXT NOT NULL,
        otp TEXT,
        otp_created_at TEXT,
        is_verified INTEGER NOT NULL DEFAULT 0,
        reset_otp TEXT,
        reset_otp_created_at TEXT,
        reset_verified INTEGER NOT NULL DEFAULT 0,
        password_changed_at TEXT,
        otp_attempts INTEGER NOT NULL DEFAULT 0,
        reset_otp_attempts INTEGER NOT NULL DEFAULT 0,
        role TEXT NOT NULL DEFAULT 'user',
        phone TEXT,
        custom_code TEXT,
        links TEXT,
        telegram_id INTEGER UNIQUE,
        telegram_name TEXT,
        fingerprint TEXT,
        last_ip TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        token TEXT NOT NULL UNIQUE,
        device_info TEXT,
        ip_address TEXT,
        fingerprint TEXT,
        created_at TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        expires_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_2fa (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL UNIQUE,
        secret TEXT,
        is_enabled INTEGER NOT NULL DEFAULT 0,
        backup_codes TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS login_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        ip_address TEXT,
        device_info TEXT,
        location TEXT,
        success INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_preferences (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL UNIQUE,
        theme TEXT DEFAULT 'dark',
        language TEXT DEFAULT 'en',
        timezone TEXT DEFAULT 'UTC',
        notifications_enabled INTEGER DEFAULT 1,
        email_notifications INTEGER DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        language TEXT NOT NULL,
        code TEXT NOT NULL,
        runner_job_id TEXT,
        -- WHICH worker this job physically runs on. Without it, a follow-up
        -- call (restart/stop/logs) went to whichever worker happened to be
        -- first in the pool, so with 2+ workers the site would report a
        -- perfectly healthy bot as dead. NULL = the single-worker default.
        worker_url TEXT,
        desired_state TEXT NOT NULL DEFAULT 'running',
        env TEXT,
        telegram_bot_detected INTEGER NOT NULL DEFAULT 0,
        telegram_bot_username TEXT,
        telegram_bot_id TEXT,
        telegram_token_fingerprint TEXT,
        telegram_check_status TEXT,
        telegram_verified_at TEXT,
        telegram_framework TEXT,
        telegram_update_mode TEXT,
        telegram_token_source TEXT,
        -- Where a deployed app came from, so "is there a newer version" is a
        -- comparison instead of an archaeology project. repo_commit is the SHA
        -- the runner actually cloned; repo_entry is the file inside the repo
        -- that runs, because one repo can hold more than one runnable thing.
        repo_url TEXT,
        repo_entry TEXT,
        repo_commit TEXT,
        auto_deploy INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS activity_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        details TEXT,
        ip_address TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    """
    -- Admin panel: every destructive action lands here (who did what, when).
    CREATE TABLE IF NOT EXISTS admin_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        target TEXT,
        details TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    -- Operational runner/job actions, separate from user analytics and deploy
    -- metrics. Stores only redacted reasons/identifiers, never source or env.
    CREATE TABLE IF NOT EXISTS runner_action_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        runner_url TEXT NOT NULL,
        job_id INTEGER,
        job_name TEXT,
        actor TEXT NOT NULL DEFAULT 'system',
        action TEXT NOT NULL,
        details TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    -- Telegram-level ban: blocks a raw Telegram user id from the bot
    -- entirely (before /link, before any account exists). Nothing like
    -- this exists on the website — is_suspended there always requires an
    -- account row to hang the flag off of, so it can't stop someone who
    -- has never linked/signed up from spamming the bot itself.
    CREATE TABLE IF NOT EXISTS banned_telegram_ids (
        telegram_id INTEGER PRIMARY KEY,
        banned_by INTEGER,
        reason TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    -- Public "Report abuse" inbox for live URLs / published pages.
    CREATE TABLE IF NOT EXISTS abuse_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL,
        reason TEXT,
        ip TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS snippets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        language TEXT,
        content TEXT NOT NULL,
        share_token TEXT UNIQUE,
        is_public INTEGER DEFAULT 0,
        views INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    """
    -- Termux home snapshots: tar+gzip+base64 of the user's $HOME. Restored
    -- on first spawn after a deploy/restart so nano files, bash history,
    -- pip --user packages etc. survive container restarts.
    CREATE TABLE IF NOT EXISTS term_homes (
        user_id INTEGER PRIMARY KEY,
        tarball_b64 TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    """
    -- RunSpace job workspace snapshots: tar+gzip+base64 of the DATA files a
    -- bot writes into its own directory (database.db, session.json, data/…).
    --
    -- Why this exists: the runner keeps each job in JOBS_DATA_DIR/<runner_id>,
    -- which survives Stop/Restart and code edits, but on Render's free tier
    -- the container filesystem is REBUILT on every deploy. A referral bot's
    -- points/history therefore vanished on redeploy unless a paid Persistent
    -- Disk was mounted. Snapshotting to Postgres (which IS durable) closes
    -- that gap on the free plan and doubles as the download source.
    --
    -- Keyed by the SITE job id (jobs.id), not the runner id: the runner id
    -- changes whenever a job is recreated, the site id does not.
    -- TELEGRAM ACCOUNT LINKING
    --
    -- A short-lived code the site issues and the bot redeems, so a Telegram
    -- chat can prove which CodeNest account it belongs to. Without this the
    -- bot has no identity at all: any stranger who finds the bot's username
    -- could deploy code onto the platform.
    --
    -- Keyed by user_id, so requesting a new code REPLACES the old one — an
    -- account can never have two live codes, and an abandoned code cannot be
    -- redeemed later by someone who saw it over a shoulder.
    CREATE TABLE IF NOT EXISTS telegram_link_codes (
        user_id INTEGER PRIMARY KEY,
        code TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_data_snapshots (
        job_id INTEGER PRIMARY KEY,
        tarball_b64 TEXT NOT NULL,
        file_count INTEGER NOT NULL DEFAULT 0,
        byte_size INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE CASCADE
    )
    """,
    """
    -- Admin-managed network/device blocks. A block prevents new signups and
    -- new jobs; it does not treat a shared IP as proof or lock existing users
    -- out of their data. Revocation is retained for the audit trail.
    CREATE TABLE IF NOT EXISTS admin_blocks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT NOT NULL,
        value TEXT NOT NULL,
        reason TEXT,
        created_by INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT,
        revoked_at TEXT,
        revoked_by INTEGER,
        FOREIGN KEY (created_by) REFERENCES users (id),
        FOREIGN KEY (revoked_by) REFERENCES users (id)
    )
    """,
    """
    -- Dynamically managed runner services. The secret is stored as plain JSON
    -- (see services/secrets_store.py); the admin API never returns it after
    -- registration. The column keeps its old name so no schema migration is
    -- needed for a change in how the value is encoded.
    CREATE TABLE IF NOT EXISTS runner_nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL,
        url TEXT NOT NULL UNIQUE,
        encrypted_secret TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_by INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (created_by) REFERENCES users (id)
    )
    """,
    """
    -- Immutable source revisions. Environment secrets are deliberately not
    -- duplicated here; rollback reuses the job's current stored env.
    CREATE TABLE IF NOT EXISTS bot_revisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        version INTEGER NOT NULL,
        action TEXT NOT NULL DEFAULT 'deploy',
        language TEXT NOT NULL,
        code TEXT NOT NULL,
        status TEXT NOT NULL,
        error TEXT,
        created_at TEXT NOT NULL,
        promoted_at TEXT,
        FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE CASCADE,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
        UNIQUE (job_id, version)
    )
    """,
    """
    -- Short-lived proof that an authenticated user verified a BotFather token
    -- before creating a bot. Only a SHA-256 digest is stored, never the token.
    CREATE TABLE IF NOT EXISTS telegram_token_verifications (
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        token_hash TEXT NOT NULL,
        bot_username TEXT NOT NULL,
        bot_id TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consumed_at TEXT,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    """
    -- Durable webhook delivery queue. A payload is held only while its handler
    -- is pending; it is cleared on completion, leaving the ID/timestamps for
    -- retry de-duplication without retaining message text.
    CREATE TABLE IF NOT EXISTS telegram_webhook_updates (
        update_id BIGINT PRIMARY KEY,
        claimed_at BIGINT NOT NULL,
        completed_at BIGINT,
        payload TEXT
    )
    """,
    """
    -- Immutable deployment history for the admin Telegram-bot view. This says
    -- who ran/updated/restarted what without retaining a second copy of code
    -- or any bot token.
    CREATE TABLE IF NOT EXISTS job_deploy_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        job_id INTEGER,
        action TEXT NOT NULL,
        job_name TEXT NOT NULL,
        telegram_bot_detected INTEGER NOT NULL DEFAULT 0,
        telegram_bot_username TEXT,
        telegram_bot_id TEXT,
        telegram_check_status TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
        FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE SET NULL
    )
    """,
    """
    -- Durable bot activity. chat_id is retained even when no site account is
    -- linked, so the admin funnel includes the people who have not signed up.
    CREATE TABLE IF NOT EXISTS bot_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL,
        telegram_user_id TEXT,
        user_id INTEGER,
        display_name TEXT,
        event_type TEXT NOT NULL,
        command TEXT,
        payload TEXT,
        outcome TEXT NOT NULL DEFAULT 'ok',
        error TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE SET NULL
    )
    """,
    """
    -- Bot Store. One row = one complete bot product you can deploy in one tap.
    -- `source` separates the built-in products (mirrored from
    -- services/bot_templates on boot, so counts and ratings attach to them
    -- like any other listing) from community submissions, which stay `pending`
    -- until an owner approves them. `code` is always one complete, runnable
    -- Python file — never a fragment and never a platform-specific dialect.
    CREATE TABLE IF NOT EXISTS store_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT NOT NULL UNIQUE,
        source TEXT NOT NULL DEFAULT 'built-in',
        builtin_id TEXT,
        title TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL DEFAULT 'Uncategorised',
        tags TEXT NOT NULL DEFAULT '',
        language TEXT NOT NULL DEFAULT 'python',
        framework TEXT NOT NULL DEFAULT '',
        difficulty TEXT NOT NULL DEFAULT 'Intermediate',
        features TEXT NOT NULL DEFAULT '',
        setup_notes TEXT NOT NULL DEFAULT '',
        env_fields TEXT NOT NULL DEFAULT '[]',
        code TEXT NOT NULL DEFAULT '',
        code_hash TEXT NOT NULL DEFAULT '',
        version TEXT NOT NULL DEFAULT '1.0.0',
        status TEXT NOT NULL DEFAULT 'pending',
        review_note TEXT NOT NULL DEFAULT '',
        author_user_id INTEGER,
        author_name TEXT NOT NULL DEFAULT 'CodeNest',
        featured INTEGER NOT NULL DEFAULT 0,
        install_count INTEGER NOT NULL DEFAULT 0,
        rating_sum INTEGER NOT NULL DEFAULT 0,
        rating_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        published_at TEXT,
        FOREIGN KEY (author_user_id) REFERENCES users (id) ON DELETE SET NULL
    )
    """,
    """
    -- Every deploy that started from a store listing. Kept per install (not as
    -- a bare counter) so the owner console can show WHICH bot each person
    -- took, and so an abusive listing can be traced to its installs.
    CREATE TABLE IF NOT EXISTS store_installs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_slug TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        job_id INTEGER,
        version TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
        FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE SET NULL
    )
    """,
    """
    -- One rating per person per listing (the UNIQUE index is created in
    -- init_db because an added constraint cannot live on an added column).
    CREATE TABLE IF NOT EXISTS store_reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_slug TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        rating INTEGER NOT NULL DEFAULT 5,
        comment TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    """
    -- Saved-for-later listings. A favourite is not an install: it only pins
    -- the listing into the person's own library tab.
    CREATE TABLE IF NOT EXISTS store_favorites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_slug TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
    """
    -- Rich broadcast — resumable, per-recipient delivery with retry/backoff.
    -- Text is stored as safe Telegram HTML; optional inline URL buttons are
    -- JSON; media (photo/video/document) is optional. Each campaign has many
    -- recipient rows so a restart can continue from pending ones.
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
    """,
    """
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
    """,
    """
    -- Queen runner pin — a queen user can claim one runner exclusively.
    -- When exclusive=1, only that queen's jobs may be placed there; other
    -- users are routed to the remaining free runners (512MB exclusive).
    -- No env var needed; chosen from inside the bot/admin panel.
    CREATE TABLE IF NOT EXISTS queen_runner_pins (
        user_id INTEGER PRIMARY KEY,
        runner_url TEXT NOT NULL,
        exclusive INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    """,
]


def _column_exists(conn: _Connection, table: str, column: str) -> bool:
    """True if `column` exists on `table` (works on both engines)."""
    if DIALECT == "postgres":
        row = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = ? AND column_name = ?",
            (table, column),
        ).fetchone()
        return bool(row)
    rows = conn.execute(f"PRAGMA table_info({_safe_ident(table)})").fetchall()
    return any(r["name"] == column for r in rows)


def init_db():
    """Create all tables if missing. Safe to run on every startup."""
    conn = get_db_connection()
    try:
        if DIALECT == "postgres":
            # CITEXT gives us case-insensitive username/email (== SQLite NOCASE)
            conn.execute("CREATE EXTENSION IF NOT EXISTS citext")

        for ddl in _SCHEMA_TABLES:
            conn.execute(_translate_ddl(ddl))

        # Existing installs already have the dedupe table; add the temporary
        # payload column so webhook requests can be acknowledged only after the
        # update is durably queued. Completed legacy rows carry no useful body.
        if not _column_exists(conn, "telegram_webhook_updates", "payload"):
            conn.execute("ALTER TABLE telegram_webhook_updates ADD COLUMN payload TEXT")
        conn.execute("UPDATE telegram_webhook_updates SET payload=NULL "
                     "WHERE completed_at IS NOT NULL AND payload IS NOT NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tg_webhook_pending "
                     "ON telegram_webhook_updates (completed_at, claimed_at)")

        # ------------------------------------------------------------------
        # MIGRATION 001 (developer-first pivot): the vault product is gone.
        # Drop every table that backed Vault/Cards/IDs/Contacts/WiFi/Servers/
        # Seeds/Notes/Bookmarks/Tasks (+ never-used api_keys/notifications).
        # Idempotent — safe on every boot. Kept tables (users, sessions,
        # user_2fa, jobs, snippets, activity_log, admin_audit_log, abuse_reports,
        # login_history, user_preferences) only reference users, never these.
        _DROPPED_VAULT_TABLES = (
            "wifi_shares", "user_wifi",  # child first (FK parent second)
            "vault_entries", "user_notes", "user_bookmarks", "user_categories",
            "user_cards", "user_tasks", "user_identities", "user_contacts",
            "user_servers", "user_recovery", "api_keys", "notifications",
        )
        for _t in _DROPPED_VAULT_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {_safe_ident(_t)}")

        # Legacy-DB migration: ensure the `role` column exists on users.
        if not _column_exists(conn, "users", "role"):
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")

        # Wrong-OTP-attempt counters (server-side OTP rate limiting).
        if not _column_exists(conn, "users", "otp_attempts"):
            conn.execute("ALTER TABLE users ADD COLUMN otp_attempts INTEGER NOT NULL DEFAULT 0")
        if not _column_exists(conn, "users", "reset_otp_attempts"):
            conn.execute("ALTER TABLE users ADD COLUMN reset_otp_attempts INTEGER NOT NULL DEFAULT 0")
        if not _column_exists(conn, "users", "password_changed_at"):
            conn.execute("ALTER TABLE users ADD COLUMN password_changed_at TEXT")

        # Pivot: admin flag, suspension flag, terms-of-use acceptance stamp.
        if not _column_exists(conn, "users", "is_admin"):
            conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        if not _column_exists(conn, "users", "is_suspended"):
            conn.execute("ALTER TABLE users ADD COLUMN is_suspended INTEGER NOT NULL DEFAULT 0")
        if not _column_exists(conn, "users", "agreed_terms_at"):
            conn.execute("ALTER TABLE users ADD COLUMN agreed_terms_at TEXT")
        # /queen — per-job RLIMIT skipped entirely for this user (see
        # runner/app.py's mem_limit_mb). Separate from job_limit_override,
        # which already existed and covers job COUNT, not RAM per job.
        if not _column_exists(conn, "users", "mem_unlimited"):
            conn.execute("ALTER TABLE users ADD COLUMN mem_unlimited INTEGER NOT NULL DEFAULT 0")
        # .zip bundles are extracted for real onto the runner's filesystem
        # (services/pingbot.py + runner/app.py:_extract_zip_bundle) — more
        # surface than a single pasted file, so it's admin-granted rather
        # than open to everyone by default. Off (0) until an admin turns it
        # on for a specific user.
        if not _column_exists(conn, "users", "can_upload_zip"):
            conn.execute("ALTER TABLE users ADD COLUMN can_upload_zip INTEGER NOT NULL DEFAULT 0")
        # Per-user override of MAX_JOBS_PER_USER. NULL means "use the global
        # default" — this doesn't exist on the website admin panel at all;
        # the limit is currently one fixed number for every account.
        if not _column_exists(conn, "users", "job_limit_override"):
            conn.execute("ALTER TABLE users ADD COLUMN job_limit_override INTEGER")

        # Telegram login + device/IP tracking. These live in the CREATE TABLE
        # above, so FRESH databases already have them — but an existing
        # deployment never got them, and every login crashed with
        # `psycopg2.errors.UndefinedColumn: column "fingerprint" does not exist`.
        # Adding them here is what actually upgrades a live database.
        if not _column_exists(conn, "users", "telegram_id"):
            conn.execute("ALTER TABLE users ADD COLUMN telegram_id BIGINT")
            # UNIQUE must be a separate statement for an added column.
            try:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_telegram_id "
                    "ON users (telegram_id)"
                )
            except Exception as exc:  # pragma: no cover - index is best effort
                logger.warning("telegram_id unique index: %s", exc)
        # Who the linked Telegram account IS, cached at link time. A bare
        # numeric chat id cannot be recognised by the person reading it, so
        # the dashboard could confirm "connected" but not "connected to whom".
        if not _column_exists(conn, "users", "telegram_name"):
            conn.execute("ALTER TABLE users ADD COLUMN telegram_name TEXT")
        if not _column_exists(conn, "users", "fingerprint"):
            conn.execute("ALTER TABLE users ADD COLUMN fingerprint TEXT")
        if not _column_exists(conn, "users", "last_ip"):
            conn.execute("ALTER TABLE users ADD COLUMN last_ip TEXT")

        # Job environment variables (JSON blob). Added after launch, so
        # existing deployments need the ALTER too.
        if not _column_exists(conn, "jobs", "env"):
            conn.execute("ALTER TABLE jobs ADD COLUMN env TEXT")

        # Which worker a job physically lives on. Existing rows stay NULL and
        # fall back to the primary worker, which is exactly where they already
        # are — so this migration cannot strand a running bot.
        if not _column_exists(conn, "jobs", "worker_url"):
            conn.execute("ALTER TABLE jobs ADD COLUMN worker_url TEXT")
        # Desired process state survives control-plane and runner redeploys.
        # Existing deployed rows default to running, matching the platform's
        # 24/7 promise; future Stop actions set this to stopped explicitly.
        if not _column_exists(conn, "jobs", "desired_state"):
            conn.execute("ALTER TABLE jobs ADD COLUMN desired_state TEXT NOT NULL DEFAULT 'running'")

        # Telegram bot metadata is safe identity/status only. Raw tokens remain
        # solely in the user's source/env and are never copied here.
        for _col, _ddl in (
            ("telegram_bot_detected", "INTEGER NOT NULL DEFAULT 0"),
            ("telegram_bot_username", "TEXT"),
            ("telegram_bot_id", "TEXT"),
            ("telegram_token_fingerprint", "TEXT"),
            ("telegram_check_status", "TEXT"),
            ("telegram_verified_at", "TEXT"),
            ("telegram_framework", "TEXT"),
            ("telegram_update_mode", "TEXT"),
            ("telegram_token_source", "TEXT"),
        ):
            if not _column_exists(conn, "jobs", _col):
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {_safe_ident(_col)} {_ddl}")

        # GitHub-backed apps: what they were built from, and whether they follow
        # the branch by themselves. Existing rows stay NULL/0 — a job with no
        # repo_url is simply not a repo job, which is what every row was before
        # this existed, so the migration cannot change how an old app behaves.
        for _col, _ddl in (
            ("repo_url", "TEXT"),
            ("repo_entry", "TEXT"),
            ("repo_commit", "TEXT"),
            ("auto_deploy", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if not _column_exists(conn, "jobs", _col):
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {_safe_ident(_col)} {_ddl}")

        # Same story for the sessions table.
        if not _column_exists(conn, "sessions", "fingerprint"):
            conn.execute("ALTER TABLE sessions ADD COLUMN fingerprint TEXT")
        if not _column_exists(conn, "sessions", "expires_at"):
            conn.execute("ALTER TABLE sessions ADD COLUMN expires_at TEXT")

        # Read paths filter by time, then group by chat/command.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_events_created_at ON bot_events (created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_events_chat_id ON bot_events (chat_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_blocks_lookup "
                     "ON admin_blocks (scope, value, revoked_at, expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_job_deploy_events_created "
                     "ON job_deploy_events (created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_job_deploy_events_user "
                     "ON job_deploy_events (user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tg_verify_user_expiry "
                     "ON telegram_token_verifications (user_id, expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_tg_token_fingerprint "
                     "ON jobs (telegram_token_fingerprint)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_revisions_job_version "
                     "ON bot_revisions (job_id, version)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runner_nodes_enabled "
                     "ON runner_nodes (enabled)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runner_action_events_runner "
                     "ON runner_action_events (runner_url, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runner_action_events_job "
                     "ON runner_action_events (job_id, created_at)")

        # Bot Store lookups: the catalog is filtered by status + sorted by
        # installs, and every detail/library call resolves a listing by slug.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_store_items_status "
                     "ON store_items (status, category)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_store_items_installs "
                     "ON store_items (install_count)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_store_installs_user "
                     "ON store_installs (user_id, item_slug)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_store_installs_slug "
                     "ON store_installs (item_slug)")
        # One rating and one favourite per person per listing.
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_store_reviews_one "
                     "ON store_reviews (item_slug, user_id)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_store_favorites_one "
                     "ON store_favorites (item_slug, user_id)")

        # Rich broadcast — resumable delivery (idempotent)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_broadcast_campaigns_status "
                     "ON broadcast_campaigns (status, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_campaign "
                     "ON broadcast_recipients (campaign_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_telegram "
                     "ON broadcast_recipients (telegram_id, status)")
        # Legacy installs: if broadcast tables already existed without html column
        # (e.g. a partial earlier migration), add it idempotently.
        try:
            if not _column_exists(conn, "broadcast_campaigns", "html"):
                conn.execute("ALTER TABLE broadcast_campaigns ADD COLUMN html TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        try:
            if not _column_exists(conn, "broadcast_campaigns", "caption"):
                conn.execute("ALTER TABLE broadcast_campaigns ADD COLUMN caption TEXT")
        except Exception:
            pass
        try:
            if not _column_exists(conn, "broadcast_campaigns", "blocked_count"):
                conn.execute("ALTER TABLE broadcast_campaigns ADD COLUMN blocked_count INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            if not _column_exists(conn, "broadcast_recipients", "next_retry_at"):
                conn.execute("ALTER TABLE broadcast_recipients ADD COLUMN next_retry_at TEXT")
        except Exception:
            pass
        # Queen runner pin — idempotent index
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_queen_runner_pins_user "
                     "ON queen_runner_pins (user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_queen_runner_pins_url "
                     "ON queen_runner_pins (runner_url)")

        conn.commit()
    finally:
        conn.close()

    if DIALECT == "postgres":
        logger.info("PostgreSQL schema initialized (pool ready)")
    else:
        logger.info("SQLite database initialized at: %s", DB_PATH)