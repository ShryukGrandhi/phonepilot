"""Multi-user HTTP service: `phonepilot serve`.

Every request is resolved to a user from the session cookie before it can
reach a phone, an event stream, a frame, or a run file; the user's `UserRuntime`
is the only object the request can act on. Defense in depth on top of that:
run files are served from the user's own folder *and* checked against the
runs table; frames re-check phone ownership; POSTs need a custom header
(CSRF) plus a SameSite=Strict cookie; API keys never leave the server.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from ..cloud import CloudError
from .auth import Auth, AuthError, clear_cookie_header, cookie_header, COOKIE_NAME
import os
from .runtime import ConfigError, KeyResolver, Limits, Pool, QuotaError, Registry, UserRuntime
from .sandbox import SandboxError, pick_backend
from .secrets import SecretBox
from .store import Store, User

STATIC = Path(__file__).with_name("static")
MAX_BODY = 64 * 1024
SSE_KEEPALIVE_S = 15.0
CSRF_HEADER = "X-PhonePilot"
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                               "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; "
                               "form-action 'self'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class Service:
    def __init__(self, data_dir: Path, master_key: str | None = None, limits: Limits | None = None,
                 pool: Pool | None = None, secure_cookies: bool = False, trust_proxy: bool = False,
                 log: Callable[[str], None] = print, backend: Any = None, sandbox: str | None = None,
                 allowed_origins: tuple[str, ...] | None = None):
        self.data_dir = data_dir
        env_origins = tuple(o.strip() for o in os.environ.get("PHONEPILOT_ALLOWED_ORIGINS", "").split(",") if o.strip())
        self.allowed_origins = tuple(allowed_origins) if allowed_origins is not None else env_origins
        self.store = Store(data_dir / "phonepilot.sqlite3")
        self.box = SecretBox(master_key)
        self.auth = Auth(self.store)
        self.limits = limits or Limits()
        self.keys = KeyResolver(self.store, self.box, pool or Pool())
        self.backend = backend or pick_backend(sandbox, log)
        self.registry = Registry(self.store, self.keys, self.limits, data_dir, self.backend, log)
        self.secure_cookies = secure_cookies
        self.trust_proxy = trust_proxy
        self.log = log


class Handler(BaseHTTPRequestHandler):
    svc: Service
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        return  # no access log with query strings / bodies

    # ------------------------------------------------------------ helpers
    def _ip(self) -> str:
        if self.svc.trust_proxy:
            fwd = self.headers.get("X-Forwarded-For")
            if fwd:
                return fwd.split(",")[0].strip()
        return self.client_address[0]

    def _cookie_token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        c = SimpleCookie()
        try:
            c.load(raw)
        except Exception:  # noqa: BLE001
            return None
        return c[COOKIE_NAME].value if COOKIE_NAME in c else None

    def _user(self) -> User | None:
        return self.svc.auth.user_for_token(self._cookie_token())

    def _runtime(self) -> UserRuntime | None:
        user = self._user()
        return self.svc.registry.get(user) if user else None

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("body too large")
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _cors_headers(self) -> dict[str, str]:
        origin = self.headers.get("Origin")
        if origin and origin in self.svc.allowed_origins:
            return {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true",
                    "Access-Control-Allow-Headers": f"Content-Type, {CSRF_HEADER}",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Vary": "Origin"}
        return {}

    def _cookie(self, token: str) -> str:
        cross_site = bool(self.svc.allowed_origins)
        return cookie_header(token, self.svc.secure_cookies or cross_site, samesite="None" if cross_site else "Strict")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        for k, v in self._cors_headers().items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        for k, v in self._cors_headers().items():
            self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: dict[str, Any], extra: dict[str, str] | None = None) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json", {"Cache-Control": "no-store", **(extra or {})})

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _static(self, name: str) -> None:
        self._send(200, (STATIC / name).read_bytes(), "text/html; charset=utf-8", {"Cache-Control": "no-store"})

    def _query(self) -> dict[str, str]:
        from urllib.parse import parse_qs, urlsplit

        q = parse_qs(urlsplit(self.path).query)
        return {k: v[0] for k, v in q.items() if v}

    # ---------------------------------------------------------------- GET
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/login":
            return self._static("login.html")
        if path == "/healthz":
            return self._json(200, {"ok": True})
        rt = self._runtime()
        if rt is None:
            if path == "/":
                return self._redirect("/login")
            return self._json(401, {"error": "sign in required"})
        if path == "/":
            return self._static("app.html")
        if path == "/api/state":
            return self._json(200, rt.state())
        if path == "/api/runs":
            return self._json(200, {"runs": self.svc.store.runs_for_user(rt.user.id)})
        if path == "/api/events":
            return self._sse(rt)
        if path == "/api/frame.png":
            return self._frame(rt, self._query().get("phone"))
        if path.startswith("/runs/"):
            parts = path[len("/runs/"):].split("/", 2)   # <slot>/<run dir>/<file...>
            if len(parts) != 3:
                return self._json(404, {"error": "not found"})
            target = rt.run_file(f"{parts[0]}/{parts[1]}", parts[2])
            if target is None:
                return self._json(404, {"error": "not found"})
            ctype = {"png": "image/png", "html": "text/html; charset=utf-8", "json": "application/json",
                     "jsonl": "text/plain; charset=utf-8"}.get(target.suffix[1:], "application/octet-stream")
            return self._send(200, target.read_bytes(), ctype, {"Cache-Control": "private, max-age=3600"})
        self._json(404, {"error": f"no route GET {path}"})

    def _frame(self, rt: UserRuntime, slot: str | None) -> None:
        try:
            png = rt.frame(slot)
        except LookupError as exc:
            return self._json(404, {"error": str(exc)})
        except PermissionError as exc:
            return self._json(403, {"error": str(exc)})
        except (CloudError, ValueError):
            return self._json(404, {"error": "no frame"})
        self._send(200, png, "image/png", {"Cache-Control": "no-store"})

    def _sse(self, rt: UserRuntime) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        q = rt.hub.subscribe()
        try:
            self._sse_write({"kind": "hello", "history": list(rt.hub.history), "state": rt.state()})
            while True:
                try:
                    event = q.get(timeout=SSE_KEEPALIVE_S)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._sse_write(event)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            rt.hub.unsubscribe(q)

    def _sse_write(self, event: dict[str, Any]) -> None:
        self.wfile.write(b"data: " + json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n\n")
        self.wfile.flush()

    # ---------------------------------------------------------------- POST
    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if self.headers.get(CSRF_HEADER) != "1":
            return self._json(403, {"error": f"missing {CSRF_HEADER} header"})
        try:
            body = self._body()
        except ValueError as exc:
            return self._json(413, {"error": str(exc)})
        ip = self._ip()
        try:
            if path == "/api/auth/signup":
                login = self.svc.auth.signup(str(body.get("email", "")), str(body.get("password", "")),
                                             (body.get("invite") or None), ip)
                return self._json(200, {"ok": True, "email": login.user.email, "is_admin": login.user.is_admin},
                                  {"Set-Cookie": self._cookie(login.token)})
            if path == "/api/auth/login":
                login = self.svc.auth.login(str(body.get("email", "")), str(body.get("password", "")), ip)
                return self._json(200, {"ok": True, "email": login.user.email},
                                  {"Set-Cookie": self._cookie(login.token)})
            if path == "/api/auth/logout":
                self.svc.auth.logout(self._cookie_token())
                return self._json(200, {"ok": True}, {"Set-Cookie": clear_cookie_header()})
        except AuthError as exc:
            return self._json(400, {"error": str(exc)})

        rt = self._runtime()
        if rt is None:
            return self._json(401, {"error": "sign in required"})
        try:
            if path == "/api/keys":
                h = self.svc.keys.set(rt.user.id, str(body.get("provider", "")), str(body.get("value", "")))
                rt.reset_clients()
                return self._json(200, {"ok": True, "hint": h, "keys": self.svc.keys.summary(rt.user.id)})
            if path == "/api/keys/delete":
                self.svc.keys.delete(rt.user.id, str(body.get("provider", "")))
                rt.reset_clients()
                return self._json(200, {"ok": True, "keys": self.svc.keys.summary(rt.user.id)})
            if path == "/api/admin/invite":
                return self._json(200, {"ok": True, "invite": self.svc.auth.create_invite(rt.user)})
            slot = body.get("phone") or None
            if path == "/api/session/start":
                rt.start_session(slot, body.get("timeout_seconds"))
            elif path == "/api/session/attach":
                rt.attach_session(slot, str(body.get("session_id", "")).strip())
            elif path == "/api/session/end":
                rt.end_session(slot)
            elif path == "/api/task":
                routed = rt.run_task(str(body.get("task", "")), slot)
                return self._json(200, {"ok": True, "routed": routed, **rt.state()})
            elif path == "/api/task/stop":
                rt.stop_task(slot)
            else:
                return self._json(404, {"error": f"no route POST {path}"})
        except (ConfigError, QuotaError, RuntimeError, ValueError, SandboxError) as exc:
            return self._json(409, {"error": str(exc)})
        except PermissionError as exc:
            return self._json(403, {"error": str(exc)})
        except AuthError as exc:
            return self._json(403, {"error": str(exc)})
        except CloudError:
            return self._json(502, {"error": "phone service error"})
        self._json(200, {"ok": True, **rt.state()})


def make_server(svc: Service, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"svc": svc})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve(data_dir: Path, host: str = "0.0.0.0", port: int = 8080, secure_cookies: bool = False,
          trust_proxy: bool = False, log: Callable[[str], None] = print, sandbox: str | None = None) -> None:
    svc = Service(data_dir, secure_cookies=secure_cookies, trust_proxy=trust_proxy, log=log, sandbox=sandbox)
    server = make_server(svc, host, port)
    log(f"PhonePilot service on http://{host}:{server.server_address[1]}/  (data: {data_dir.resolve()})")
    if svc.store.user_count() == 0:
        log("no accounts yet: the first signup becomes admin (no invite needed)")
    sweeper = threading.Thread(target=_sweep_loop, args=(svc,), daemon=True)
    sweeper.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        log("stopping sandboxes and ending their phones…")
        svc.registry.shutdown()
        svc.store.close()


def _sweep_loop(svc: Service) -> None:
    while True:
        time.sleep(60)
        try:
            svc.registry.sweep()
        except Exception:  # noqa: BLE001
            pass
