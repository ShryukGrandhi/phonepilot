"""Thin, typed client for the Phone Harness Cloud API.

Covers the public contract documented at https://phone-harness.com/docs/openapi.json:
sessions, phone operations, snapshots, viewer, account, history, APK install.

Design notes
- One `_request` funnel: auth header, JSON decode, retry on transient failures,
  and a single place that maps HTTP status -> typed exception.
- Everything returned is an immutable dataclass or plain dict; nothing here
  mutates caller state.
- Phone operations are NOT idempotent on the server (taps replay), so `op()`
  never retries on its own. Only reads (GET) and session creation with an
  Idempotency-Key are safe to retry.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "https://api.phone-harness.com"
DEFAULT_TIMEOUT_S = 60.0
READY_POLL_S = 2.0
RETRY_BACKOFF_S = (1.0, 2.0, 4.0)
RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})


class CloudError(Exception):
    """Any non-2xx answer from the Cloud API, with the parsed error body."""

    def __init__(self, status: int, payload: dict[str, Any], method: str, path: str):
        self.status = status
        self.payload = payload
        self.method = method
        self.path = path
        super().__init__(f"{method} {path} -> {status}: {payload.get('error', payload)}")

    @property
    def code(self) -> str | None:
        return self.payload.get("code")

    @property
    def unsupported(self) -> bool:
        return bool(self.payload.get("unsupported"))


class AuthError(CloudError):
    """401/403: bad key, no rental access, or not enough credit."""


class NotFound(CloudError):
    """404/410: no live session visible to the caller."""


class NotReady(CloudError):
    """409: session is provisioning, closing, or the action conflicts with state."""


class Unsupported(CloudError):
    """400 with unsupported:true — this provider cannot do that op."""


class BadRequest(CloudError):
    """400/422: invalid arguments."""


class ProvisioningFailed(RuntimeError):
    """The session reached state=error before becoming ready."""


@dataclass(frozen=True)
class Session:
    id: str
    state: str
    screen: tuple[int, int] | None
    ops: tuple[str, ...]
    expires_at: float | None
    watch_url: str | None
    error: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Session":
        screen = d.get("screen")
        return cls(
            id=d["id"],
            state=d["state"],
            screen=(int(screen["w"]), int(screen["h"])) if screen else None,
            ops=tuple(d.get("ops") or ()),
            expires_at=d.get("expires_at"),
            watch_url=d.get("watch_url"),
            error=d.get("error"),
            raw=d,
        )

    @property
    def is_ready(self) -> bool:
        return self.state == "ready"

    def seconds_left(self, now: float | None = None) -> float | None:
        if self.expires_at is None:
            return None
        return self.expires_at - (now if now is not None else time.time())


def _error_class(status: int, payload: dict[str, Any]) -> type[CloudError]:
    if status in (401, 403):
        return AuthError
    if status in (404, 410):
        return NotFound
    if status == 409:
        return NotReady
    if status == 400 and payload.get("unsupported"):
        return Unsupported
    if status in (400, 422):
        return BadRequest
    return CloudError


def new_idempotency_key(prefix: str = "pp") -> str:
    """A key matching ^[A-Za-z0-9_-]{16,128}$."""
    return f"{prefix}-{secrets.token_urlsafe(18)}".replace("=", "")[:128]


class PhoneHarnessClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ):
        key = api_key or os.environ.get("PHONE_HARNESS_API_KEY")
        if not key:
            raise ValueError("PHONE_HARNESS_API_KEY is not set")
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {key}", "User-Agent": "phonepilot/0.1"},
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "PhoneHarnessClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ core
    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        retry: bool = False,
    ) -> httpx.Response:
        attempts = len(RETRY_BACKOFF_S) + 1 if retry else 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                resp = self._http.request(method, path, json=json, content=content, headers=headers)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    self._sleep(RETRY_BACKOFF_S[attempt])
                    continue
                raise CloudError(0, {"error": f"network: {exc}"}, method, path) from exc
            if resp.status_code in RETRYABLE_STATUSES and attempt + 1 < attempts:
                self._sleep(RETRY_BACKOFF_S[attempt])
                continue
            if resp.status_code >= 400:
                payload = _safe_json(resp)
                raise _error_class(resp.status_code, payload)(resp.status_code, payload, method, path)
            return resp
        raise CloudError(0, {"error": f"exhausted retries: {last_exc}"}, method, path)

    def _json(self, method: str, path: str, **kw) -> Any:
        return _safe_json(self._request(method, path, **kw))

    # --------------------------------------------------------------- account
    def health(self) -> dict[str, Any]:
        return self._json("GET", "/health", retry=True)

    def account(self) -> dict[str, Any]:
        return self._json("GET", "/me", retry=True)

    def history(self, limit: int | None = None) -> list[dict[str, Any]]:
        items = self._json("GET", "/history", retry=True)
        return items[:limit] if limit else items

    # -------------------------------------------------------------- sessions
    def create_session(self, timeout_seconds: int = 300, idempotency_key: str | None = None) -> Session:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        body = {"timeout_seconds": int(timeout_seconds)}
        # Safe to retry only when keyed: the server replays the same session.
        data = self._json("POST", "/sessions", json=body, headers=headers, retry=bool(idempotency_key))
        return Session.from_json(data)

    def get_session(self, sid: str) -> Session:
        return Session.from_json(self._json("GET", f"/sessions/{sid}", retry=True))

    def list_sessions(self) -> list[Session]:
        return [Session.from_json(d) for d in self._json("GET", "/sessions", retry=True)]

    def end_session(self, sid: str) -> dict[str, Any]:
        return self._json("DELETE", f"/sessions/{sid}", retry=True)

    def recover(self, request_key: str) -> dict[str, Any]:
        return self._json("GET", f"/sessions/requests/{request_key}", retry=True)

    def wait_ready(self, sid: str, timeout_s: float = 600.0, poll_s: float = READY_POLL_S, on_poll=None) -> Session:
        """Poll until state=ready. Raises ProvisioningFailed on state=error, TimeoutError otherwise."""
        deadline = time.monotonic() + timeout_s
        while True:
            s = self.get_session(sid)
            if on_poll:
                on_poll(s)
            if s.is_ready:
                return s
            if s.state == "error":
                raise ProvisioningFailed(s.error or "session entered error state")
            if s.state == "closing":
                raise ProvisioningFailed("session is closing")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"session {sid} still {s.state} after {timeout_s:.0f}s")
            self._sleep(poll_s)

    # ----------------------------------------------------------------- phone
    def op(self, sid: str, op: str, **kw: Any) -> Any:
        """Run one advertised operation and return its `result`. Never retried."""
        data = self._json("POST", f"/sessions/{sid}/op", json={"op": op, "kw": kw})
        return data.get("result") if isinstance(data, dict) else data

    def snapshot(self, sid: str) -> bytes:
        """Coalesced still frame (<= 1 capture / 2 s server-side). Cheaper than screen.capture."""
        resp = self._request("GET", f"/sessions/{sid}/frame.png", retry=True)
        ctype = resp.headers.get("content-type", "")
        if not ctype.startswith("image/"):
            raise CloudError(resp.status_code, {"error": f"unexpected content-type {ctype}"}, "GET", "frame.png")
        return resp.content

    def owner_viewer(self, sid: str) -> dict[str, Any]:
        return self._json("GET", f"/sessions/{sid}/owner-viewer")

    def install_apk(self, sid: str, apk_path: str | Path) -> dict[str, Any]:
        data = Path(apk_path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        headers = {
            "Content-Type": "application/vnd.android.package-archive",
            "Content-Length": str(len(data)),
            "X-APK-SHA256": digest,
        }
        return self._json("POST", f"/sessions/{sid}/apk", content=data, headers=headers)

    # ------------------------------------------------------------------- adb
    def enable_adb(self, sid: str, public_key: str) -> dict[str, Any]:
        return self._json("POST", f"/sessions/{sid}/adb", json={"public_key": public_key.strip()})

    def get_adb(self, sid: str) -> dict[str, Any]:
        return self._json("GET", f"/sessions/{sid}/adb")

    def revoke_adb(self, sid: str) -> dict[str, Any]:
        return self._json("DELETE", f"/sessions/{sid}/adb")


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {"error": resp.text[:500] or f"HTTP {resp.status_code} with empty body"}
