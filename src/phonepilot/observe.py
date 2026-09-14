"""Turn a raw phone state into what the model sees.

Two channels, deliberately redundant:

1. **Elements** — accessibility nodes that a human could plausibly act on
   (clickable, or carrying text / a description), numbered in reading order.
   The model taps by number, so a tap lands on the node's exact center instead
   of a guessed pixel.
2. **Screenshot with marks** — the same numbers drawn onto the image
   (set-of-mark prompting), so the model can match what it reads to what it sees
   and still notice things the hierarchy hides (icons, images, custom views).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont

from .device import Node

MAX_ELEMENTS = 60
MIN_NODE_PX = 6
MAX_LABEL_CHARS = 60


@dataclass(frozen=True)
class Element:
    index: int
    node: Node

    def describe(self) -> str:
        n = self.node
        parts = [f"[{self.index}]", n.short_class or "View"]
        if n.text:
            parts.append(f"text={_clip(n.text)!r}")
        if n.desc and n.desc != n.text:
            parts.append(f"desc={_clip(n.desc)!r}")
        if n.short_id:
            parts.append(f"id={n.short_id}")
        if n.clickable:
            parts.append("clickable")
        parts.append(f"@({n.x},{n.y}) {n.w}x{n.h}")
        return " ".join(parts)


@dataclass(frozen=True)
class Observation:
    step: int
    image: Image.Image
    marked: Image.Image
    elements: tuple[Element, ...]
    app: str | None
    screen: tuple[int, int]
    seconds_left: float | None

    def element(self, index: int) -> Element:
        for e in self.elements:
            if e.index == index:
                return e
        raise KeyError(f"no element [{index}] on screen; visible indices: {[e.index for e in self.elements]}")

    def elements_text(self) -> str:
        if not self.elements:
            return "(no accessibility nodes exposed; rely on the screenshot and tap_xy)"
        return "\n".join(e.describe() for e in self.elements)

    def summary(self) -> str:
        left = "unknown" if self.seconds_left is None else f"{int(self.seconds_left)}s"
        return (
            f"Step {self.step}. Foreground app: {self.app or 'unknown'}. "
            f"Screen {self.screen[0]}x{self.screen[1]} px. Session time left: {left}.\n"
            f"Interactive/text elements (index, class, text, center, size):\n{self.elements_text()}"
        )


def select_elements(nodes: Sequence[Node], screen: tuple[int, int], max_n: int = MAX_ELEMENTS) -> tuple[Element, ...]:
    """Keep actionable nodes, drop noise, number them top-to-bottom, left-to-right."""
    w, h = screen
    seen: set[tuple[str, int, int]] = set()
    kept: list[Node] = []
    for n in nodes:
        if not (n.clickable or n.text or n.desc):
            continue
        if n.w < MIN_NODE_PX or n.h < MIN_NODE_PX:
            continue
        if not (0 <= n.x < w and 0 <= n.y < h):
            continue
        key = (n.label, n.x, n.y)
        if key in seen:
            continue
        seen.add(key)
        kept.append(n)
    kept.sort(key=lambda n: (n.y // 40, n.x))
    return tuple(Element(i + 1, n) for i, n in enumerate(kept[:max_n]))


def draw_marks(image: Image.Image, elements: Sequence[Element]) -> Image.Image:
    """Overlay element boxes + index labels. Returns a new image; input untouched."""
    out = image.copy()
    draw = ImageDraw.Draw(out, "RGBA")
    font = _font(max(12, image.width // 48))
    for e in elements:
        n = e.node
        x0, y0 = n.x - n.w // 2, n.y - n.h // 2
        x1, y1 = n.x + n.w // 2, n.y + n.h // 2
        color = (255, 64, 64, 255) if n.clickable else (64, 160, 255, 255)
        draw.rectangle((x0, y0, x1, y1), outline=color, width=2)
        label = str(e.index)
        tw, th = _text_size(draw, label, font)
        lx = max(0, min(image.width - tw - 4, x0))
        ly = max(0, y0 - th - 2) if y0 - th - 2 >= 0 else y0 + 2
        draw.rectangle((lx, ly, lx + tw + 4, ly + th + 2), fill=color)
        draw.text((lx + 2, ly), label, fill=(255, 255, 255, 255), font=font)
    return out


def _clip(s: str, n: int = MAX_LABEL_CHARS) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


_FONT_CACHE: dict[int, ImageFont.ImageFont] = {}


def _font(size: int) -> ImageFont.ImageFont:
    if size not in _FONT_CACHE:
        try:
            _FONT_CACHE[size] = ImageFont.truetype("arial.ttf", size)
        except OSError:
            try:
                _FONT_CACHE[size] = ImageFont.truetype("DejaVuSans.ttf", size)
            except OSError:
                _FONT_CACHE[size] = ImageFont.load_default()
    return _FONT_CACHE[size]


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]
