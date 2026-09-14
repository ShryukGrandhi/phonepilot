"""ADB transport: drive the cloud phone with stock `adb`.

Two shapes of `POST /sessions/{sid}/adb` exist:

* **documented** (guides/connect-adb, OpenAPI): register an `ssh-ed25519` public key, get an
  SSH gateway + pinned host key + one forwardable port, open `ssh -N -L`, `adb connect 127.0.0.1:<local>`.
* **live on 2026-09-14**: the endpoint rejects SSH keys ("expected one ADB public key (the
  contents of adbkey.pub)") and answers `{"transport": "adb", "host": ..., "port": ...}` — a
  direct ADB-over-TCP endpoint that authenticates with adb's own RSA key (`~/.android/adbkey`).

`AdbTunnel.open()` handles both: it first offers adb's key; if the service wants SSH it falls
back to a throwaway ed25519 key and the tunnel. Either way the result is an adb serial.

`AdbDevice` exposes the same surface as `device.Device` (tap, scroll, type_text, tree,
screenshot, launch, ...), so the agent loop does not know which transport it is on. On
top of that it has what the HTTP ops deliberately do not: `shell()`, `grant()`, `logcat()`.
"""

from __future__ import annotations

import io
import re
import shutil
import socket
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from .cloud import CloudError, PhoneHarnessClient, Session
from .device import DIRECTIONS, Node, TextItem, sanitize_text

DEFAULT_LOCAL_PORT = 15555
TUNNEL_TIMEOUT_S = 25.0
ADB_TIMEOUT_S = 60.0
KEYCODES = {
    "enter": "KEYCODE_ENTER", "return": "KEYCODE_ENTER", "tab": "KEYCODE_TAB", "space": "KEYCODE_SPACE",
    "backspace": "KEYCODE_DEL", "delete": "KEYCODE_FORWARD_DEL", "escape": "KEYCODE_ESCAPE", "esc": "KEYCODE_ESCAPE",
    "home": "KEYCODE_HOME", "back": "KEYCODE_BACK", "recents": "KEYCODE_APP_SWITCH", "menu": "KEYCODE_MENU",
    "up": "KEYCODE_DPAD_UP", "down": "KEYCODE_DPAD_DOWN", "left": "KEYCODE_DPAD_LEFT", "right": "KEYCODE_DPAD_RIGHT",
    "power": "KEYCODE_POWER", "volumeup": "KEYCODE_VOLUME_UP", "volumedown": "KEYCODE_VOLUME_DOWN",
    "search": "KEYCODE_SEARCH",
}
# characters `adb shell input text` needs escaped (the string passes through the device shell)
SHELL_SPECIAL = set("\\\"'`$&|;<>()*?[]{}~#!")


class AdbError(RuntimeError):
    pass


# ------------------------------------------------------------------ tunnel
@dataclass(frozen=True)
class TunnelInfo:
    host: str
    port: int
    username: str
    host_key: str
    forward_host: str
    forward_port: int
    expires_at: float


