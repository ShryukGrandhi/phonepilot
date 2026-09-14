"""Multi-user service: accounts, encrypted keys, and — most importantly — isolation between users."""

from __future__ import annotations

import http.client
import json
import threading
import time
from pathlib import Path
from http.cookies import SimpleCookie

import httpx
import pytest

from phonepilot.brain.base import Action
from phonepilot.cloud import PhoneHarnessClient
from phonepilot.service.app import Service, make_server
from phonepilot.service.sandbox import ThreadBackend
from phonepilot.service.auth import Auth, AuthError
from phonepilot.service.runtime import Limits, Pool
from phonepilot.service.secrets import SecretBox, hint
from phonepilot.service.store import Store
from tests.conftest import FakePhone, node
from tests.test_agent import ScriptedBrain

MASTER = SecretBox.generate_master_key()


# ------------------------------------------------------------ unit: store/auth/secrets
def test_secretbox_roundtrip_and_hint():
    box = SecretBox(MASTER)
    sealed = box.seal("pck_38273d1df42dccce2125136001172204fdf956cc")
    assert sealed != b"pck_38273d1df42dccce2125136001172204fdf956cc"
    assert box.open(sealed).startswith("pck_")
    assert hint("pck_38273d1df42dccce2125136001172204fdf956cc") == "pck_3…56cc"
    with pytest.raises(ValueError):
        SecretBox(SecretBox.generate_master_key()).open(sealed)


