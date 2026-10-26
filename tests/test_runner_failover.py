"""Explicitly owner-suspended runner services are skipped, not mistaken for wakeups."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TERM_HOMES_DIR", tempfile.mkdtemp(prefix="runner-failover-homes-"))

from services import runner_client  # noqa: E402


class SuspendedResponse:
    status_code = 503
    text = "Service Suspended — This service has been suspended by its owner."
    headers = {}


class CreatedResponse:
    status_code = 201
    text = "{}"
    headers = {}

    def json(self):
        return {"id": "new-job"}


def test_owner_suspension_is_not_retried_as_a_wakeup():
    assert runner_client._is_owner_suspended(SuspendedResponse())
    assert not runner_client._looks_like_wakeup(SuspendedResponse())


def test_create_skips_suspended_runner_and_uses_next_healthy_one(monkeypatch):
    pool = ["https://suspended.example", "https://healthy.example"]
    calls = []
    responses = [SuspendedResponse(), CreatedResponse()]
    monkeypatch.setattr(runner_client, "runner_pool", lambda: pool)
    monkeypatch.setattr(runner_client, "_placement_order", lambda *a, **k: pool)
    monkeypatch.setattr(runner_client, "_secret_for_runner", lambda _url: "test-secret")

    def request(method, url, **kwargs):
        calls.append((method, url))
        return responses.pop(0)

    monkeypatch.setattr(runner_client.requests, "request", request)
    result = runner_client._runner_http("POST", "/internal/jobs", {"code": "pass"})

    assert calls == [
        ("POST", "https://suspended.example/internal/jobs"),
        ("POST", "https://healthy.example/internal/jobs"),
    ]
    assert result.status_code == 201
    assert result.placed_on == "https://healthy.example"
