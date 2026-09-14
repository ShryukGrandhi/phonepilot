"""Session lifecycle: get a ready phone, hand it back when done.

Provisioning is the slow part (~2 min cold on the current provider), so the
helpers here make it painless to (a) reuse a session across several tasks and
(b) always release a phone we created, even when the agent crashes.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator

from .cloud import NotFound, PhoneHarnessClient, Session, new_idempotency_key

DEFAULT_TIMEOUT_S = 900
PROVISION_WAIT_S = 600.0


@dataclass(frozen=True)
class Lease:
    session: Session
    created_here: bool
    ready_wait_s: float


def acquire(
    client: PhoneHarnessClient,
    session_id: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_S,
    log: Callable[[str], None] = print,
) -> Lease:
    """Return a ready session: the given one, the newest ready one, or a fresh one."""
    t0 = time.monotonic()
    if session_id:
        s = client.get_session(session_id)
        if not s.is_ready:
            log(f"session {s.id} is {s.state}; waiting for ready…")
            s = client.wait_ready(s.id, PROVISION_WAIT_S, on_poll=_progress(log))
        return Lease(s, created_here=False, ready_wait_s=time.monotonic() - t0)

    key = new_idempotency_key()
    s = client.create_session(timeout_seconds=timeout_seconds, idempotency_key=key)
    log(f"created session {s.id} (timeout {timeout_seconds}s, idempotency key {key}); provisioning…")
    s = client.wait_ready(s.id, PROVISION_WAIT_S, on_poll=_progress(log))
    wait = time.monotonic() - t0
    log(f"session {s.id} ready after {wait:.0f}s: screen {s.screen}, {len(s.ops)} ops")
    return Lease(s, created_here=True, ready_wait_s=wait)


def release(client: PhoneHarnessClient, session_id: str, log: Callable[[str], None] = print) -> None:
    try:
        r = client.end_session(session_id)
    except NotFound:
        log(f"session {session_id} already gone")
        return
    state = "released" if r.get("released") else "closing (cleanup pending)"
    log(f"session {session_id} {state}")


@contextmanager
def leased(
    client: PhoneHarnessClient,
    session_id: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_S,
    keep: bool = False,
    log: Callable[[str], None] = print,
) -> Iterator[Lease]:
    """Context manager: yields a ready session; ends it on exit unless reused or `keep`."""
    lease = acquire(client, session_id, timeout_seconds, log)
    try:
        yield lease
    finally:
        if lease.created_here and not keep:
            release(client, lease.session.id, log)
        elif keep:
            log(f"keeping session {lease.session.id} alive (--keep); end it with: phonepilot end {lease.session.id}")


def make_device(client: PhoneHarnessClient, session: Session, transport: str = "http",
                log: Callable[[str], None] = print):
    """Build the phone object for a ready session on the chosen transport.

    Returns (device, closer). `closer()` tears down anything the transport opened
    (the adb connection / tunnel); it never ends the session itself.
    """
    from .device import Device

    if transport == "http":
        return Device(client, session), (lambda: None)
    if transport == "adb":
        from .adb import AdbDevice, AdbTunnel

        tunnel = AdbTunnel(client, session, log=log).open()
        return AdbDevice(tunnel, session), tunnel.close
    raise ValueError(f"unknown transport {transport!r}; use 'http' or 'adb'")


def _progress(log: Callable[[str], None]):
    last = {"t": 0.0}

    def on_poll(s: Session) -> None:
        now = time.monotonic()
        if now - last["t"] >= 15:
            last["t"] = now
            log(f"  … {s.state} (age {s.raw.get('age_s', '?')}s)")

    return on_poll