def test_auth_first_user_is_admin_then_invites_required(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    auth = Auth(store)
    first = auth.signup("a@x.io", "correct horse battery", None, "1.1.1.1")
    assert first.user.is_admin
    with pytest.raises(AuthError):
        auth.signup("b@x.io", "correct horse battery", None, "1.1.1.1")
    code = auth.create_invite(first.user)
    second = auth.signup("b@x.io", "correct horse battery", code, "1.1.1.1")
    assert not second.user.is_admin
    with pytest.raises(AuthError):
        auth.signup("c@x.io", "correct horse battery", code, "1.1.1.1")  # single use
    with pytest.raises(AuthError):
        auth.signup("d@x.io", "short", "whatever", None)
    assert auth.user_for_token(first.token).id == first.user.id
    assert auth.user_for_token("nope") is None
    with pytest.raises(AuthError):
        auth.login("a@x.io", "wrong password here", "1.1.1.1")
    assert auth.login("A@X.IO", "correct horse battery", "1.1.1.1").user.id == first.user.id
    auth.logout(first.token)
    assert auth.user_for_token(first.token) is None


def test_login_rate_limit(tmp_path):
    auth = Auth(Store(tmp_path / "db.sqlite3"))
    auth.signup("a@x.io", "correct horse battery", None, None)
    for _ in range(8):
        with pytest.raises(AuthError, match="wrong"):
            auth.login("a@x.io", "bad password 123", "9.9.9.9")
    with pytest.raises(AuthError, match="too many"):
        auth.login("a@x.io", "correct horse battery", "9.9.9.9")


# ------------------------------------------------------------ http: two users, one server
class TwoPhones:
    """One fake phone per user, so a leak would be observable."""

    def __init__(self):
        self.a, self.b = FakePhone(sid="phoneA"), FakePhone(sid="phoneB")
        self.a.color, self.b.color = (10, 10, 10), (200, 200, 200)
        self.a.tree = [node("A button", x=100, y=100)]
        self.b.tree = [node("B button", x=600, y=600)]


@pytest.fixture
def service(tmp_path, monkeypatch):
    phones = TwoPhones()
    monkeypatch.setattr("phonepilot.agent.time.sleep", lambda s: None)

    def client_factory(env):  # the sandbox env carries exactly one user's phone key -> that user's fake phone
        phone = {"pck_userA_key_000000000000": phones.a, "pck_userB_key_000000000000": phones.b}[env["PHONE_HARNESS_API_KEY"]]
        return PhoneHarnessClient(api_key="test-key", transport=httpx.MockTransport(phone.handle), sleep=lambda s: None)

    def brain_factory(env):
        key = env.get("GEMINI_API_KEY") or env.get("ANTHROPIC_API_KEY") or ""
        return ScriptedBrain([Action("tap", {"element": 1}, "tap"), Action("done", {"success": True, "summary": "ok", "result": key[-4:]})])

    backend = ThreadBackend(client_factory, brain_factory, log=lambda m: None)
    svc = Service(tmp_path / "data", master_key=MASTER, limits=Limits(max_phones_per_user=1, daily_phone_minutes=90,
                                                                       max_steps=5, transport="http"), pool=Pool(None, None, None),
                  backend=backend)
    server = make_server(svc, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield svc, server.server_address[1], phones
    server.shutdown()
    server.server_close()


class Browser:
    def __init__(self, port):
        self.port, self.cookie = port, None

    def call(self, method, path, body=None, csrf=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if csrf and method == "POST":
            headers["X-PhonePilot"] = "1"
        if self.cookie:
            headers["Cookie"] = f"pp_session={self.cookie}"
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        sc = resp.getheader("Set-Cookie")
        if sc:
            c = SimpleCookie()
            c.load(sc)
            self.cookie = c["pp_session"].value or None
        conn.close()
        return resp.status, resp.getheader("Content-Type", ""), data

    def js(self, method, path, body=None, **kw):
        status, _, data = self.call(method, path, body, **kw)
        return status, json.loads(data)


def wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_isolation_between_two_users(service):
    svc, port, phones = service
    a, b = Browser(port), Browser(port)

    # unauthenticated: nothing
    assert a.call("GET", "/")[0] == 302 and a.call("GET", "/api/state")[0] == 401
    assert a.call("POST", "/api/task", {"task": "x"}, csrf=False)[0] == 403, "CSRF header required"

    # signup A (admin), invite B
    st, j = a.js("POST", "/api/auth/signup", {"email": "a@x.io", "password": "correct horse battery"})
    assert st == 200 and j["is_admin"]
    st, j = a.js("POST", "/api/admin/invite", {})
    invite = j["invite"]
    st, j = b.js("POST", "/api/auth/signup", {"email": "b@x.io", "password": "correct horse battery", "invite": invite})
    assert st == 200 and not j["is_admin"]
    assert b.js("POST", "/api/admin/invite", {})[0] == 403, "non-admin cannot mint invites"

    # no keys yet -> cannot start (checked before any sandbox is launched)
    st, j = a.js("POST", "/api/session/start", {})
    assert st == 409 and "Phone Harness API key" in j["error"]
    assert a.js("POST", "/api/keys", {"provider": "phone_harness", "value": "garbage"})[0] == 409

    # each user sets their own keys; hints only come back
    st, j = a.js("POST", "/api/keys", {"provider": "phone_harness", "value": "pck_userA_key_000000000000"})
    assert st == 200 and j["hint"] == "pck_u…0000" and "pck_userA" not in json.dumps(j)
    a.js("POST", "/api/keys", {"provider": "gemini", "value": "AIzaSy_user_A_model_key_000000"})
    b.js("POST", "/api/keys", {"provider": "phone_harness", "value": "pck_userB_key_000000000000"})
    b.js("POST", "/api/keys", {"provider": "gemini", "value": "AIzaSy_user_B_model_key_000000"})
    st, j = a.js("GET", "/api/state")
    assert j["keys"]["phone_harness"]["own"] == "pck_u…0000" and j["user"]["email"] == "a@x.io"

    # both start phones
    assert a.js("POST", "/api/session/start", {"timeout_seconds": 600})[0] == 200
    assert b.js("POST", "/api/session/start", {"timeout_seconds": 600})[0] == 200
    rt_a, rt_b = svc.registry.get(svc.store.user_by_id(j and svc.store.user_by_email("a@x.io")[0].id)), None
    users = {u: svc.store.user_by_email(u)[0] for u in ("a@x.io", "b@x.io")}
    rt_a, rt_b = svc.registry.get(users["a@x.io"]), svc.registry.get(users["b@x.io"])
    assert wait_for(lambda: rt_a.status == "ready" and rt_b.status == "ready", timeout=10)
    assert rt_a.session_id == "phoneA" and rt_b.session_id == "phoneB"
    assert rt_a.sandbox.id != rt_b.sandbox.id and rt_a.sandbox.token != rt_b.sandbox.token, "one sandbox per user"
    assert a.js("POST", "/api/session/start", {})[0] == 409, "one phone per user"

    # frames come from each user's own phone
    _, _, fa = a.call("GET", "/api/frame.png")
    _, _, fb = b.call("GET", "/api/frame.png")
    assert fa != fb

    # B cannot attach A's phone (even knowing the id); A cannot see B's session id anywhere
    b.js("POST", "/api/session/end", {})
    assert wait_for(lambda: rt_b.status == "no_phone", timeout=10)
    st, j = b.js("POST", "/api/session/attach", {"session_id": "phoneA"})
    assert st == 403
    assert "phoneB" not in json.dumps(a.js("GET", "/api/state")[1])

    # A runs a task; the result carries A's model key suffix, so it used A's brain
    assert a.js("POST", "/api/task", {"task": "tap it"})[0] == 200
    assert wait_for(lambda: any(e["kind"] == "outcome" for e in list(rt_a.hub.history)) and rt_a.status == "ready", timeout=15)
    outcome = next(e for e in list(rt_a.hub.history) if e["kind"] == "outcome")
    assert outcome["success"] and outcome["result"] == "0000"
    step = next(e for e in list(rt_a.hub.history) if e["kind"] == "step")
    assert step["marked_url"].startswith("/runs/")
    assert ("input.tap", {"x": 100, "y": 100}) in phones.a.ops_log and not any(op == "input.tap" for op, _ in phones.b.ops_log)

    # B cannot read A's run files, A can; B's event stream never saw A's events
    assert a.call("GET", step["marked_url"])[0] == 200
    assert b.call("GET", step["marked_url"])[0] == 404
    assert not any(e["kind"] in ("step", "outcome") for e in list(rt_b.hub.history))
    assert wait_for(lambda: [r["task"] for r in a.js("GET", "/api/runs")[1]["runs"]] == ["tap it"], timeout=5)
    assert b.js("GET", "/api/runs")[1]["runs"] == []

    # path traversal
    assert a.call("GET", "/runs/../../phonepilot.sqlite3")[0] == 404
    assert a.call("GET", f"/runs/{step['marked_url'].split('/')[2]}/../../../phonepilot.sqlite3")[0] == 404

    # logout invalidates the cookie
    a.js("POST", "/api/auth/logout", {})
    assert a.call("GET", "/api/state")[0] == 401

    # secrets never appear in the sqlite file in plaintext
    raw = (svc.data_dir / "phonepilot.sqlite3").read_bytes()
    assert b"pck_userA_key" not in raw and b"AIzaSy_user_A" not in raw and b"correct horse battery" not in raw


def test_stale_no_phone_snapshot_does_not_cancel_a_pending_start(tmp_path):
    """The sandbox's initial 'hello' (status no_phone) can arrive after we asked it to start; it must be ignored."""
    from phonepilot.service.runtime import KeyResolver, Limits, Pool, UserRuntime
    from phonepilot.service.store import Store

    store = Store(tmp_path / "db.sqlite3")
    user = store.create_user("a@x.io", b"h", b"s")
    rt = UserRuntime(user, store, KeyResolver(store, SecretBox(MASTER), Pool(None, None, None)), Limits(), tmp_path, backend=None)
    rt.status = "starting"
    rt._start_pending = True
    rt._absorb_state({"status": "no_phone", "session_id": None})      # stale snapshot
    assert rt.status == "starting"
    rt._absorb_state({"status": "starting", "session_id": None})      # sandbox acknowledged
    assert rt.status == "starting" and not rt._start_pending
    rt._absorb_state({"status": "ready", "session_id": "s1", "seconds_left": 600})
    assert rt.status == "ready" and rt.session_id == "s1" and store.phone_owner("s1") == user.id
    rt._absorb_state({"status": "no_phone", "session_id": None})      # a real end
    assert rt.status == "no_phone" and rt.session_id is None and store.open_phones(user.id) == []


def test_sandbox_end_during_provisioning_releases_phone(phone, monkeypatch, tmp_path):
    """AppState (the sandbox) must release a phone that becomes ready after an end request."""
    import phonepilot.web.server as ws
    from phonepilot.sessions import Lease

    gate = threading.Event()
    phone.state = "ready"
    client = PhoneHarnessClient(api_key="test-key", transport=httpx.MockTransport(phone.handle), sleep=lambda s: None)

    def slow_acquire(c, sid, timeout, log):  # provisioning that we control
        gate.wait(5)
        return Lease(c.get_session("fake123"), created_here=True, ready_wait_s=0.0)

    monkeypatch.setattr(ws, "acquire", slow_acquire)
    app = ws.AppState(client, ScriptedBrain([]), tmp_path, log=None)
    app.start_session(300)
    assert app.status == "starting"
    app.end_session()  # while provisioning
    assert app.status == "ending" and app._end_requested
    gate.set()
    assert wait_for(lambda: app.status == "no_phone", timeout=10)
    assert phone.state == "closing", "the phone was released as soon as it became ready"


def test_security_headers_present(service):
    svc, port, _ = service
    b = Browser(port)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/login")
    resp = conn.getresponse()
    resp.read()
    assert resp.getheader("Content-Security-Policy", "").startswith("default-src 'self'")
    assert resp.getheader("X-Frame-Options") == "DENY" and resp.getheader("X-Content-Type-Options") == "nosniff"
    conn.close()
