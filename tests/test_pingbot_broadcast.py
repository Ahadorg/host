"""Rich broadcast system — text/media, safe HTML, URL buttons, preview/confirm,
status/cancel, retry/backoff, resumable, rate-limit respect, one failure never blocks others.
"""
import os
import sys
import tempfile
import time
import json
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TERM_HOMES_DIR", tempfile.mkdtemp(prefix="broadcast-homes-"))

import database  # noqa: E402
database.init_db()

from services import pingbot_broadcast as pb  # noqa: E402
# ensure schema for broadcast (idempotent)
pb.ensure_broadcast_schema()

def _clear_broadcast():
    conn = database.get_db_connection()
    try:
        conn.execute("DELETE FROM broadcast_recipients")
        conn.execute("DELETE FROM broadcast_campaigns")
        conn.commit()
    finally:
        conn.close()

def _wait_for_status(cid, want_status, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        c = pb.get_campaign(cid)
        if c and c["status"] == want_status:
            return True
        time.sleep(0.05)
    return False

def test_sanitize_html_allows_safe_and_escapes_unsafe():
    _clear_broadcast()
    raw = '<b>hello</b> <script>alert(1)</script> <a href="https://example.com">click</a> <a href="javascript:alert(1)">bad</a> plain & text'
    out = pb.sanitize_html(raw)
    assert "<b>hello</b>" in out
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert '<a href="https://example.com">click</a>' in out
    # javascript URL must be escaped
    assert "javascript:" not in out or "&lt;a" in out
    assert "plain &amp; text" in out or "plain & text" not in out
    # alias works
    assert pb.safe_html(raw) == out
    assert pb.escape_html(raw) == out

def test_parse_url_buttons_various_forms():
    _clear_broadcast()
    assert pb.parse_url_buttons(None) == []
    assert pb.parse_url_buttons("") == []
    assert pb.parse_url_buttons("-") == []
    # list form
    lst = pb.parse_url_buttons([{"text": "Visit", "url": "https://example.com"}])
    assert lst == [[{"text": "Visit", "url": "https://example.com"}]]
    # multiline string with |
    spec = "Visit | https://example.com\nDocs | https://docs.example.com"
    rows = pb.parse_url_buttons(spec)
    assert len(rows) == 2
    assert rows[0][0]["text"] == "Visit"
    assert rows[0][0]["url"] == "https://example.com"
    # invalid url skipped
    bad = pb.parse_url_buttons([{"text": "X", "url": "javascript:alert(1)"}])
    assert bad == []
    # JSON string
    j = json.dumps([[{"text": "A", "url": "https://a.example"}]])
    assert pb.parse_url_buttons(j) == [[{"text": "A", "url": "https://a.example"}]]

def test_create_campaign_preview_confirm_and_status_cancel():
    _clear_broadcast()
    pb.ensure_broadcast_schema()
    # create preview campaign with explicit recipients
    cid = pb.create_campaign(999, "<b>hello</b> world", buttons="Docs | https://example.com", recipients=[111, 222, 333], auto_start=False)
    c = pb.get_campaign(cid)
    assert c is not None
    assert c["status"] == "preview"
    # html sanitized
    assert "<b>hello</b>" in c["html"]
    assert c["total_count"] == 3
    # preview text contains html and button
    prev = pb.preview_text(cid)
    assert "hello" in prev
    assert "Recipients" in prev
    # status
    st = pb.campaign_stats(cid)
    assert st["total"] == 3 and st["pending"] == 3
    # confirm -> sending and worker would start (but we stop it for sync test)
    pb.stop_broadcast_worker()
    assert pb.confirm_campaign(cid) is True
    c2 = pb.get_campaign(cid)
    assert c2["status"] == "sending"
    # cancel another preview
    cid2 = pb.create_campaign(999, "to cancel", recipients=[444], auto_start=False)
    assert pb.cancel_campaign(cid2) is True
    c3 = pb.get_campaign(cid2)
    assert c3["status"] == "cancelled"
    # cancelling already cancelled returns False
    assert pb.cancel_campaign(cid2) is False

def test_one_failure_does_not_block_others():
    _clear_broadcast()
    pb.clear_send_fn()
    # create campaign with 3 recipients, inject send that fails for middle one
    cid = pb.create_campaign(1, "hello all", recipients=[10, 20, 30], auto_start=False)
    pb.confirm_campaign(cid)
    pb.stop_broadcast_worker()
    calls = []
    def fake_send(tid, campaign):
        calls.append(tid)
        if tid == 20:
            return {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
        return {"ok": True}
    pb.set_send_fn(fake_send)
    try:
        res = pb.process_campaign_sync(cid, max_recipients=10)
        # all three attempted, even though 20 failed
        assert set(calls) == {10, 20, 30}
        # 20 should be marked blocked (chat not found is blocked classification), others sent
        stats = pb.campaign_stats(cid)
        assert stats["sent"] == 2
        assert stats["blocked"] == 1 or stats["failed"] == 1
        assert stats["pending"] == 0
    finally:
        pb.clear_send_fn()

def test_blocked_recipient_does_not_stop_worker():
    _clear_broadcast()
    cid = pb.create_campaign(1, "block test", recipients=[100, 200, 300], auto_start=False)
    pb.confirm_campaign(cid)
    pb.stop_broadcast_worker()
    def fake_send(tid, campaign):
        if tid == 200:
            return {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}
        return {"ok": True}
    pb.set_send_fn(fake_send)
    try:
        pb.process_campaign_sync(cid)
        stats = pb.campaign_stats(cid)
        # 200 blocked, others sent
        assert stats["sent"] == 2
        assert stats["blocked"] == 1
        # ensure not stuck: status completed
        c = pb.get_campaign(cid)
        assert c["status"] == "completed"
    finally:
        pb.clear_send_fn()

def test_429_is_respected_and_retried_with_backoff():
    _clear_broadcast()
    cid = pb.create_campaign(1, "rate limit", recipients=[501, 502], auto_start=False)
    pb.confirm_campaign(cid)
    pb.stop_broadcast_worker()
    attempts = {}
    sleep_calls = []
    orig_sleep = time.sleep
    # patch sleep inside pb worker? For sync mode, we still call _schedule_retry with retry_after but sync doesn't sleep; we test _extract_retry_after and retry logic
    def fake_send(tid, campaign):
        n = attempts.get(tid, 0)
        attempts[tid] = n + 1
        if tid == 501 and n == 0:
            return {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 1", "parameters": {"retry_after": 1}}
        return {"ok": True}
    pb.set_send_fn(fake_send)
    try:
        # Use sync processing which does not auto sleep on 429 but schedules retry.
        # First pass: 501 gets 429 and scheduled for retry, 502 sent
        pb.process_campaign_sync(cid, max_recipients=2)
        # after first sync, 501 should still be pending with next_retry_at set (but sync's second loop would have retried if we looped)
        # Our sync loops over pending repeatedly; so after 501's 429, it schedules retry and moves on, then next iteration picks 501 again if retry time passed?
        # Since we set retry_after 1 sec, pending filter will exclude it until time passes. So after first sync, stats should show 1 pending (501), 1 sent (502)
        # That demonstrates 429 handling
        stats = pb.campaign_stats(cid)
        # With current sync logic, 501 stays pending because next_retry_at is in future (1 sec)
        assert stats["pending"] == 1 or stats["sent"] == 2  # allow either because time may have passed
        # Wait for retry window and process again
        time.sleep(1.1)
        pb.process_campaign_sync(cid, max_recipients=5)
        stats2 = pb.campaign_stats(cid)
        assert stats2["sent"] == 2
        assert stats2["pending"] == 0
    finally:
        pb.clear_send_fn()

def test_resumable_delivery_after_restart():
    _clear_broadcast()
    cid = pb.create_campaign(1, "resumable", recipients=[601, 602, 603, 604], auto_start=False)
    pb.confirm_campaign(cid)
    pb.stop_broadcast_worker()
    # simulate partial delivery: send 2 manually, leave 2 pending
    sent = set()
    def fake_send_partial(tid, campaign):
        if tid in (601, 602):
            sent.add(tid)
            return {"ok": True}
        # simulate crash before others — leave pending
        return {"ok": False, "error_code": 500, "description": "simulated crash before send"}  # will be marked failed if not retried?
        # Instead we will mark only first two as sent and keep pending for others by scheduling retry
    # But to mimic resumable, we directly process only 2 via max_recipients then simulate restart
    def fake_send_ok(tid, campaign):
        return {"ok": True}
    pb.set_send_fn(fake_send_ok)
    try:
        # process only 2
        pb.process_campaign_sync(cid, max_recipients=2)
        stats = pb.campaign_stats(cid)
        assert stats["sent"] == 2
        assert stats["pending"] == 2
        # "restart": stop worker (already stopped), ensure schema, resume
        pb.ensure_broadcast_schema()
        # simulate restart by checking that campaign still sending and pending persisted
        c = pb.get_campaign(cid)
        assert c["status"] == "sending"
        # resume processing remaining
        pb.process_campaign_sync(cid, max_recipients=10)
        stats2 = pb.campaign_stats(cid)
        assert stats2["sent"] == 4
        assert stats2["pending"] == 0
        c2 = pb.get_campaign(cid)
        assert c2["status"] == "completed"
    finally:
        pb.clear_send_fn()

def test_media_and_buttons_persisted():
    _clear_broadcast()
    cid = pb.create_campaign(10, "media caption", media_type="photo", media_file_id="FILE123", buttons="Watch | https://example.com/watch", recipients=[701], auto_start=False)
    c = pb.get_campaign(cid)
    assert c["media_type"] == "photo"
    assert c["media_file_id"] == "FILE123"
    assert c["buttons_json"] is not None
    rows = json.loads(c["buttons_json"])
    assert rows[0][0]["url"] == "https://example.com/watch"
    pb.confirm_campaign(cid)
    pb.stop_broadcast_worker()
    captured = {}
    def fake_send(tid, camp):
        captured["method"] = camp.get("media_type")
        captured["file"] = camp.get("media_file_id")
        return {"ok": True}
    pb.set_send_fn(fake_send)
    try:
        pb.process_campaign_sync(cid)
        assert captured["method"] == "photo"
        assert captured["file"] == "FILE123"
    finally:
        pb.clear_send_fn()

def test_postgres_migration_idempotent():
    # ensure calling twice doesn't error and tables still work
    pb.ensure_broadcast_schema()
    pb.ensure_broadcast_schema()
    cid = pb.create_campaign(1, "idempotent", recipients=[800], auto_start=False)
    assert cid is not None
    _clear_broadcast()

def test_pg_sql_is_valid_postgres():
    # Check our DDL parses as valid PG after translation (mirrors validate_postgres_sql)
    try:
        import pglast
    except ImportError:
        return  # skip if not available
    import database as _db
    orig = _db.DIALECT
    _db.DIALECT = "postgres"
    try:
        from database import _translate_ddl, _SCHEMA_TABLES
        # find broadcast DDLs
        for ddl in _SCHEMA_TABLES:
            if "broadcast_" in ddl:
                pg = _translate_ddl(ddl)
                # must parse
                try:
                    pglast.parse_sql(pg)
                except Exception as exc:
                    assert False, f"PG parse failed for broadcast DDL: {exc}\n{pg[:500]}"
    finally:
        _db.DIALECT = orig

def test_rate_limit_not_blocking_on_failure():
    _clear_broadcast()
    # Verify that after a generic failure, worker still continues to next recipient without long sleep blocking others
    cid = pb.create_campaign(1, "fail continue", recipients=[901, 902, 903], auto_start=False)
    pb.confirm_campaign(cid)
    pb.stop_broadcast_worker()
    order = []
    def fake_send(tid, camp):
        order.append(tid)
        if tid == 902:
            return {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
        return {"ok": True}
    pb.set_send_fn(fake_send)
    try:
        pb.process_campaign_sync(cid)
        assert order == [901, 902, 903]
        stats = pb.campaign_stats(cid)
        # 902 should be blocked (chat not found is blocked classification)
        assert stats["blocked"] == 1
        assert stats["sent"] == 2
    finally:
        pb.clear_send_fn()

