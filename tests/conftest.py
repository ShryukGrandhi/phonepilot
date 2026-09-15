"""Shared fixtures: an in-memory fake of the Phone Harness API behind httpx.MockTransport."""

from __future__ import annotations

import base64
import io
import json
import time
from dataclasses import dataclass, field

import httpx
import pytest
from PIL import Image

from phonepilot.cloud import PhoneHarnessClient

SCREEN = (720, 1280)
ALL_OPS = [
    "screen.capture", "screen.bounds", "screen.require", "screen.text", "input.tap", "input.press",
    "input.drag", "input.scroll", "input.keys", "input.text", "nav.home", "nav.back", "nav.recents",
    "apps.launch", "apps.current", "apps.list", "tree",
]


def png_bytes(color=(40, 40, 40), size=SCREEN) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


@dataclass
class FakePhone:
    """Minimal stateful stand-in for the cloud service."""

    polls_until_ready: int = 0
    state: str = "provisioning"
    ops_log: list = field(default_factory=list)
    color: tuple = (40, 40, 40)
    tree: list = field(default_factory=list)
    app: str | None = "app.lawnchair"
    fail_next_op: dict | None = None
    sid: str = "fake123"
    fail_create: str | None = None  # when set, POST /sessions answers 409 with this message (provider full)
    fail_provision: str | None = None  # when set, a created session goes provisioning -> error with this message

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if request.headers.get("authorization") != "Bearer test-key":
            return httpx.Response(401, json={"error": "bad key"})
        if path == "/me":
            return httpx.Response(200, json={"balance_cents": 1000, "price_cents_per_minute": 35})
        if path == "/sessions" and method == "POST":
            if self.fail_create:
                return httpx.Response(409, json={"error": self.fail_create})
            self.state = "provisioning" if (self.polls_until_ready or self.fail_provision) else "ready"
            return httpx.Response(202, json=self._session())
        if path == f"/sessions/{self.sid}" and method == "GET":
            if self.fail_provision and self.state == "provisioning":
                self.state = "error"
                return httpx.Response(200, json={**self._session(), "error": self.fail_provision})
            if self.polls_until_ready > 0:
                self.polls_until_ready -= 1
                if self.polls_until_ready == 0:
                    self.state = "ready"
            return httpx.Response(200, json=self._session())
        if path == f"/sessions/{self.sid}" and method == "DELETE":
            self.state = "closing"
            return httpx.Response(202, json={"released": False, "cleanup_pending": True})
        if path == f"/sessions/{self.sid}/frame.png":
            return httpx.Response(200, content=png_bytes(self.color), headers={"content-type": "image/png"})
        if path == f"/sessions/{self.sid}/op":
            return self._op(json.loads(request.content))
        return httpx.Response(404, json={"error": f"no route {method} {path}"})

    def _session(self) -> dict:
        ready = self.state == "ready"
        return {
            "id": self.sid, "state": self.state, "provider": "shlut", "device": "fake", "close_requested": None,
            "cleanup_pending": False, "cleanup_status": None, "billing_mode": "metered", "phone": None,
            "watch_url": "https://example/watch", "screen": {"w": SCREEN[0], "h": SCREEN[1]} if ready else None,
            "age_s": 1, "ready_age_s": 0, "idle_s": 0, "ops": ALL_OPS if ready else [],
            "expires_at": time.time() + 600, "timeout_seconds": 600,
        }

    def _op(self, body: dict) -> httpx.Response:
        if self.state != "ready":
            return httpx.Response(409, json={"error": "session not ready", "state": self.state})
        op, kw = body["op"], body.get("kw", {})
        self.ops_log.append((op, kw))
        if self.fail_next_op:
            status, payload = self.fail_next_op["status"], self.fail_next_op["payload"]
            self.fail_next_op = None
            return httpx.Response(status, json=payload)
        if op not in ALL_OPS:
            return httpx.Response(400, json={"error": "operation is unavailable on this phone", "unsupported": True})
        if op == "screen.capture":
            b64 = base64.b64encode(png_bytes(self.color)).decode()
            return httpx.Response(200, json={"result": {"png_b64": b64, "bounds": {"x": 0, "y": 0, "w": 720, "h": 1280}}})
        if op == "tree":
            return httpx.Response(200, json={"result": self.tree})
        if op == "apps.current":
            return httpx.Response(200, json={"result": self.app})
        if op == "apps.list":
            return httpx.Response(200, json={"result": ["com.android.settings", "com.android.contacts"]})
        if op == "apps.launch":
            self.app = kw["name"]
            self.color = (90, 90, 90)
            return httpx.Response(200, json={"result": kw["name"]})
        if op == "input.text" and "%s" in kw.get("s", ""):
            return httpx.Response(400, json={"error": "input.text: shlut rejected the operation arguments"})
        if op == "input.tap":
            self.color = tuple((c + 60) % 256 for c in self.color)  # every tap visibly changes the screen
        return httpx.Response(200, json={"result": None})


@pytest.fixture
def phone() -> FakePhone:
    return FakePhone()


@pytest.fixture
def client(phone: FakePhone) -> PhoneHarnessClient:
    return PhoneHarnessClient(api_key="test-key", transport=httpx.MockTransport(phone.handle), sleep=lambda s: None)


def node(text="", desc="", clickable=True, x=100, y=100, w=200, h=60, res_id="", cls="android.widget.Button"):
    return {"text": text, "desc": desc, "id": res_id, "class": cls, "clickable": clickable, "x": x, "y": y, "w": w, "h": h}