class AdbTunnel:
    """SSH local-forward from 127.0.0.1:<local_port> to the phone's ADB port, plus `adb connect`."""

    def __init__(self, client: PhoneHarnessClient, session: Session, local_port: int = DEFAULT_LOCAL_PORT,
                 log: Callable[[str], None] = print):
        if shutil.which("adb") is None:
            raise AdbError("adb not found on PATH (install Android platform-tools)")
        self.client = client
        self.session = session
        self.local_port = local_port
        self.log = log
        self.serial = f"127.0.0.1:{local_port}"
        self._dir = Path(tempfile.mkdtemp(prefix="phonepilot-adb-"))
        self._ssh: subprocess.Popen | None = None
        self.info: TunnelInfo | None = None

    # ---------------------------------------------------------- lifecycle
    def open(self) -> "AdbTunnel":
        data = self._register()
        if data.get("transport") == "adb" or "forward_port" not in data:
            return self._open_direct(data)
        return self._open_ssh(data)

    def _register(self) -> dict[str, Any]:
        """Handle every shape of POST /sessions/{sid}/adb seen so far.

        1. body {public_key: <adbkey.pub>}  -> {transport: "adb", host, port}            (docs, 2026-09-14 pm)
        2. no body                          -> {transport: "adb", host, port, code}      (live, 2026-09-14 eve)
           the code is sent over adb itself: `adb shell unlock <code>` after connecting
        3. body {public_key: <ssh-ed25519>} -> SSH tunnel details                          (original docs)
        """
        adb_pub = adb_public_key()
        if adb_pub:
            self.log("registering adb's RSA public key (~/.android/adbkey.pub) with POST /sessions/{sid}/adb …")
            try:
                return self.client.enable_adb(self.session.id, adb_pub)
            except CloudError as exc:
                if exc.status != 400:
                    raise
                message = str(exc.payload.get("error", ""))
                if "no body" in message or "reset" in message:
                    self.log("service uses the unlock-code flow; enabling ADB without a key")
                    return self.client.enable_adb_codeflow(self.session.id)
                self.log(f"service declined the adb key ({message}); trying an ssh-ed25519 key")
        key = self._dir / "id_ed25519"
        _run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "phonepilot", "-f", str(key)])
        public_key = (self._dir / "id_ed25519.pub").read_text(encoding="utf-8").strip()
        return self.client.enable_adb(self.session.id, public_key)

    def _open_direct(self, data: dict[str, Any]) -> "AdbTunnel":
        self.serial = f"{data['host']}:{int(data['port'])}"
        self.info = TunnelInfo(host=data["host"], port=int(data["port"]), username="", host_key="",
                               forward_host="", forward_port=0, expires_at=float(data.get("expires_at") or 0))
        self.log(f"direct ADB endpoint {self.serial} (transport=adb)")
        out = _run(["adb", "connect", self.serial], timeout=30).stdout
        self.log(f"adb connect: {out.strip()}")
        self.log(f"adb device state: {self._wait_for_device()}")
        code = data.get("code")
        if code:
            self._unlock(code)
        return self

    def _unlock(self, code: str) -> None:
        """Unlock-code flow: the phone answers every shell command with 'locked' until the code is presented."""
        out = _run(["adb", "-s", self.serial, "shell", "unlock", code], timeout=30, check=False).stdout.strip()
        self.log(f"adb unlock: {out or 'ok'}")
        probe = _run(["adb", "-s", self.serial, "shell", "getprop", "ro.product.model"], timeout=30, check=False).stdout.strip()
        if probe.startswith("locked"):
            raise AdbError(f"phone still locked after presenting the code: {probe}")

    def _open_ssh(self, data: dict[str, Any]) -> "AdbTunnel":
        key = self._dir / "id_ed25519"
        if not key.exists():
            raise AdbError("service returned an SSH tunnel but no ssh key was registered")
        if shutil.which("ssh") is None:
            raise AdbError("ssh not found on PATH (needed for the SSH tunnel variant)")
        self.info = TunnelInfo(
            host=data["host"], port=int(data["port"]), username=data["username"], host_key=data["host_key"],
            forward_host=data["forward_host"], forward_port=int(data["forward_port"]), expires_at=float(data["expires_at"]),
        )
        known_hosts = self._dir / "known_hosts"
        known_hosts.write_text(f"[{self.info.host}]:{self.info.port} {self.info.host_key}\n", encoding="utf-8")
        cmd = [
            "ssh", "-F", "/dev/null", "-N", "-T",
            "-i", str(key), "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts.as_posix()}",
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "BatchMode=yes",
            "-p", str(self.info.port),
            "-L", f"127.0.0.1:{self.local_port}:{self.info.forward_host}:{self.info.forward_port}",
            f"{self.info.username}@{self.info.host}",
        ]
        self.log(f"opening SSH tunnel 127.0.0.1:{self.local_port} -> {self.info.host}:{self.info.port} (host key pinned)")
        self._ssh = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._wait_for_local_port()
        out = _run(["adb", "connect", self.serial], timeout=30).stdout
        self.log(f"adb connect: {out.strip()}")
        state = self._wait_for_device()
        self.log(f"adb device state: {state}")
        return self

    def close(self, revoke: bool = True) -> None:
        try:
            _run(["adb", "disconnect", self.serial], timeout=15, check=False)
        except AdbError:
            pass
        if self._ssh and self._ssh.poll() is None:
            self._ssh.terminate()
            try:
                self._ssh.wait(5)
            except subprocess.TimeoutExpired:
                self._ssh.kill()
        if revoke:
            try:
                self.client.revoke_adb(self.session.id)
                self.log("adb access revoked")
            except CloudError as exc:
                self.log(f"revoke failed: {exc}")
        shutil.rmtree(self._dir, ignore_errors=True)

    def __enter__(self) -> "AdbTunnel":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ helpers
    def _wait_for_local_port(self) -> None:
        deadline = time.monotonic() + TUNNEL_TIMEOUT_S
        while time.monotonic() < deadline:
            assert self._ssh is not None
            if self._ssh.poll() is not None:
                err = (self._ssh.stderr.read() if self._ssh.stderr else b"").decode(errors="replace")
                raise AdbError(f"ssh exited ({self._ssh.returncode}): {err.strip()}")
            with socket.socket() as s:
                s.settimeout(0.5)
                if s.connect_ex(("127.0.0.1", self.local_port)) == 0:
                    return
            time.sleep(0.5)
        raise AdbError(f"ssh tunnel did not open 127.0.0.1:{self.local_port} within {TUNNEL_TIMEOUT_S:.0f}s")

    def _wait_for_device(self) -> str:
        deadline = time.monotonic() + TUNNEL_TIMEOUT_S
        last = ""
        while time.monotonic() < deadline:
            last = _run(["adb", "-s", self.serial, "get-state"], timeout=15, check=False).stdout.strip()
            if last == "device":
                return last
            time.sleep(1.0)
        raise AdbError(f"adb device never reached state 'device' (last: {last!r})")


