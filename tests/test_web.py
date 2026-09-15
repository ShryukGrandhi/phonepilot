"""The local web UI server, driven end to end over HTTP against the fake phone."""

from __future__ import annotations

import http.client
import json
import threading
import time

import httpx
import pytest

from phonepilot.brain.base import Action
from phonepilot.cloud import PhoneHarnessClient
from phonepilot.web.server import AppState, make_server
from tests.conftest import node
from tests.test_agent import ScriptedBrain


@pytest.fixture
def web(phone, tmp_path, monkeypatch):
    monkeypatch.setattr("phonepilot.agent.time.sleep", lambda s: None)
    monkeypatch.setattr("phonepilot.sessions.time.monotonic", time.monotonic)
    client = PhoneHarnessClient(api_key="test-key", transport=httpx.MockTransport(phone.handle), sleep=lambda s: None)
    brain = ScriptedBrain([Action("tap", {"element": 1}, "tap it"), Action("done", {"success": True, "summary": "done", "result": "42"})])
    app = AppState(client, brain, tmp_path, max_steps=5)
    server = make_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield app, server.server_address[1]
    server.shutdown()
    server.server_close()


def call(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    payload = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=payload, headers={"Content-Type": "application/json"} if payload else {})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, resp.getheader("Content-Type", ""), data


def wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_index_and_state(web):
    app, port = web
    status, ctype, body = call(port, "GET", "/")
    assert status == 200 and "text/html" in ctype and b"PhonePilot" in body
    status, _, body = call(port, "GET", "/api/state")
    assert status == 200 and json.loads(body)["status"] == "no_phone"


def test_task_requires_a_phone(web):
    app, port = web
    status, _, body = call(port, "POST", "/api/task", {"task": "do it"})
    assert status == 409 and "not ready" in json.loads(body)["error"]
    status, _, _ = call(port, "GET", "/api/frame.png")
    assert status == 404


def test_full_flow_start_run_events_frame_end(web, phone):
    app, port = web
    phone.tree = [node("Go", x=360, y=640)]
    status, _, _ = call(port, "POST", "/api/session/start", {"timeout_seconds": 300})
    assert status == 200
    assert wait_for(lambda: app.status == "ready")
    assert app.created_here and app.session and app.session.id == "fake123"

    status, ctype, body = call(port, "GET", "/api/frame.png")
    assert status == 200 and ctype == "image/png" and body[:4] == b"\x89PNG"

    status, _, _ = call(port, "POST", "/api/task", {"task": "tap go"})
    assert status == 200
    assert wait_for(lambda: app.status == "ready" and app.agent is None)
    kinds = [e["kind"] for e in app.hub.history]
    assert "task" in kinds and kinds.count("step") == 2 and "outcome" in kinds
    step = next(e for e in app.hub.history if e["kind"] == "step")
    assert step["action"] == "tap" and step["marked_url"].startswith("/runs/")
    status, ctype, body = call(port, "GET", step["marked_url"])
    assert status == 200 and ctype == "image/png"
    outcome = next(e for e in app.hub.history if e["kind"] == "outcome")
    assert outcome["success"] is True and outcome["result"] == "42"

    # second task while idle is fine; a task while running is refused
    app.status = "running"
    status, _, body = call(port, "POST", "/api/task", {"task": "again"})
    assert status == 409 and "already running" in json.loads(body)["error"]
    app.status = "ready"

    status, _, _ = call(port, "POST", "/api/session/end")
    assert status == 200
    assert wait_for(lambda: app.status == "no_phone")
    assert phone.state == "closing"


def test_run_files_cannot_escape_runs_dir(web):
    app, port = web
    status, _, _ = call(port, "GET", "/runs/../../pyproject.toml")
    assert status == 404


def test_sse_stream_sends_hello_then_events(web):
    app, port = web
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/api/events")
    resp = conn.getresponse()
    assert resp.getheader("Content-Type") == "text/event-stream"
    first = resp.readline()
    assert first.startswith(b"data: ")
    hello = json.loads(first[6:])
    assert hello["kind"] == "hello" and hello["state"]["status"] == "no_phone"
    resp.readline()  # blank line terminating the event
    app.hub.publish("log", text="ping from test")
    line = resp.readline()
    assert json.loads(line[6:])["text"] == "ping from test"
    conn.close()
