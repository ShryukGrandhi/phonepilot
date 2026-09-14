"""Per-user runtime: each signed-in user gets their own clients, phone, agent, and event stream.

Isolation model
- A `UserRuntime` is keyed by user id. It builds its Phone Harness client and model
  brain from that user's own encrypted keys (or the operator's pooled keys, if
  configured) and never touches another user's objects.
- A phone (Phone Harness session) is recorded in the store with its owner at
  creation. Attaching, streaming frames, running tasks, and reading run files all
  check ownership against the store, not just against in-memory state.
- Run folders live under data/users/<uid>/runs/, so file paths cannot cross users
  even before the ownership check.
- Quotas (phones per user, phone-minutes per day, steps per run) are enforced here.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..agent import Agent, AgentConfig, new_trace
from ..brain import make_brain
from ..brain.base import Brain
from ..cloud import CloudError, PhoneHarnessClient, Session
from ..sessions import acquire, make_device, release
from ..trace import StepRecord
from ..web.server import Hub
from .secrets import SecretBox, hint
from .store import Store, User

PROVIDERS = ("phone_harness", "gemini", "anthropic")
KEY_PREFIX = {"phone_harness": "pck_", "gemini": "AIza", "anthropic": "sk-ant-"}


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

    def summary(self, uid: str) -> dict[str, Any]:
        hints = self.store.key_hints(uid)
        return {
            p: {"own": hints.get(p), "pool": bool(getattr(self.pool, p))} for p in PROVIDERS
        }


class UserRuntime:
    """Mirror of the local AppState, scoped to one user and backed by the store."""

    def __init__(self, user: User, store: Store, keys: KeyResolver, limits: Limits, data_dir: Path,
                 log: Callable[[str], None] | None = None):
        self.user = user
        self.store = store
        self.keys = keys
        self.limits = limits
        self.runs_dir = data_dir / "users" / user.id / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.terminal_log = log
        self.hub = Hub()
        self.lock = threading.Lock()
        self.client: PhoneHarnessClient | None = None
        self.brain: Brain | None = None
        self.session: Session | None = None
        self.device: Any = None
        self._close_transport: Callable[[], None] = lambda: None
        self.status = "no_phone"
        self.task: str | None = None
        self.agent: Agent | None = None
        self.run_dir: Path | None = None
        self.run_id: str | None = None
        self.last_used = time.time()

    # ------------------------------------------------------------ clients
    def _ensure_clients(self) -> None:
        if self.client is None:
            key, source = self.keys.phone_key(self.user.id)
            self.client = PhoneHarnessClient(api_key=key)
            self._log(f"phone harness key: {source}")
        if self.brain is None:
            provider, key, source = self.keys.model(self.user.id)
            self.brain = _brain_with_key(provider, key)
            self._log(f"model: {self.brain.name}/{self.brain.model} ({source} key)")

    def reset_clients(self) -> None:
        """Called after the user changes a key; takes effect for the next phone."""
        if self.status == "no_phone":
            if self.client:
                self.client.close()
            self.client = None
            self.brain = None

    # -------------------------------------------------------------- state
    def state(self) -> dict[str, Any]:
        self.last_used = time.time()
        s = self.session
        used = self.store.phone_minutes_today(self.user.id)
        return {
            "status": self.status,
            "session_id": s.id if s else None,
            "screen": list(s.screen) if s and s.screen else None,
            "seconds_left": int(self.device.seconds_left() or 0) if self.device else None,
            "task": self.task,
            "brain": f"{self.brain.name}/{self.brain.model}" if self.brain else None,
            "transport": self.limits.transport,
            "run_dir": self.run_dir.name if self.run_dir else None,
            "user": {"email": self.user.email, "is_admin": self.user.is_admin},
            "keys": self.keys.summary(self.user.id),
            "quota": {
                "phone_minutes_used_today": round(used, 1),
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

    # ------------------------------------------------------------ session
    def start_session(self, timeout_seconds: int | None) -> None:
        timeout = min(int(timeout_seconds or self.limits.default_session_timeout_s), self.limits.max_session_timeout_s)
        with self.lock:
            if self.status != "no_phone":
                raise RuntimeError(f"cannot start a phone while status is {self.status}")
            if len(self.store.open_phones(self.user.id)) >= self.limits.max_phones_per_user:
                raise QuotaError("you already have a phone open; end it first")
            if self.store.phone_minutes_today(self.user.id) >= self.limits.daily_phone_minutes:
                raise QuotaError("daily phone-minute quota reached")
            self._ensure_clients()
            self.status = "starting"
        self._push_state()
        threading.Thread(target=self._start_bg, args=(None, timeout), daemon=True).start()

    def attach_session(self, sid: str) -> None:
        with self.lock:
            if self.status != "no_phone":
                raise RuntimeError(f"cannot attach while status is {self.status}")
            if self.store.phone_owner(sid) != self.user.id:
                raise PermissionError("that session is not yours")
            self._ensure_clients()
            self.status = "starting"
        self._push_state()
        threading.Thread(target=self._start_bg, args=(sid, 0), daemon=True).start()

    def _start_bg(self, sid: str | None, timeout: int) -> None:
        assert self.client
        try:
            if sid is None:
                s = self.client.create_session(timeout_seconds=timeout, idempotency_key=None)
                self.store.add_phone(s.id, self.user.id, s.expires_at, self.limits.transport)
                self.store.audit(self.user.id, "phone.start", s.id)
                self._log(f"created session {s.id} (timeout {timeout}s); provisioning…")
                lease = acquire(self.client, s.id, timeout, self._log)
            else:
                lease = acquire(self.client, sid, timeout, self._log)
            self.session = lease.session
            self.store.mark_phone_ready(lease.session.id, lease.session.expires_at)
            self.device, self._close_transport = make_device(self.client, lease.session, self.limits.transport, self._log)
            self.status = "ready"
        except Exception as exc:  # noqa: BLE001
            self._log(f"phone failed to start: {exc}")
            self.status = "no_phone"
        self._push_state()

    def end_session(self) -> None:
        with self.lock:
            if self.status in ("no_phone", "ending"):
                return
            if self.agent:
                self.agent.cancel()
            self.status = "ending"
        self._push_state()
        threading.Thread(target=self._end_bg, daemon=True).start()

    def _end_bg(self) -> None:
        try:
            self._close_transport()
        except Exception as exc:  # noqa: BLE001
            self._log(f"transport close failed: {exc}")
        self._close_transport = lambda: None
        if self.session and self.client:
            try:
                release(self.client, self.session.id, self._log)
            except CloudError as exc:
                self._log(f"end failed: {exc}")
            self.store.end_phone(self.session.id)
            self.store.audit(self.user.id, "phone.end", self.session.id)
        self.session = None
        self.device = None
        self.task = None
        self.agent = None
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
            if self.status != "ready" or not self.device or not self.brain or not self.session:
                raise RuntimeError("phone is not ready")
            self.status = "running"
            self.task = task
        self.hub.publish("task", task=task)
        self._push_state()
        threading.Thread(target=self._run_bg, args=(task,), daemon=True).start()

    def _run_bg(self, task: str) -> None:
        assert self.device and self.session and self.brain
        trace = new_trace(task, self.session.id, self.brain, self.runs_dir)
        self.run_dir = trace.dir
        self.run_id = self.store.add_run(self.user.id, self.session.id, task, trace.dir.name)
        self.store.audit(self.user.id, "task.start", task[:120])
        self.agent = Agent(self.device, self.brain, trace, AgentConfig(max_steps=self.limits.max_steps),
                           log=self._log, on_step=self._on_step)
        outcome = self.agent.run(task)
        self.store.finish_run(self.run_id, outcome.success, outcome.summary, outcome.result, outcome.steps)
        self.hub.publish("outcome", success=outcome.success, summary=outcome.summary, result=outcome.result,
                         steps=outcome.steps, error=outcome.error, report_url=f"/runs/{trace.dir.name}/report.html")
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
        self.hub.publish("step", step=rec.step, app=rec.app, elements=rec.elements, thought=rec.thought,
                         action=rec.action, args=rec.args, feedback=rec.feedback, marked_url=base + rec.marked,
                         shot_url=base + rec.screenshot, latency_llm_s=rec.latency_llm_s, latency_act_s=rec.latency_act_s)

    # -------------------------------------------------------------- frame
    def frame(self) -> bytes:
        if not self.session or not self.client or self.status in ("no_phone", "starting", "ending"):
            raise LookupError("no ready phone")
        if self.store.phone_owner(self.session.id) != self.user.id:
            raise PermissionError("not your phone")
        if hasattr(self.device, "transport") and getattr(self.device, "transport") == "adb":
            import io

            buf = io.BytesIO()
            self.device.screenshot().save(buf, format="PNG")
            return buf.getvalue()
        return self.client.snapshot(self.session.id)

    def run_file(self, run_dir_name: str, rel: str) -> Path | None:
        if not self.store.run_dir_owned(self.user.id, run_dir_name):
            return None
        root = self.runs_dir.resolve()
        target = (root / run_dir_name / rel).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return None
        return target


def _brain_with_key(provider: str, key: str) -> Brain:
    """Build a brain with an explicit key (never via process-wide env, which other users share)."""
    if provider == "anthropic":
        from ..brain.anthropic import AnthropicBrain

        return AnthropicBrain(api_key=key)
    if provider == "gemini":
        from ..brain.gemini import GeminiBrain

        return GeminiBrain(api_key=key)
    return make_brain(provider)


class Registry:
    """uid -> UserRuntime, created lazily; idle runtimes without a phone are dropped."""

    IDLE_S = 3600

    def __init__(self, store: Store, keys: KeyResolver, limits: Limits, data_dir: Path,
                 log: Callable[[str], None] | None = None):
        self.store, self.keys, self.limits, self.data_dir, self.log = store, keys, limits, data_dir, log
        self._by_uid: dict[str, UserRuntime] = {}
        self._lock = threading.Lock()

    def get(self, user: User) -> UserRuntime:
        with self._lock:
            rt = self._by_uid.get(user.id)
            if rt is None:
                rt = UserRuntime(user, self.store, self.keys, self.limits, self.data_dir, self.log)
                self._by_uid[user.id] = rt
            rt.user = user
            return rt

    def sweep(self) -> None:
        now = time.time()
        with self._lock:
            for uid, rt in list(self._by_uid.items()):
                if rt.status == "no_phone" and now - rt.last_used > self.IDLE_S:
                    del self._by_uid[uid]

    def shutdown(self) -> None:
        with self._lock:
            runtimes = list(self._by_uid.values())
        for rt in runtimes:
            if rt.session and rt.client:
                try:
                    rt._close_transport()
                    release(rt.client, rt.session.id, rt._log)
                    rt.store.end_phone(rt.session.id)
                except Exception:  # noqa: BLE001
                    pass
