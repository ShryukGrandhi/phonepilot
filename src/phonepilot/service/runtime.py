"""Per-user runtime for the multi-user service: one sandbox per phone session.

The web tier never holds a phone, an agent, or a decrypted key for longer than it
takes to launch a sandbox. Each signed-in user gets a `UserRuntime` that:

- resolves *that user's* keys (or the operator's pool) and hands them to a fresh
  sandbox (process or container) as environment variables;
- proxies phone/task/frame calls to that sandbox with a per-sandbox bearer token;
- tails the sandbox's event stream in a thread and republishes it into the user's
  own Hub (so browser tabs subscribe locally) while recording phones/runs in the
  store for ownership checks and metering;
- enforces quotas (phones per user, phone-minutes per day, steps, session length).

Isolation therefore has three layers: the HTTP session cookie → user, the store
rows that name an owner for every phone and run, and the OS boundary of the
sandbox itself (container or separate process with its own env, adb identity and
working directory).
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..web.server import Hub
from .sandbox import Sandbox, SandboxError, SandboxSpec
from .secrets import SecretBox, hint
from .store import Store, User

PROVIDERS = ("phone_harness", "gemini", "anthropic")
KEY_PREFIX = {"phone_harness": "pck_", "gemini": "AIza", "anthropic": "sk-ant-"}
IDLE_SANDBOX_S = 600  # a sandbox with no phone is stopped after this


class ConfigError(Exception):
    """The user has not configured something they need (shown verbatim in the UI)."""


class QuotaError(Exception):
    pass


@dataclass(frozen=True)
class Limits:
    max_phones_per_user: int = int(os.environ.get("PHONEPILOT_MAX_PHONES_PER_USER", "1"))
    daily_phone_minutes: float = float(os.environ.get("PHONEPILOT_DAILY_PHONE_MINUTES", "90"))
    max_steps: int = int(os.environ.get("PHONEPILOT_MAX_STEPS", "25"))
    max_session_timeout_s: int = int(os.environ.get("PHONEPILOT_MAX_SESSION_TIMEOUT", "1800"))
    default_session_timeout_s: int = int(os.environ.get("PHONEPILOT_DEFAULT_SESSION_TIMEOUT", "900"))
    transport: str = os.environ.get("PHONEPILOT_TRANSPORT", "http")


@dataclass(frozen=True)
class Pool:
    """Operator-provided keys shared by all users (optional). Users' own keys take precedence."""

    phone_harness: str | None = os.environ.get("PHONEPILOT_POOL_PHONE_KEY") or None
    gemini: str | None = os.environ.get("PHONEPILOT_POOL_GEMINI_KEY") or None
    anthropic: str | None = os.environ.get("PHONEPILOT_POOL_ANTHROPIC_KEY") or None


class KeyResolver:
    def __init__(self, store: Store, box: SecretBox, pool: Pool):
        self.store, self.box, self.pool = store, box, pool

    def set(self, uid: str, provider: str, value: str) -> str:
        value = value.strip()
        if provider not in PROVIDERS:
            raise ValueError("unknown provider")
        if not value.startswith(KEY_PREFIX[provider]) or len(value) < 20:
            raise ValueError(f"that does not look like a {provider} key (expected it to start with {KEY_PREFIX[provider]!r})")
        self.store.set_key(uid, provider, self.box.seal(value), hint(value))
        self.store.audit(uid, "key.set", provider)
        return hint(value)

    def delete(self, uid: str, provider: str) -> None:
        self.store.delete_key(uid, provider)
        self.store.audit(uid, "key.delete", provider)

    def _own(self, uid: str, provider: str) -> str | None:
        enc = self.store.get_key(uid, provider)
        return self.box.open(enc) if enc else None

    def phone_key(self, uid: str) -> tuple[str, str]:
        own = self._own(uid, "phone_harness")
        if own:
            return own, "own"
        if self.pool.phone_harness:
            return self.pool.phone_harness, "pool"
        raise ConfigError("Add your Phone Harness API key in Settings before starting a phone.")

    def model(self, uid: str) -> tuple[str, str, str]:
        for provider in ("anthropic", "gemini"):
            own = self._own(uid, provider)
            if own:
                return provider, own, "own"
        for provider, key in (("anthropic", self.pool.anthropic), ("gemini", self.pool.gemini)):
            if key:
                return provider, key, "pool"
        raise ConfigError("Add a Gemini or Anthropic API key in Settings; the agent needs a model.")

    def sandbox_env(self, uid: str) -> tuple[dict[str, str], str]:
        """Environment for a sandbox: exactly this user's keys, nothing else. Returns (env, description)."""
        phone_key, phone_src = self.phone_key(uid)
        provider, model_key, model_src = self.model(uid)
        env = {"PHONE_HARNESS_API_KEY": phone_key, "PHONEPILOT_BRAIN": provider,
               ("ANTHROPIC_API_KEY" if provider == "anthropic" else "GEMINI_API_KEY"): model_key}
        return env, f"phone key: {phone_src} · model: {provider} ({model_src} key)"

    def summary(self, uid: str) -> dict[str, Any]:
        hints = self.store.key_hints(uid)
        return {p: {"own": hints.get(p), "pool": bool(getattr(self.pool, p))} for p in PROVIDERS}