# ------------------------------------------------------------------ device
class AdbDevice:
    """Same surface as `device.Device`, implemented with adb commands over the tunnel."""

    transport = "adb"

    def __init__(self, tunnel: AdbTunnel, session: Session, sleep=time.sleep):
        self.tunnel = tunnel
        self.serial = tunnel.serial
        self.sid = session.id
        self.expires_at = session.expires_at
        self._sleep = sleep
        self.ops = frozenset()
        if session.screen:
            self.width, self.height = session.screen
        else:
            self.width, self.height = self._probe_size()

    # ----------------------------------------------------------- plumbing
    def supports(self, op: str) -> bool:
        return True

    def seconds_left(self) -> float | None:
        return None if self.expires_at is None else self.expires_at - time.time()

    def shell(self, cmd: str, timeout: float = ADB_TIMEOUT_S, binary: bool = False) -> str | bytes:
        """Run a command on the phone. This is the escape hatch the HTTP ops do not have."""
        r = _run(["adb", "-s", self.serial, "exec-out" if binary else "shell", cmd], timeout=timeout, binary=binary)
        return r.stdout

    def _probe_size(self) -> tuple[int, int]:
        m = re.search(r"Physical size:\s*(\d+)x(\d+)", str(self.shell("wm size")))
        if not m:
            raise AdbError("could not read screen size via `wm size`")
        return int(m.group(1)), int(m.group(2))

    # ------------------------------------------------------------ reading
    def screenshot(self) -> Image.Image:
        png = self.shell("screencap -p", binary=True)
        return Image.open(io.BytesIO(png)).convert("RGB")

    def snapshot(self) -> Image.Image:
        return self.screenshot()

    def tree(self) -> tuple[Node, ...]:
        # exec-out: raw stdout, no pty mangling of the XML
        raw = self.shell("uiautomator dump /dev/tty", binary=True)
        return parse_uiautomator(raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw))

    def texts(self) -> tuple[TextItem, ...]:
        return tuple(TextItem(n.text, n.x, n.y, n.w, n.h) for n in self.tree() if n.text)

    def current_app(self) -> str | None:
        out = str(self.shell("dumpsys activity activities | grep -E 'topResumedActivity|mResumedActivity' | head -1"))
        m = re.search(r"\s([A-Za-z0-9_.]+)/", out)
        return m.group(1) if m else None

    def apps(self, include_system: bool = False) -> tuple[str, ...]:
        out = str(self.shell("pm list packages" + ("" if include_system else " -3")))
        return tuple(sorted(line.split(":", 1)[1].strip() for line in out.splitlines() if line.startswith("package:")))

    # -------------------------------------------------------------- input
    def tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {self._cx(x)} {self._cy(y)}")

    def long_press(self, x: int, y: int, duration: float = 0.8) -> None:
        x, y = self._cx(x), self._cy(y)
        self.shell(f"input swipe {x} {y} {x} {y} {int(duration * 1000)}")

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.35) -> None:
        self.shell(f"input swipe {self._cx(x1)} {self._cy(y1)} {self._cx(x2)} {self._cy(y2)} {int(duration * 1000)}")

    def swipe(self, direction: str, distance: float = 0.45, at: tuple[int, int] | None = None, duration: float = 0.35) -> None:
        if direction not in DIRECTIONS:
            raise ValueError(f"direction must be one of {DIRECTIONS}")
        cx, cy = at or (self.width // 2, int(self.height * 0.55))
        dx, dy = int(self.width * distance), int(self.height * distance)
        end = {"up": (cx, cy - dy // 2), "down": (cx, cy + dy // 2), "left": (cx - dx // 2, cy), "right": (cx + dx // 2, cy)}[direction]
        start = (2 * cx - end[0], 2 * cy - end[1])
        self.drag(*start, *end, duration=duration)

    def scroll(self, direction: str = "down", amount: float = 0.5, at: tuple[int, int] | None = None) -> None:
        opposite = {"up": "down", "down": "up", "left": "right", "right": "left"}
        if direction not in opposite:
            raise ValueError(f"direction must be one of {DIRECTIONS}")
        self.swipe(opposite[direction], distance=amount, at=at, duration=0.4)

    def type_text(self, text: str, submit: bool = False) -> str:
        clean = sanitize_text(text)
        for line in clean.split("\n"):
            if line:
                self.shell(f"input text {escape_for_input(line)}")
        if "\n" in clean or submit:
            self.key("enter")
        return clean

    def key(self, name: str) -> None:
        name = name.strip().lower()
        code = KEYCODES.get(name)
        if code is None and len(name) == 1 and name.isalnum():
            code = f"KEYCODE_{name.upper()}"
        if code is None:
            raise ValueError(f"unsupported key {name!r}")
        self.shell(f"input keyevent {code}")

    def home(self) -> None:
        self.key("home")
        self._sleep(0.5)

    def back(self) -> None:
        self.key("back")
        self._sleep(0.3)

    def recents(self) -> None:
        self.key("recents")
        self._sleep(0.5)

    def launch(self, package: str) -> str:
        """Resolve the package's launcher activity and start it (monkey is unavailable on redroid)."""
        resolved = str(self.shell(
            f"cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER {package}"
        )).strip().splitlines()
        component = next((line.strip() for line in reversed(resolved) if "/" in line), None)
        if not component or "No activity found" in " ".join(resolved):
            raise AdbError(f"{package}: no launchable activity")
        out = str(self.shell(f"am start -W -n {component}"))
        if "Error" in out or "does not exist" in out:
            raise AdbError(f"{package}: am start failed: {out.strip()[:200]}")
        self._sleep(0.5)
        return package

    # ------------------------------------------------------------ extras
    def grant(self, package: str, permission: str) -> None:
        """Pre-grant a runtime permission, e.g. android.permission.POST_NOTIFICATIONS."""
        self.shell(f"pm grant {package} {permission}")

    def logcat(self, lines: int = 200) -> str:
        return str(self.shell(f"logcat -d -t {int(lines)}"))

    # ------------------------------------------------------------ timing
    def wait(self, seconds: float) -> None:
        self._sleep(max(0.0, float(seconds)))

    def _cx(self, x: int) -> int:
        return max(0, min(self.width - 1, int(round(x))))

    def _cy(self, y: int) -> int:
        return max(0, min(self.height - 1, int(round(y))))


# ----------------------------------------------------------------- helpers
def parse_uiautomator(xml: str) -> tuple[Node, ...]:
    """Flatten a uiautomator hierarchy dump into Nodes with center coordinates."""
    start = xml.find("<hierarchy")
    end = xml.rfind("</hierarchy>")
    if start < 0 or end < 0:
        return ()
    root = ET.fromstring(xml[start:end + len("</hierarchy>")])
    nodes: list[Node] = []
    for el in root.iter("node"):
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", el.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = (int(v) for v in m.groups())
        nodes.append(Node(
            text=el.get("text") or "", desc=el.get("content-desc") or "", res_id=el.get("resource-id") or "",
            cls=el.get("class") or "", clickable=el.get("clickable") == "true",
            x=(x1 + x2) // 2, y=(y1 + y2) // 2, w=x2 - x1, h=y2 - y1,
        ))
    return tuple(nodes)


def escape_for_input(text: str) -> str:
    """Escape for `adb shell input text`: spaces become %s, shell metacharacters get a backslash."""
    out = []
    for ch in text:
        if ch == " ":
            out.append("%s")
        elif ch in SHELL_SPECIAL:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def adb_public_key() -> str | None:
    """Contents of ~/.android/adbkey.pub, generating the pair with adb if needed."""
    home = Path.home() / ".android"
    pub = home / "adbkey.pub"
    if not pub.exists():
        home.mkdir(parents=True, exist_ok=True)
        _run(["adb", "keygen", str(home / "adbkey")], timeout=30, check=False)
    if not pub.exists():
        return None
    return pub.read_text(encoding="utf-8").strip() or None


def _run(cmd: list[str], timeout: float = ADB_TIMEOUT_S, check: bool = True, binary: bool = False) -> Any:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, text=not binary,
                           encoding=None if binary else "utf-8", errors=None if binary else "replace")
    except subprocess.TimeoutExpired as exc:
        raise AdbError(f"timed out: {' '.join(cmd[:4])}…") from exc
    if check and r.returncode != 0:
        err = r.stderr if isinstance(r.stderr, str) else r.stderr.decode(errors="replace")
        out = r.stdout if isinstance(r.stdout, str) else r.stdout.decode(errors="replace")
        detail = (err.strip() or out.strip())[:300]
        raise AdbError(f"{' '.join(cmd[:5])} failed ({r.returncode}): {detail}")
    return r
