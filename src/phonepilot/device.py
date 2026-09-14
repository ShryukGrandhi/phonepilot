"""High-level Android device wrapper over one Phone Harness session.

Everything the agent does to the phone goes through here. The wrapper adds the
things the raw op vocabulary leaves to the caller:

- accessibility nodes as typed, immutable `Node`s
- screenshots as PIL images (fresh `screen.capture`, or the cheap coalesced
  `frame.png` snapshot for change detection)
- content-direction scrolling (`scroll("down")` = show me what is further down)
  versus finger-direction swiping (`swipe("up")` = thumb moves up)
- text sanitising, because `input.text` accepts printable ASCII only and
  rejects the literal `%s`
- `wait_stable()`: poll frames until two in a row match, so the agent observes
  a settled screen instead of a mid-animation one
"""

from __future__ import annotations

import io
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

from PIL import Image

from .cloud import CloudError, NotReady, PhoneHarnessClient, Session, Unsupported

SUPPORTED_KEYS = frozenset(
    "enter tab space backspace delete escape home back recents menu up down left right "
    "power volumeup volumedown search return esc".split()
)
DIRECTIONS = ("up", "down", "left", "right")
SETTLE_AFTER_TAP_S = 0.8
STABLE_TIMEOUT_S = 5.0
STABLE_INTERVAL_S = 0.6
STABLE_THRESHOLD = 0.004  # mean abs pixel diff (0..1) below which two frames are "the same"
DIFF_SIZE = (36, 64)


@dataclass(frozen=True)
class Node:
    text: str
    desc: str
    res_id: str
    cls: str
    clickable: bool
    x: int
    y: int
    w: int
    h: int

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Node":
        return cls(
            text=d.get("text") or "",
            desc=d.get("desc") or "",
            res_id=d.get("id") or "",
            cls=d.get("class") or "",
            clickable=bool(d.get("clickable")),
            x=int(d.get("x", 0)),
            y=int(d.get("y", 0)),
            w=int(d.get("w", 0)),
            h=int(d.get("h", 0)),
        )

    @property
    def label(self) -> str:
        return self.text or self.desc

    @property
    def short_id(self) -> str:
        return self.res_id.rsplit("/", 1)[-1] if self.res_id else ""

    @property
    def short_class(self) -> str:
        return self.cls.rsplit(".", 1)[-1] if self.cls else ""


@dataclass(frozen=True)
class TextItem:
    text: str
    x: int
    y: int
    w: int
    h: int