class UserRuntime:
    def __init__(self, user: User, store: Store, keys: KeyResolver, limits: Limits, data_dir: Path, backend: Any,
                 log: Callable[[str], None] | None = None):
        self.user = user
        self.store = store
        self.keys = keys
        self.limits = limits
        self.backend = backend
        self.runs_dir = data_dir / "users" / user.id / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.terminal_log = log
        self.hub = Hub()
        self.lock = threading.Lock()
        self.sandbox: Sandbox | None = None
        self.remote: dict[str, Any] = {}      # last state reported by the sandbox
        self.status = "no_phone"              # no_phone | starting | ready | running | ending
        self.session_id: str | None = None
        self.run_id: str | None = None
        self.last_used = time.time()
        self._watcher: threading.Thread | None = None
        self._start_pending = False  # we asked the sandbox to start a phone; ignore stale no_phone snapshots
        self.last_error: str | None = None

    # -------------------------------------------------------------- state
    def state(self) -> dict[str, Any]:
        self.last_used = time.time()
        r = self.remote
        return {
            "status": self.status,
            "session_id": self.session_id,
            "screen": r.get("screen"),
            "seconds_left": r.get("seconds_left"),
            "task": r.get("task"),
            "brain": r.get("brain"),
            "transport": self.limits.transport,
            "run_dir": r.get("run_dir"),
            "sandbox": {"backend": self.sandbox.backend, "id": self.sandbox.id} if self.sandbox else None,
            "last_error": self.last_error,
            "user": {"email": self.user.email, "is_admin": self.user.is_admin},
            "keys": self.keys.summary(self.user.id),
            "quota": {
                "phone_minutes_used_today": round(self.store.phone_minutes_today(self.user.id), 1),
                "daily_phone_minutes": self.limits.daily_phone_minutes,
                "max_steps": self.limits.max_steps,
                "max_session_timeout_s": self.limits.max_session_timeout_s,
            },
        }

    def _log(self, text: str) -> None:
        if self.terminal_log:
            self.terminal_log(f"[{self.user.email}] {text}")
        self.hub.publish("log", text=text)

    def _push_state(self) -> None:
        self.hub.publish("state", **self.state())

    # ------------------------------------------------------------ sandbox
    def _ensure_sandbox(self) -> Sandbox:
        if self.sandbox is not None:
            return self.sandbox
        env, desc = self.keys.sandbox_env(self.user.id)
        spec = SandboxSpec(user_id=self.user.id, runs_dir=self.runs_dir, env=env,
                           transport=self.limits.transport, max_steps=self.limits.max_steps)
        sb = self.backend.start(spec)
        self.sandbox = sb
        self._log(f"sandbox {sb.id} up ({sb.backend}); {desc}")
        self.store.audit(self.user.id, "sandbox.start", f"{sb.backend}:{sb.id}")
        self._watcher = threading.Thread(target=self._watch, args=(sb,), daemon=True)
        self._watcher.start()
        return sb

    def _stop_sandbox(self) -> None:
        sb, self.sandbox = self.sandbox, None
        if sb is None:
            return
        try:
            self.backend.stop(sb)
        except Exception as exc:  # noqa: BLE001
            self._log(f"sandbox stop failed: {exc}")
        self.store.audit(self.user.id, "sandbox.stop", sb.id)
        self.remote = {}
        self.session_id = None

    def _watch(self, sb: Sandbox) -> None:
        """Tail the sandbox's SSE stream, mirror events into the user's hub, keep the store in sync."""
        try:
            resp = sb.open_stream("/api/events")
            for raw in resp:
                if not raw.startswith(b"data: "):
                    continue
                event = json.loads(raw[6:])
                if event.get("kind") == "hello":
                    self._absorb_state(event.get("state") or {})
                    continue
                if event.get("kind") == "state":
                    self._absorb_state(event)
                    self.hub.publish("state", **self.state())
                    continue
                if event.get("kind") == "log" and str(event.get("text", "")).startswith("phone failed to start"):
                    self.last_error = str(event["text"])
                    self._start_pending = False
                if event.get("kind") == "task":
                    self.run_id = None
                if event.get("kind") == "step" and self.run_id is None and self.session_id:
                    run_dir = event["marked_url"].split("/")[2]
                    self.run_id = self.store.add_run(self.user.id, self.session_id, self.remote.get("task") or "", run_dir)
                if event.get("kind") == "outcome" and self.run_id:
                    self.store.finish_run(self.run_id, event.get("success"), event.get("summary") or "",
                                          event.get("result"), int(event.get("steps") or 0))
                    self.run_id = None
                self.hub.publish(event.get("kind", "log"), **{k: v for k, v in event.items() if k not in ("kind", "t")})
        except Exception as exc:  # noqa: BLE001
            if self.sandbox is sb:
                self._log(f"sandbox stream ended: {exc}")
                self.status = "no_phone"
                self._stop_sandbox()
                self._push_state()

    def _absorb_state(self, remote: dict[str, Any]) -> None:
        rs = remote.get("status")
        if rs == "no_phone" and self._start_pending:
            # stale snapshot from before the sandbox processed our start request; ignore
            return
        if rs in ("starting", "ready", "running"):
            self._start_pending = False
        self.remote = {k: remote.get(k) for k in ("status", "session_id", "screen", "seconds_left", "task", "brain", "run_dir")}
        new_sid = remote.get("session_id")
        if new_sid and new_sid != self.session_id:
            self.session_id = new_sid
            if self.store.phone_owner(new_sid) is None:
                self.store.add_phone(new_sid, self.user.id, None, self.limits.transport)
                self.store.audit(self.user.id, "phone.start", new_sid)
        rs = remote.get("status")
        if rs == "ready" and self.session_id and self.status in ("starting", "running"):
            self.store.mark_phone_ready(self.session_id, time.time() + (remote.get("seconds_left") or 0))
        if rs in ("ready", "running", "starting"):
            self.status = rs
        elif rs == "no_phone" and self.status != "ending":
            if self.session_id:
                self.store.end_phone(self.session_id)
            self.session_id = None
            self.status = "no_phone"
            self._start_pending = False

    # ------------------------------------------------------------ session
    def start_session(self, timeout_seconds: int | None) -> None:
        timeout = min(int(timeout_seconds or self.limits.default_session_timeout_s), self.limits.max_session_timeout_s)
        self.last_error = None
        with self.lock:
            if self.status != "no_phone":
                raise RuntimeError(f"cannot start a phone while status is {self.status}")
            if len(self.store.open_phones(self.user.id)) >= self.limits.max_phones_per_user:
                raise QuotaError("you already have a phone open; end it first")
            if self.store.phone_minutes_today(self.user.id) >= self.limits.daily_phone_minutes:
                raise QuotaError("daily phone-minute quota reached")
            self.keys.sandbox_env(self.user.id)  # raises ConfigError early, before any sandbox is launched
            self.status = "starting"
        self._push_state()
        threading.Thread(target=self._start_bg, args=("/api/session/start", {"timeout_seconds": timeout}), daemon=True).start()

    def attach_session(self, sid: str) -> None:
        with self.lock:
            if self.status != "no_phone":
                raise RuntimeError(f"cannot attach while status is {self.status}")
            if self.store.phone_owner(sid) != self.user.id:
                raise PermissionError("that session is not yours")
            self.keys.sandbox_env(self.user.id)
            self.status = "starting"
        self._push_state()
        threading.Thread(target=self._start_bg, args=("/api/session/attach", {"session_id": sid}), daemon=True).start()

    def _start_bg(self, path: str, body: dict[str, Any]) -> None:
        self._start_pending = True
        try:
            sb = self._ensure_sandbox()
            status, data, _ = sb.request("POST", path, body)
            if status != 200:
                raise SandboxError(json.loads(data or b"{}").get("error", f"sandbox answered {status}"))
        except Exception as exc:  # noqa: BLE001
            self._log(f"phone failed to start: {exc}")
            self._start_pending = False
            self.status = "no_phone"
            self._push_state()

    def end_session(self) -> None:
        with self.lock:
            if self.status in ("no_phone", "ending"):
                if self.sandbox and self.status == "no_phone":
                    threading.Thread(target=self._stop_sandbox, daemon=True).start()
                return
            self.status = "ending"
        self._push_state()
        threading.Thread(target=self._end_bg, daemon=True).start()

    def _end_bg(self) -> None:
        sid = self.session_id
        if self.sandbox:
            try:
                self._start_pending = False
                self.sandbox.request("POST", "/api/session/end", {})
                # wait for the sandbox to report no_phone (release() done); a phone that is still
                # provisioning is ended by the sandbox as soon as provisioning returns (~2 min)
                for _ in range(900):
                    time.sleep(0.2)
                    if self.remote.get("status") == "no_phone":
                        break
            except SandboxError as exc:
                self._log(f"end failed: {exc}")
        if sid:
            self.store.end_phone(sid)
            self.store.audit(self.user.id, "phone.end", sid)
        self._stop_sandbox()
        self.status = "no_phone"
        self._push_state()

    # --------------------------------------------------------------- task
    def run_task(self, task: str) -> None:
        task = task.strip()
        if not task:
            raise ValueError("empty task")
        if len(task) > 2000:
            raise ValueError("task too long")
        with self.lock:
            if self.status == "running":
                raise RuntimeError("a task is already running")
            if self.status != "ready" or not self.sandbox:
                raise RuntimeError("phone is not ready")
            status, data, _ = self.sandbox.request("POST", "/api/task", {"task": task})
            if status != 200:
                raise RuntimeError(json.loads(data or b"{}").get("error", f"sandbox answered {status}"))
            self.status = "running"
            self.remote["task"] = task
        self.store.audit(self.user.id, "task.start", task[:120])
        self._push_state()

    def stop_task(self) -> None:
        if self.sandbox:
            self.sandbox.request("POST", "/api/task/stop", {})

    # -------------------------------------------------------------- frame
    def frame(self) -> bytes:
        if not self.sandbox or self.status in ("no_phone", "starting", "ending"):
            raise LookupError("no ready phone")
        if self.session_id and self.store.phone_owner(self.session_id) != self.user.id:
            raise PermissionError("not your phone")
        status, data, ctype = self.sandbox.request("GET", "/api/frame.png", timeout=30)
        if status != 200 or not ctype.startswith("image/"):
            raise LookupError("no frame available")
        return data

    def run_file(self, run_dir_name: str, rel: str) -> Path | None:
        if not self.store.run_dir_owned(self.user.id, run_dir_name):
            return None
        root = self.runs_dir.resolve()
        target = (root / run_dir_name / rel).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return None
        return target

    def reset_clients(self) -> None:
        """After a key change: a sandbox without a phone is stopped so the next phone uses the new keys."""
        if self.status == "no_phone" and self.sandbox:
            self._stop_sandbox()

    # ------------------------------------------------------------ cleanup
    def idle_cleanup(self) -> None:
        if self.sandbox and self.status == "no_phone" and time.time() - self.last_used > IDLE_SANDBOX_S:
            self._stop_sandbox()


class Registry:
    """uid -> UserRuntime, created lazily."""

    def __init__(self, store: Store, keys: KeyResolver, limits: Limits, data_dir: Path, backend: Any,
                 log: Callable[[str], None] | None = None):
        self.store, self.keys, self.limits, self.data_dir, self.backend, self.log = store, keys, limits, data_dir, backend, log
        self._by_uid: dict[str, UserRuntime] = {}
        self._lock = threading.Lock()

    def get(self, user: User) -> UserRuntime:
        with self._lock:
            rt = self._by_uid.get(user.id)
            if rt is None:
                rt = UserRuntime(user, self.store, self.keys, self.limits, self.data_dir, self.backend, self.log)
                self._by_uid[user.id] = rt
            rt.user = user
            return rt

    def sweep(self) -> None:
        with self._lock:
            runtimes = list(self._by_uid.values())
        for rt in runtimes:
            rt.idle_cleanup()

    def shutdown(self) -> None:
        with self._lock:
            runtimes = list(self._by_uid.values())
        for rt in runtimes:
            try:
                if rt.session_id:
                    rt._end_bg()
                else:
                    rt._stop_sandbox()
            except Exception:  # noqa: BLE001
                pass
