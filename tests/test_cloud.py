import httpx
import pytest

from phonepilot.cloud import (
    AuthError, BadRequest, CloudError, NotFound, NotReady, PhoneHarnessClient, ProvisioningFailed, Unsupported,
    new_idempotency_key,
)


def test_requires_api_key(monkeypatch):
    monkeypatch.delenv("PHONE_HARNESS_API_KEY", raising=False)
    with pytest.raises(ValueError):
        PhoneHarnessClient()


def test_create_then_wait_ready_polls_until_ready(client, phone):
    phone.polls_until_ready = 3
    s = client.create_session(timeout_seconds=600, idempotency_key=new_idempotency_key())
    assert s.state == "provisioning" and s.screen is None and s.ops == ()
    ready = client.wait_ready(s.id, timeout_s=30)
    assert ready.is_ready and ready.screen == (720, 1280) and "tree" in ready.ops


def test_wait_ready_raises_on_error_state(client, phone):
    phone.state = "error"
    with pytest.raises(ProvisioningFailed):
        client.wait_ready("fake123")


def test_op_returns_result_and_never_retries(client, phone):
    phone.state = "ready"
    assert client.op("fake123", "apps.current") == "app.lawnchair"
    phone.fail_next_op = {"status": 503, "payload": {"error": "busy"}}
    with pytest.raises(CloudError) as exc:
        client.op("fake123", "input.tap", x=1, y=2)
    assert exc.value.status == 503
    assert phone.ops_log.count(("input.tap", {"x": 1, "y": 2})) == 1


def test_error_mapping(client, phone):
    phone.state = "ready"
    with pytest.raises(Unsupported):
        client.op("fake123", "shell", cmd="id")
    phone.fail_next_op = {"status": 409, "payload": {"error": "not ready"}}
    with pytest.raises(NotReady):
        client.op("fake123", "nav.home")
    phone.fail_next_op = {"status": 400, "payload": {"error": "bad args"}}
    with pytest.raises(BadRequest):
        client.op("fake123", "input.text", s="x")
    with pytest.raises(NotFound):
        client.get_session("nope")


def test_auth_error():
    c = PhoneHarnessClient(api_key="wrong", transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "bad"})))
    with pytest.raises(AuthError):
        c.account()


def test_get_retries_transient_5xx():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"error": "warming up"})
        return httpx.Response(200, json={"ok": True})

    c = PhoneHarnessClient(api_key="k", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert c.health() == {"ok": True}
    assert calls["n"] == 3


def test_snapshot_rejects_non_image(client, phone):
    phone.state = "ready"
    assert client.snapshot("fake123")[:8] == b"\x89PNG\r\n\x1a\n"
    c = PhoneHarnessClient(api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"error": "x"})))
    with pytest.raises(CloudError):
        c.snapshot("fake123")


def test_idempotency_key_format():
    key = new_idempotency_key()
    assert 16 <= len(key) <= 128
    assert all(ch.isalnum() or ch in "-_" for ch in key)


def test_session_seconds_left(client, phone):
    phone.state = "ready"
    s = client.get_session("fake123")
    assert 590 < s.seconds_left() <= 600