class Device:
    def __init__(self, client: PhoneHarnessClient, session: Session, sleep=time.sleep):
        if not session.screen:
            raise ValueError("session has no screen geometry yet (not ready?)")
        self.client = client
        self.sid = session.id
        self.width, self.height = session.screen
        self.ops = frozenset(session.ops)
        self.expires_at = session.expires_at
        self._sleep = sleep

    # ------------------------------------------------------------- plumbing
    def supports(self, op: str) -> bool:
        return op in self.ops

    def seconds_left(self) -> float | None:
        return None if self.expires_at is None else self.expires_at - time.time()

    def _op(self, op: str, **kw: Any) -> Any:
        if self.ops and op not in self.ops:
            raise Unsupported(400, {"error": f"{op} not advertised by this session", "unsupported": True}, "POST", "op")
        return self.client.op(self.sid, op, **kw)

    # -------------------------------------------------------------- reading
    def screenshot(self) -> Image.Image:
        """Fresh capture via screen.capture (~2 s). Falls back to the snapshot frame."""
        try:
            result = self._op("screen.capture")
            png = _b64decode(result["png_b64"])
        except (CloudError, KeyError, TypeError):
            png = self.client.snapshot(self.sid)
        return Image.open(io.BytesIO(png)).convert("RGB")

    def snapshot(self) -> Image.Image:
        """Coalesced frame (server caches for 2 s). Cheap; good for change detection."""
        return Image.open(io.BytesIO(self.client.snapshot(self.sid))).convert("RGB")

    def tree(self) -> tuple[Node, ...]:
        result = self._op("tree") or []
        return tuple(Node.from_json(n) for n in result)

    def texts(self) -> tuple[TextItem, ...]:
        result = self._op("screen.text") or []
        return tuple(TextItem(t["text"], t["x"], t["y"], t["w"], t["h"]) for t in result)

    def current_app(self) -> str | None:
        return self._op("apps.current")

    def apps(self, include_system: bool = False) -> tuple[str, ...]:
        return tuple(self._op("apps.list", include_system=include_system) or ())

    # ---------------------------------------------------------------- input
    def tap(self, x: int, y: int) -> None:
        self._op("input.tap", x=self._cx(x), y=self._cy(y))

    def long_press(self, x: int, y: int, duration: float = 0.8) -> None:
        self._op("input.press", x=self._cx(x), y=self._cy(y), duration=float(duration))

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.35) -> None:
        self._op(
            "input.drag",
            x1=self._cx(x1), y1=self._cy(y1), x2=self._cx(x2), y2=self._cy(y2), duration=float(duration),
        )

    def swipe(self, direction: str, distance: float = 0.45, at: tuple[int, int] | None = None, duration: float = 0.35) -> None:
        """Finger moves in `direction`. swipe('up') = thumb up = next item in a feed."""
        if direction not in DIRECTIONS:
            raise ValueError(f"direction must be one of {DIRECTIONS}")
        cx, cy = at or (self.width // 2, int(self.height * 0.55))
        dx = int(self.width * distance)
        dy = int(self.height * distance)
        end = {
            "up": (cx, cy - dy // 2), "down": (cx, cy + dy // 2),
            "left": (cx - dx // 2, cy), "right": (cx + dx // 2, cy),
        }[direction]
        start = (2 * cx - end[0], 2 * cy - end[1])
        self.drag(*start, *end, duration=duration)

    def scroll(self, direction: str = "down", amount: float = 0.5, at: tuple[int, int] | None = None) -> None:
        """Content direction: scroll('down') reveals what is further down (finger moves up)."""
        opposite = {"up": "down", "down": "up", "left": "right", "right": "left"}
        if direction not in opposite:
            raise ValueError(f"direction must be one of {DIRECTIONS}")
        self.swipe(opposite[direction], distance=amount, at=at, duration=0.4)

    def type_text(self, text: str, submit: bool = False) -> str:
        """Type into the focused field. Returns the ASCII text actually sent."""
        clean = sanitize_text(text)
        for chunk in _split_percent_s(clean):
            if chunk:
                self._op("input.text", s=chunk)
        if submit:
            self.key("enter")
        return clean

    def key(self, name: str) -> None:
        name = name.strip().lower()
        if name not in SUPPORTED_KEYS and not (len(name) == 1 and name.isalnum()):
            raise ValueError(f"unsupported key {name!r}; use one of {sorted(SUPPORTED_KEYS)} or a single alphanumeric")
        self._op("input.keys", combo=name)

    def home(self) -> None:
        self._op("nav.home")

    def back(self) -> None:
        self._op("nav.back")

    def recents(self) -> None:
        self._op("nav.recents")

    def launch(self, package: str) -> str:
        return self._op("apps.launch", name=package)

    # --------------------------------------------------------------- timing
    def wait(self, seconds: float) -> None:
        self._sleep(max(0.0, float(seconds)))

    def wait_stable(
        self,
        timeout: float = STABLE_TIMEOUT_S,
        interval: float = STABLE_INTERVAL_S,
        threshold: float = STABLE_THRESHOLD,
        first: Image.Image | None = None,
    ) -> Image.Image:
        """Return a frame once two consecutive frames differ by less than `threshold`."""
        deadline = time.monotonic() + timeout
        prev = first or self.snapshot()
        while time.monotonic() < deadline:
            self._sleep(interval)
            cur = self.snapshot()
            if image_diff(prev, cur) < threshold:
                return cur
            prev = cur
        return prev

    # ------------------------------------------------------------ internals
    def _cx(self, x: int) -> int:
        return max(0, min(self.width - 1, int(round(x))))

    def _cy(self, y: int) -> int:
        return max(0, min(self.height - 1, int(round(y))))


# ----------------------------------------------------------------- helpers
def sanitize_text(text: str) -> str:
    """Fold to printable ASCII (+ newline/backspace) as input.text requires.

    Accented letters are stripped of their marks (é -> e), curly quotes and
    dashes are mapped to ASCII, and anything else non-ASCII is dropped.
    """
    replacements = {
        "‘": "'", "’": "'", "“": '"', "”": '"',
        "–": "-", "—": "-", "…": "...", " ": " ",
    }
    out = []
    for ch in text:
        mapped = replacements.get(ch)
        if mapped is not None:
            out.append(mapped)
            continue
        if ch in ("\n", "\b"):
            out.append(ch)
            continue
        if ord(ch) < 128:
            if ch.isprintable():
                out.append(ch)
            continue
        decomposed = unicodedata.normalize("NFKD", ch)
        out.extend(c for c in decomposed if ord(c) < 128 and c.isprintable())
    return "".join(out)


def _split_percent_s(text: str) -> Iterable[str]:
    """The server rejects a literal '%s'. Send '%' and 's' as separate chunks."""
    parts = text.split("%s")
    if len(parts) == 1:
        return (text,)
    out: list[str] = []
    for i, part in enumerate(parts):
        if i:
            out.append("%")
            part = "s" + part
        out.append(part)
    return tuple(out)


def image_diff(a: Image.Image, b: Image.Image) -> float:
    """Mean absolute grayscale difference in 0..1 on a tiny thumbnail (stability check)."""
    ga = a.convert("L").resize(DIFF_SIZE, Image.BILINEAR)
    gb = b.convert("L").resize(DIFF_SIZE, Image.BILINEAR)
    pa, pb = ga.tobytes(), gb.tobytes()
    total = sum(abs(x - y) for x, y in zip(pa, pb))
    return total / (255.0 * len(pa))


CHANGE_SIZE = (180, 320)
CHANGE_TOLERANCE = 24


def changed_fraction(a: Image.Image, b: Image.Image, tolerance: int = CHANGE_TOLERANCE) -> float:
    """Fraction of pixels (0..1) whose grayscale value moved by more than `tolerance`.

    Sensitive to a word typed into a field (~0.5%) while ignoring the status-bar
    clock ticking or a blinking cursor (<0.05%). Use for "did my action do anything".
    """
    if a.size != b.size:
        return 1.0
    ra = a.convert("RGB").resize(CHANGE_SIZE, Image.BILINEAR).tobytes()
    rb = b.convert("RGB").resize(CHANGE_SIZE, Image.BILINEAR).tobytes()
    # a pixel "moved" if any channel moved: catches highlight-colour changes
    # (a selected AM/PM toggle, a focused field) that grayscale would flatten out
    moved = 0
    for i in range(0, len(ra), 3):
        if (abs(ra[i] - rb[i]) > tolerance or abs(ra[i + 1] - rb[i + 1]) > tolerance
                or abs(ra[i + 2] - rb[i + 2]) > tolerance):
            moved += 1
    return moved / (len(ra) // 3)


def _b64decode(s: str) -> bytes:
    import base64

    return base64.b64decode(s)
