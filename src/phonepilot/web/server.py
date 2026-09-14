"""Local browser UI for PhonePilot.

    phonepilot web            # http://127.0.0.1:8765

Three panels: a live view of the cloud phone (polled from the Cloud API's
`GET /sessions/{sid}/frame.png`), a chat box for natural-language tasks, and a
streaming log of every agent step (reasoning, action, verified outcome, the
exact marked screenshot the model saw).

Everything phone-related goes through `PhoneHarnessClient`; the page talks
only to this local server. No framework: stdlib `http.server` plus
Server-Sent Events, so the project stays dependency-light.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from ..agent import Agent, AgentConfig, new_trace
from ..brain.base import Brain
from ..cloud import CloudError, PhoneHarnessClient, Session
from ..sessions import acquire, make_device, release
from ..trace import StepRecord

INDEX_HTML = Path(__file__).with_name("index.html")
ACCOUNT_CACHE_S = 30.0
SSE_KEEPALIVE_S = 15.0
EVENT_HISTORY = 300


class Hub:
    """Fan-out of JSON events to every connected browser tab."""

    def __init__(self) -> None:
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self.history: deque[dict[str, Any]] = deque(maxlen=EVENT_HISTORY)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, kind: str, **data: Any) -> dict[str, Any]:
        event = {"kind": kind, "t": time.time(), **data}
        self.history.append(event)
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            q.put(event)
        return event


class AppState:
    """Owns the cloud client, one phone, and at most one running agent."""

    def __init__(self, client: PhoneHarnessClient, brain: Brain, runs_dir: Path, max_steps: int = 25,
                 log: Callable[[str], None] | None = None, transport: str = "http"):
        self.client = client
        self.brain = brain
        self.runs_dir = runs_dir
        self.max_steps = max_steps
        self.terminal_log = log
        self.transport = transport
        self._close_transport: Callable[[], None] = lambda: None
        self.hub = Hub()
        self.lock = threading.Lock()
        self.session: Session | None = None
        self.device: Any = None  # Device or AdbDevice, same surface
        self.created_here = False
        self.status = "no_phone"  # no_phone | starting | ready | running | ending
        self.task: str | None = None
        self.agent: Agent | None = None
        self.run_dir: Path | None = None
        self._account: dict[str, Any] = {}
        self._account_t = 0.0
        self._end_requested = False

    # --------------------------------------------------------------- state
    def _expire_if_needed(self) -> None:
        """A phone that hit its Phone Harness deadline is gone; reflect that instead of showing 'ready'."""
        left = self.device.seconds_left() if self.device else None
        if self.status in ("ready", "running") and left is not None and left <= 0:
            try:
                self._close_transport()
            except Exception:  # noqa: BLE001
                pass
            self._close_transport = lambda: None
            self._log(f"session {self.session.id if self.session else '?'} reached its deadline and was closed by the service")
            self.session, self.device, self.task, self.agent = None, None, None, None
            self.status = "no_phone"

    def state(self) -> dict[str, Any]:
        self._expire_if_needed()
        acct = self._account_cached()
        s = self.session
        return {
            "status": self.status,
            "session_id": s.id if s else None,
            "session_state": s.state if s else None,
            "screen": list(s.screen) if s and s.screen else None,
            "seconds_left": int(self.device.seconds_left() or 0) if self.device else None,
            "task": self.task,
            "brain": f"{self.brain.name}/{self.brain.model}",
            "transport": self.transport,
            "balance_cents": acct.get("balance_cents"),
            "price_cents_per_minute": acct.get("price_cents_per_minute"),
            "run_dir": self.run_dir.name if self.run_dir else None,
        }

    def _account_cached(self) -> dict[str, Any]:
        if time.monotonic() - self._account_t > ACCOUNT_CACHE_S:
            try:
                self._account = self.client.account()
            except CloudError:
                pass
            self._account_t = time.monotonic()
        return self._account

    def _log(self, text: str) -> None:
        if self.terminal_log:
            self.terminal_log(text)
        self.hub.publish("log", text=text)

    def _push_state(self) -> None:
        self.hub.publish("state", **self.state())

    # ------------------------------------------------------------- session
    def start_session(self, timeout_seconds: int) -> None:
        with self.lock:
            if self.status != "no_phone":
                raise RuntimeError(f"cannot start a phone while status is {self.status}")
            self.status = "starting"
        self._push_state()
        threading.Thread(target=self._start_session_bg, args=(None, timeout_seconds), daemon=True).start()

    def attach_session(self, sid: str) -> None:
        with self.lock:
            if self.status != "no_phone":
                raise RuntimeError(f"cannot attach while status is {self.status}")
            self.status = "starting"
        self._push_state()
        threading.Thread(target=self._start_session_bg, args=(sid, 0), daemon=True).start()

    def _start_session_bg(self, sid: str | None, timeout_seconds: int) -> None:
        try:
            lease = acquire(self.client, sid, timeout_seconds, self._log)
            if self._end_requested:
                # the user (or the orchestrator) gave up while the phone was still provisioning
                self._log("phone became ready after an end request; releasing it")
                if lease.created_here:
                    release(self.client, lease.session.id, self._log)
                self._end_requested = False
                self.status = "no_phone"
                self._push_state()
                return
            self.session = lease.session
            self.created_here = lease.created_here
            try:
                self.device, self._close_transport = make_device(self.client, lease.session, self.transport, self._log)
            except Exception:
                # transport setup failed (e.g. adb registration); never leave a billed phone behind
                if lease.created_here:
                    self._log(f"transport setup failed; releasing session {lease.session.id}")
                    release(self.client, lease.session.id, self._log)
                self.session = None
                raise
            self.status = "ready"
            self._account_t = 0.0
        except Exception as exc:  # noqa: BLE001 — surface to the UI
            self._log(f"phone failed to start: {exc}")
            self.status = "no_phone"
        self._push_state()

    def end_session(self) -> None:
        with self.lock:
            if self.status in ("no_phone", "ending"):
                return
            if self.status == "starting":
                # provisioning cannot be interrupted; flag it so the phone is released the moment it is ready
                self._end_requested = True
                self.status = "ending"
                self._push_state()
                return
            if self.agent:
                self.agent.cancel()
            self.status = "ending"
        self._push_state()
        threading.Thread(target=self._end_session_bg, daemon=True).start()

    def _end_session_bg(self) -> None:
        try:
            self._close_transport()
        except Exception as exc:  # noqa: BLE001
            self._log(f"transport close failed: {exc}")
        self._close_transport = lambda: None
        if self.session:
            try:
                release(self.client, self.session.id, self._log)
            except CloudError as exc:
                self._log(f"end failed: {exc}")
        self.session = None
        self.device = None
        self.task = None
        self.agent = None
        self.status = "no_phone"
        self._account_t = 0.0
        self._push_state()

    # ---------------------------------------------------------------- task
    def run_task(self, task: str) -> None:
        task = task.strip()
        if not task:
            raise ValueError("empty task")
        with self.lock:
            if self.status != "ready" or not self.device:
                raise RuntimeError("phone is not ready" if self.status != "running" else "a task is already running")
            self.status = "running"
            self.task = task
        self.hub.publish("task", task=task)
        self._push_state()
        threading.Thread(target=self._run_task_bg, args=(task,), daemon=True).start()

    def _run_task_bg(self, task: str) -> None:
        assert self.device and self.session
        price = self._account_cached().get("price_cents_per_minute")
        trace = new_trace(task, self.session.id, self.brain, self.runs_dir, price)
        self.run_dir = trace.dir
        self.agent = Agent(self.device, self.brain, trace, AgentConfig(max_steps=self.max_steps),
                           log=self._log, on_step=self._on_step)
        outcome = self.agent.run(task)
        self.hub.publish(
            "outcome", success=outcome.success, summary=outcome.summary, result=outcome.result,
            steps=outcome.steps, error=outcome.error, report_url=f"/runs/{trace.dir.name}/report.html",
        )
        self.agent = None
        self.task = None
        if self.status == "running":
            self.status = "ready"
        self._push_state()

    def stop_task(self) -> None:
        if self.agent:
            self.agent.cancel()
            self._log("stop requested; finishing the current step…")

    def _on_step(self, rec: StepRecord) -> None:
        assert self.run_dir
        base = f"/runs/{self.run_dir.name}/"
        self.hub.publish(
            "step", step=rec.step, app=rec.app, elements=rec.elements, thought=rec.thought, action=rec.action,
            args=rec.args, feedback=rec.feedback, marked_url=base + rec.marked, shot_url=base + rec.screenshot,
            latency_llm_s=rec.latency_llm_s, latency_act_s=rec.latency_act_s,
        )

    # ------------------------------------------------------------ shutdown
    server: Any = None  # set by serve()/serve_sandbox()

    def shutdown(self) -> None:
        """End the phone (if we own it) and stop the HTTP server. Used by the sandbox orchestrator."""
        if self.status == "starting":
            self._end_requested = True  # _start_session_bg releases the phone when provisioning returns
            for _ in range(900):
                time.sleep(0.2)
                if self.status != "ending" and self.status != "starting":
                    break
        if self.agent:
            self.agent.cancel()
        try:
            self._close_transport()
        except Exception:  # noqa: BLE001
            pass
        if self.session and self.created_here:
            try:
                release(self.client, self.session.id, self._log)
            except CloudError as exc:
                self._log(f"end failed: {exc}")
        self.session = None
        self.status = "no_phone"
        if self.server is not None:
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    # --------------------------------------------------------------- frame
    def frame(self) -> bytes:
        if not self.session or self.status in ("no_phone", "starting", "ending"):
            raise LookupError("no ready phone")
        return self.client.snapshot(self.session.id)


class Handler(BaseHTTPRequestHandler):
    app: AppState  # set by serve()
    token: str | None = None  # sandbox mode: every request must carry this bearer token
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the terminal for agent logs
        return

    def _authorized(self) -> bool:
        if not self.token:
            return True
        return self.headers.get("Authorization") == f"Bearer {self.token}"

    # ----------------------------------------------------------------- GET
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            return self._json(200, {"ok": True, "status": self.app.status})
        if not self._authorized():
            return self._json(401, {"error": "sandbox token required"})
        if path == "/":
            return self._send(200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/state":
            return self._json(200, self.app.state())
        if path == "/api/events":
            return self._sse()
        if path == "/api/frame.png":
            return self._frame()
        if path.startswith("/runs/"):
            return self._run_file(path[len("/runs/"):])
        self._json(404, {"error": f"no route GET {path}"})

    def _frame(self) -> None:
        try:
            png = self.app.frame()
        except LookupError as exc:
            return self._json(404, {"error": str(exc)})
        except CloudError as exc:
            return self._json(502 if exc.status >= 500 else exc.status or 502, {"error": str(exc)})
        self._send(200, png, "image/png", {"Cache-Control": "no-store"})

    def _run_file(self, rel: str) -> None:
        root = self.app.runs_dir.resolve()
        target = (root / rel).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return self._json(404, {"error": "not found"})
        ctype = {"png": "image/png", "html": "text/html; charset=utf-8", "json": "application/json",
                 "jsonl": "text/plain; charset=utf-8", "mp4": "video/mp4"}.get(target.suffix[1:], "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = self.app.hub.subscribe()
        try:
            self._sse_write({"kind": "hello", "history": list(self.app.hub.history), "state": self.app.state()})
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
            self.app.hub.unsubscribe(q)

    def _sse_write(self, event: dict[str, Any]) -> None:
        self.wfile.write(b"data: " + json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n\n")
        self.wfile.flush()

    # ---------------------------------------------------------------- POST
    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if not self._authorized():
            return self._json(401, {"error": "sandbox token required"})
        body = self._body()
        try:
            if path == "/api/shutdown":
                self._json(200, {"ok": True})
                threading.Thread(target=self.app.shutdown, daemon=True).start()
                return
            if path == "/api/session/start":
                self.app.start_session(int(body.get("timeout_seconds") or 900))
            elif path == "/api/session/attach":
                self.app.attach_session(str(body.get("session_id", "")).strip())
            elif path == "/api/session/end":
                self.app.end_session()
            elif path == "/api/task":
                self.app.run_task(str(body.get("task", "")))
            elif path == "/api/task/stop":
                self.app.stop_task()
            else:
                return self._json(404, {"error": f"no route POST {path}"})
        except (RuntimeError, ValueError) as exc:
            return self._json(409, {"error": str(exc)})
        except CloudError as exc:
            return self._json(502, {"error": str(exc)})
        self._json(200, {"ok": True, **self.app.state()})

    # ------------------------------------------------------------- helpers
    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _json(self, status: int, data: dict[str, Any]) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json")

    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


def make_server(app: AppState, host: str = "127.0.0.1", port: int = 8765, token: str | None = None) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app, "token": token})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    app.server = server
    return server


def serve_sandbox(client: PhoneHarnessClient, brain: Brain, runs_dir: Path, host: str, port: int, token: str,
                  max_steps: int = 25, transport: str = "http", log: Callable[[str], None] = print,
                  parent_pid: int | None = None) -> None:
    """One phone, one agent, one bearer token; no browser. This is what runs inside a sandbox container."""
    app = AppState(client, brain, runs_dir, max_steps, log=log, transport=transport)
    server = make_server(app, host, port, token=token)
    log(f"sandbox agent server on http://{host}:{server.server_address[1]}/ (token-protected)")
    if parent_pid:
        threading.Thread(target=_watch_parent, args=(parent_pid, app, log), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if app.session and app.created_here:
            release(client, app.session.id, log)


def _watch_parent(pid: int, app: "AppState", log: Callable[[str], None]) -> None:
    """If the web tier that launched us dies, end our phone and exit instead of running (and billing) on."""
    while process_alive(pid):
        time.sleep(3.0)
    log(f"parent process {pid} is gone; ending the phone and shutting down")
    app.shutdown()


def process_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) != 0  # WAIT_OBJECT_0 == exited
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def serve(
    client: PhoneHarnessClient,
    brain: Brain,
    runs_dir: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    session_id: str | None = None,
    max_steps: int = 25,
    open_browser: bool = True,
    log: Callable[[str], None] = print,
    transport: str = "http",
) -> None:
    app = AppState(client, brain, runs_dir, max_steps, log=log, transport=transport)
    server = make_server(app, host, port)
    url = f"http://{host}:{server.server_address[1]}/"
    log(f"PhonePilot web UI at {url}  (Ctrl-C to quit)")
    if session_id:
        app.attach_session(session_id)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if app.session and app.created_here:
            log("ending the phone this UI started…")
            release(client, app.session.id, log)
