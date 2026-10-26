"""Durable, acknowledgement-first queue for Telegram webhook updates.

The webhook endpoint persists one update before returning 200. A daemon worker
runs the existing synchronous handler afterward, so a slow runner/API call does
not hold Telegram's request open. The JSON payload is cleared as soon as the
handler finishes; completed rows retain only the ID/timestamps for dedupe.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Callable

from database import get_db_connection

logger = logging.getLogger("codenest-telegram-webhook")

LEASE_NS = 300 * 1_000_000_000
KEEP_UPDATES = 50_000
REUSE_AFTER_NS = 7 * 24 * 60 * 60 * 1_000_000_000


class TelegramWebhookQueue:
    """Durable handler queue — now concurrent (3 workers) for button responsiveness.

    The old single-threaded queue serialized every callback + command. When one
    handler probed 6 sleeping runners (18s) the next button tap queued behind
    it and looked dead — that's the "inline buttons take long at night" bug.
    Workers compete for the same DB-claimed queue so parallelism helps even
    with the 5-10s cold-start stalls.
    """

    def __init__(self, *, lease_ns: int = LEASE_NS, poll_s: float = 0.8, workers: int = 3):
        self.lease_ns = int(lease_ns)
        self.poll_s = max(0.05, float(poll_s))
        self.workers = max(1, int(workers))
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._handler: Callable | None = None
        self._threads: list[threading.Thread] = []
        self._thread: threading.Thread | None = None  # compat alias for tests that check _thread
        self._stop = threading.Event()

    def start(self, handler: Callable) -> None:
        """Start workers once; safe to call from startup and each request."""
        with self._lock:
            self._handler = handler
            alive = [th for th in self._threads if th.is_alive()]
            self._threads = alive
            if alive:
                return
            self._stop.clear()
            for i in range(self.workers):
                th = threading.Thread(
                    target=self._run, daemon=True, name=f"telegram-webhook-queue-{i}")
                th.start()
                self._threads.append(th)
            self._thread = self._threads[0] if self._threads else None

    def stop(self, timeout: float = 2.0) -> None:
        """Test/process-shutdown hook; production workers are daemon threads."""
        self._stop.set()
        # wake all workers
        for _ in range(len(self._threads) or 1):
            try:
                self._queue.put(None, block=False)
            except Exception:
                pass
        for th in list(self._threads):
            if th and th is not threading.current_thread():
                th.join(timeout=max(0.0, timeout / max(1, len(self._threads))))
        self._threads = []
        self._thread = None

    def enqueue(self, update: dict, handler: Callable) -> str:
        """Persist and queue a Telegram update; return queued/duplicate/invalid/unavailable."""
        if not isinstance(update, dict):
            return "invalid"
        try:
            update_id = int(update["update_id"])
            payload = json.dumps(update, separators=(",", ":"), ensure_ascii=False)
        except (KeyError, TypeError, ValueError, OverflowError):
            return "invalid"
        except Exception:
            logger.exception("Could not serialize Telegram webhook update")
            return "invalid"

        claim = self.claim_update(update_id, payload=payload)
        if claim is None:
            return "unavailable"
        self.start(handler)
        if claim is False:
            return "duplicate"
        self._queue.put((update_id, claim))
        return "queued"

    def claim_update(self, update_id: int, payload: str | None = None):
        """Claim/reclaim an update ID. None means DB failure; False is a duplicate."""
        update_id = int(update_id)
        now_ns = time.time_ns()
        try:
            conn = get_db_connection()
        except Exception as exc:
            # Pool exhausted or DB down — treat as transient unavailable, not spam
            if "PoolError" in type(exc).__name__ or "exhausted" in str(exc).lower():
                logger.debug("claim_update pool exhausted — transient unavailable")
            else:
                logger.warning("Could not claim Telegram webhook update (%s)", type(exc).__name__)
            return None
        try:
            cur = conn.execute(
                "INSERT INTO telegram_webhook_updates"
                "(update_id,claimed_at,completed_at,payload) VALUES(?,?,NULL,?) "
                "ON CONFLICT(update_id) DO NOTHING",
                (update_id, now_ns, payload),
            )
            inserted = cur.rowcount == 1
            cur.close()
            if inserted:
                claim = now_ns
            else:
                row = conn.execute(
                    "SELECT claimed_at,completed_at,payload "
                    "FROM telegram_webhook_updates WHERE update_id=?",
                    (update_id,),
                ).fetchone()
                if not row:
                    conn.rollback()
                    return None
                claimed_at = int(row["claimed_at"] or 0)
                completed_at = int(row["completed_at"] or 0)
                old_payload = row["payload"]

                if completed_at:
                    if now_ns - completed_at <= REUSE_AFTER_NS:
                        # Clear any payload left by a process running the older
                        # completion path, but keep the dedupe row itself.
                        if old_payload is not None:
                            conn.execute(
                                "UPDATE telegram_webhook_updates SET payload=NULL "
                                "WHERE update_id=? AND completed_at IS NOT NULL",
                                (update_id,),
                            )
                        conn.commit()
                        return False
                    reclaim = True
                else:
                    reclaim = now_ns - claimed_at > self.lease_ns
                    if not reclaim:
                        # During a rolling deploy, the old handler may have
                        # claimed this ID before payload persistence existed.
                        # Save Telegram's retry now; the lease sweeper can take
                        # it over if that old process disappeared.
                        if payload is not None and old_payload is None:
                            conn.execute(
                                "UPDATE telegram_webhook_updates SET payload=? "
                                "WHERE update_id=? AND claimed_at=? "
                                "AND completed_at IS NULL AND payload IS NULL",
                                (payload, update_id, claimed_at),
                            )
                        conn.commit()
                        return False

                new_payload = payload if payload is not None else old_payload
                if completed_at:
                    cur = conn.execute(
                        "UPDATE telegram_webhook_updates "
                        "SET claimed_at=?,completed_at=NULL,payload=? "
                        "WHERE update_id=? AND claimed_at=? AND completed_at=?",
                        (now_ns, new_payload, update_id, claimed_at, completed_at),
                    )
                else:
                    cur = conn.execute(
                        "UPDATE telegram_webhook_updates "
                        "SET claimed_at=?,completed_at=NULL,payload=? "
                        "WHERE update_id=? AND claimed_at=? AND completed_at IS NULL",
                        (now_ns, new_payload, update_id, claimed_at),
                    )
                claim = now_ns if cur.rowcount == 1 else False
                cur.close()

            if claim is False:
                conn.commit()
                return False
            conn.execute(
                "DELETE FROM telegram_webhook_updates "
                "WHERE update_id<? AND completed_at IS NOT NULL",
                (update_id - KEEP_UPDATES,),
            )
            conn.commit()
            return claim
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.warning("Could not claim Telegram webhook update (%s)",
                           type(exc).__name__)
            return None
        finally:
            conn.close()

    def complete_update(self, update_id: int, claim: int) -> None:
        """Mark a claim complete and drop its temporary update body."""
        try:
            conn = get_db_connection()
        except Exception as exc:
            if "PoolError" in type(exc).__name__ or "exhausted" in str(exc).lower():
                logger.debug("complete_update pool exhausted — will be reclaimed by lease")
            else:
                logger.warning("Could not complete Telegram webhook claim (%s)", type(exc).__name__)
            return
        try:
            conn.execute(
                "UPDATE telegram_webhook_updates SET completed_at=?,payload=NULL "
                "WHERE update_id=? AND claimed_at=? AND completed_at IS NULL",
                (time.time_ns(), int(update_id), int(claim)),
            )
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.warning("Could not complete Telegram webhook claim (%s)",
                           type(exc).__name__)
        finally:
            conn.close()

    def _claim_stale_pending(self) -> list[tuple[int, int]]:
        """Atomically reclaim durable pending updates whose worker lease expired."""
        now_ns = time.time_ns()
        cutoff = now_ns - self.lease_ns
        try:
            conn = get_db_connection()
        except Exception as exc:
            if "PoolError" in type(exc).__name__ or "exhausted" in str(exc).lower():
                logger.debug("reclaim pool exhausted — backing off")
            else:
                logger.warning("Could not reclaim pending Telegram updates (%s: %s)", type(exc).__name__, exc)
            return []
        claimed = []
        try:
            rows = conn.execute(
                "SELECT update_id,claimed_at FROM telegram_webhook_updates "
                "WHERE completed_at IS NULL AND payload IS NOT NULL AND claimed_at<? "
                "ORDER BY update_id LIMIT 100",
                (cutoff,),
            ).fetchall()
            for row in rows:
                update_id = int(row["update_id"])
                old_claim = int(row["claimed_at"] or 0)
                new_claim = time.time_ns()
                cur = conn.execute(
                    "UPDATE telegram_webhook_updates SET claimed_at=? "
                    "WHERE update_id=? AND claimed_at=? AND completed_at IS NULL "
                    "AND payload IS NOT NULL",
                    (new_claim, update_id, old_claim),
                )
                if cur.rowcount == 1:
                    claimed.append((update_id, new_claim))
                cur.close()
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.warning("Could not reclaim pending Telegram updates (%s)",
                           type(exc).__name__)
        finally:
            conn.close()
        return claimed

    def _read_claim(self, update_id: int, claim: int):
        try:
            conn = get_db_connection()
        except Exception as exc:
            if "PoolError" not in type(exc).__name__:
                logger.warning("Could not load queued Telegram update %s (%s)", update_id, type(exc).__name__)
            return None
        try:
            row = conn.execute(
                "SELECT payload FROM telegram_webhook_updates "
                "WHERE update_id=? AND claimed_at=? AND completed_at IS NULL",
                (int(update_id), int(claim)),
            ).fetchone()
            if not row or row["payload"] is None:
                return None
            return json.loads(row["payload"])
        finally:
            conn.close()

    def _process(self, update_id: int, claim: int) -> None:
        try:
            update = self._read_claim(update_id, claim)
        except Exception as exc:
            # Leave it pending; a later lease pass retries after a transient DB
            # failure rather than acknowledging and discarding the update.
            logger.warning("Could not load queued Telegram update %s (%s)",
                           update_id, type(exc).__name__)
            return
        if update is None:
            return  # another worker completed/reclaimed it

        handler = self._handler
        if handler is None:
            return
        try:
            handler(update)
        except Exception as exc:
            # A bad update must not stop later messages from draining.
            logger.exception("Telegram update %s handler failed (%s)",
                             update_id, type(exc).__name__)
        finally:
            self.complete_update(update_id, claim)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                for item in self._claim_stale_pending():
                    self._queue.put(item)
                try:
                    item = self._queue.get(timeout=self.poll_s)
                except queue.Empty:
                    continue
                if item is None:
                    self._queue.task_done()
                    continue
                try:
                    self._process(*item)
                finally:
                    self._queue.task_done()
            except Exception:
                logger.exception("Telegram webhook queue worker iteration failed")
                self._stop.wait(self.poll_s)


_default_queue = TelegramWebhookQueue()


def start(handler: Callable) -> None:
    _default_queue.start(handler)


def enqueue(update: dict, handler: Callable) -> str:
    return _default_queue.enqueue(update, handler)


def claim_update(update_id: int, payload: str | None = None):
    return _default_queue.claim_update(update_id, payload)


def complete_update(update_id: int, claim: int) -> None:
    _default_queue.complete_update(update_id, claim)
