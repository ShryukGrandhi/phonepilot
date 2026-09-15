"""One sandbox per phone session: the agent, the user's keys, the adb identity and the
phone connection all live in their own process or container. The web tier never holds
a phone; it only starts sandboxes, proxies to them, and stops them.

Two backends behind one interface:

* `DockerBackend`  — `docker run` of the phonepilot image, one container per session:
  read-only rootfs, memory/CPU limits, no host mounts except the user's own runs dir,
  secrets passed as env, a per-container bearer token so only the web tier can talk to it.
* `ProcessBackend` — a `phonepilot sandbox` subprocess per session on 127.0.0.1 with a
  random port. Same protocol and token; weaker isolation (shared kernel + filesystem).
  Used when Docker is unavailable and in tests.

The sandbox itself is the single-phone agent server from `web/server.py`, started with
`--token`, so the web tier reuses one well-tested implementation.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

SANDBOX_PORT = 9000
START_TIMEOUT_S = 60.0
STOP_GRACE_S = 200.0  # a phone still provisioning is released only when provisioning returns
DOCKER_IMAGE = os.environ.get("PHONEPILOT_IMAGE", "phonepilot:latest")


class SandboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class SandboxSpec:
    user_id: str
    runs_dir: Path                 # host path, becomes /runs inside the container
    env: dict[str, str]            # PHONE_HARNESS_API_KEY, GEMINI_API_KEY / ANTHROPIC_API_KEY, PHONEPILOT_*
    transport: str = "http"
    max_steps: int = 25


@dataclass
class Sandbox:
    id: str
    user_id: str
    base_url: str
    token: str
    backend: str
    started: float
    handle: Any = None  # Popen for processes, container id for docker

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 30.0) -> tuple[int, bytes, str]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — our own sandbox
                return resp.status, resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers.get("Content-Type", "")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise SandboxError(f"sandbox unreachable: {exc}") from exc

    def open_stream(self, path: str, timeout: float = 3600.0):
        """Return a file-like response for a streaming GET (SSE)."""
        req = urllib.request.Request(self.base_url + path, method="GET")
        req.add_header("Authorization", f"Bearer {self.token}")
        return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310


class ProcessBackend:
    name = "process"

    def __init__(self, log: Callable[[str], None] = print):
        self.log = log

    def start(self, spec: SandboxSpec) -> Sandbox:
        port = _free_port()
        token = secrets.token_urlsafe(24)
        env = {**os.environ, **spec.env, "PHONEPILOT_SANDBOX_TOKEN": token}
        for k in ("PHONEPILOT_MASTER_KEY", "PHONEPILOT_POOL_PHONE_KEY", "PHONEPILOT_POOL_GEMINI_KEY", "PHONEPILOT_POOL_ANTHROPIC_KEY"):
            env.pop(k, None)  # a sandbox never sees the master key or the pool
        # a private adb server per sandbox: stock adb otherwise shares one daemon (port 5037) across
        # all processes on the host, through which any of them could address any connected phone
        env["ANDROID_ADB_SERVER_PORT"] = str(_free_port())
        cmd = [sys.executable, "-m", "phonepilot.cli", "sandbox", "--host", "127.0.0.1", "--port", str(port),
               "--runs-dir", str(spec.runs_dir), "--transport", spec.transport, "--max-steps", str(spec.max_steps),
               "--parent-pid", str(os.getpid())]
        spec.runs_dir.mkdir(parents=True, exist_ok=True)
        logfile = open(spec.runs_dir.parent / f"sandbox-{port}.log", "ab")  # noqa: SIM115 — lives as long as the process
        proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=logfile, stderr=subprocess.STDOUT)
        sb = Sandbox(id=f"proc-{proc.pid}", user_id=spec.user_id, base_url=f"http://127.0.0.1:{port}", token=token,
                     backend=self.name, started=time.time(), handle=proc)
        _wait_healthy(sb, lambda: proc.poll() is not None)
        return sb

    def stop(self, sb: Sandbox) -> None:
        proc: subprocess.Popen = sb.handle
        try:
            sb.request("POST", "/api/shutdown", {}, timeout=10)
        except SandboxError:
            pass
        try:
            proc.wait(timeout=STOP_GRACE_S)  # shutdown ends the phone first, which can take a while
        except subprocess.TimeoutExpired:
            proc.kill()

    def alive(self, sb: Sandbox) -> bool:
        return sb.handle.poll() is None


class DockerBackend:
    name = "docker"

    def __init__(self, image: str = DOCKER_IMAGE, network: str | None = None, log: Callable[[str], None] = print):
        if shutil.which("docker") is None:
            raise SandboxError("docker CLI not found")
        self.image, self.network, self.log = image, network, log
        self.reap_stale()

    def reap_stale(self) -> None:
        """Containers left by a web tier that died: ask them to shut down (ends their phones), then remove."""
        r = subprocess.run(["docker", "ps", "-q", "--filter", "label=phonepilot.sandbox=1"], capture_output=True, text=True, timeout=30)
        stale = [c for c in r.stdout.split() if c]
        for cid in stale:
            self.log(f"reaping stale sandbox container {cid[:12]}")
            subprocess.run(["docker", "stop", "-t", "120", cid], capture_output=True, timeout=180)
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=60)

    @staticmethod
    def available() -> bool:
        if shutil.which("docker") is None:
            return False
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0

    def start(self, spec: SandboxSpec) -> Sandbox:
        token = secrets.token_urlsafe(24)
        name = f"pp-{spec.user_id}-{secrets.token_hex(3)}"
        port = _free_port()
        spec.runs_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            "docker", "run", "-d", "--name", name, "--no-healthcheck", "--label", "phonepilot.sandbox=1", "--label", f"phonepilot.owner={os.getpid()}",
            "--read-only", "--tmpfs", "/tmp:rw,size=256m", "--tmpfs", "/home/phonepilot/.android:rw,size=1m",
            "--memory", "768m", "--cpus", "1", "--pids-limit", "256",
            "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
            "-p", f"127.0.0.1:{port}:{SANDBOX_PORT}",
            "-v", f"{spec.runs_dir.resolve()}:/runs",
            "-e", f"PHONEPILOT_SANDBOX_TOKEN={token}",
        ]
        if self.network:
            cmd += ["--network", self.network]
        for k, v in spec.env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [self.image, "phonepilot", "sandbox", "--host", "0.0.0.0", "--port", str(SANDBOX_PORT),
                "--runs-dir", "/runs", "--transport", spec.transport, "--max-steps", str(spec.max_steps)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise SandboxError(f"docker run failed: {r.stderr.strip()[:300]}")
        cid = r.stdout.strip()
        sb = Sandbox(id=name, user_id=spec.user_id, base_url=f"http://127.0.0.1:{port}", token=token,
                     backend=self.name, started=time.time(), handle=cid)
        _wait_healthy(sb, lambda: not self.alive(sb))
        return sb

    def stop(self, sb: Sandbox) -> None:
        try:
            sb.request("POST", "/api/shutdown", {}, timeout=10)  # lets it end the phone cleanly
        except SandboxError:
            pass
        deadline = time.monotonic() + STOP_GRACE_S
        while time.monotonic() < deadline and self.alive(sb):
            time.sleep(1.0)
        subprocess.run(["docker", "rm", "-f", sb.handle], capture_output=True, timeout=60)

    def alive(self, sb: Sandbox) -> bool:
        r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", sb.handle], capture_output=True, text=True, timeout=20)
        return r.returncode == 0 and r.stdout.strip() == "true"


class ThreadBackend:
    """In-process sandbox for tests and dev: the same token-protected agent server, in a thread.

    `client_factory(env)` and `brain_factory(env)` let tests inject a fake phone per user.
    """

    name = "thread"

    def __init__(self, client_factory: Callable[[dict[str, str]], Any], brain_factory: Callable[[dict[str, str]], Any],
                 log: Callable[[str], None] = print):
        self.client_factory, self.brain_factory, self.log = client_factory, brain_factory, log
        self._servers: dict[str, Any] = {}

    def start(self, spec: SandboxSpec) -> Sandbox:
        import threading

        from ..web.server import AppState, make_server

        token = secrets.token_urlsafe(24)
        app = AppState(self.client_factory(spec.env), self.brain_factory(spec.env), spec.runs_dir, spec.max_steps,
                       log=None, transport=spec.transport)
        server = make_server(app, "127.0.0.1", 0, token=token)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        sb = Sandbox(id=f"thread-{server.server_address[1]}", user_id=spec.user_id,
                     base_url=f"http://127.0.0.1:{server.server_address[1]}", token=token, backend=self.name,
                     started=time.time(), handle=server)
        self._servers[sb.id] = server
        _wait_healthy(sb, lambda: False)
        return sb

    def stop(self, sb: Sandbox) -> None:
        try:
            sb.request("POST", "/api/shutdown", {}, timeout=10)
        except SandboxError:
            pass
        server = self._servers.pop(sb.id, None)
        if server is not None:
            import threading as _t
            _t.Thread(target=server.shutdown, daemon=True).start()
            server.server_close()

    def alive(self, sb: Sandbox) -> bool:
        return sb.id in self._servers


def pick_backend(preferred: str | None, log: Callable[[str], None] = print):
    choice = (preferred or os.environ.get("PHONEPILOT_SANDBOX", "auto")).lower()
    if choice in ("docker", "auto") and DockerBackend.available():
        log("sandbox backend: docker (one container per phone session)")
        return DockerBackend(log=log)
    if choice == "docker":
        raise SandboxError("PHONEPILOT_SANDBOX=docker but the docker daemon is not reachable")
    log("sandbox backend: process (one subprocess per phone session)")
    return ProcessBackend(log=log)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_healthy(sb: Sandbox, dead: Callable[[], bool]) -> None:
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        if dead():
            raise SandboxError("sandbox exited during startup")
        try:
            status, _, _ = sb.request("GET", "/healthz", timeout=3)
            if status == 200:
                return
        except SandboxError:
            pass
        time.sleep(0.4)
    raise SandboxError("sandbox did not become healthy in time")
