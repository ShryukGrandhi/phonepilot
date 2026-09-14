"""Render a recorded run into an mp4: phone on the left, reasoning on the right."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .trace import load_run

CANVAS = (1280, 720)
PHONE_H = 680
BG = (15, 17, 21)
FG = (230, 230, 230)
DIM = (140, 150, 165)
ACCENT = (255, 212, 121)
OK = (155, 226, 155)
FPS = 12


def render_video(run_dir: Path, out: Path | None = None, seconds_per_step: float = 2.5) -> Path:
    if shutil.which("ffmpeg") is None:
        raise ValueError("ffmpeg not found on PATH")
    meta, steps = load_run(run_dir)
    if not steps:
        raise ValueError(f"no steps recorded in {run_dir}")
    out = out or run_dir / "demo.mp4"
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        frames = []
        for i, s in enumerate(steps):
            frame = _compose(run_dir, meta, s, i + 1, len(steps))
            p = tmpdir / f"f{i:03d}.png"
            frame.save(p)
            frames.append(p)
        concat = tmpdir / "list.txt"
        with concat.open("w", encoding="utf-8") as f:
            for p in frames:
                f.write(f"file '{p.as_posix()}'\nduration {seconds_per_step}\n")
            f.write(f"file '{frames[-1].as_posix()}'\n")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
             "-vf", f"fps={FPS},format=yuv420p", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
            check=True,
        )
    return out


def _compose(run_dir: Path, meta: dict, s: dict, n: int, total: int) -> Image.Image:
    canvas = Image.new("RGB", CANVAS, BG)
    shot = Image.open(run_dir / s["marked"]).convert("RGB")
    scale = PHONE_H / shot.height
    shot = shot.resize((int(shot.width * scale), PHONE_H), Image.LANCZOS)
    canvas.paste(shot, (20, (CANVAS[1] - PHONE_H) // 2))

    d = ImageDraw.Draw(canvas)
    x = 40 + shot.width
    width = CANVAS[0] - x - 30
    big, med, small = _font(26), _font(19), _font(16)
    y = 30
    d.text((x, y), "PhonePilot", font=big, fill=ACCENT)
    d.text((x + 160, y + 6), f"step {n}/{total} · {s.get('app') or ''}", font=small, fill=DIM)
    y += 46
    y = _para(d, x, y, f"Task: {meta['task']}", med, FG, width)
    y += 14
    y = _para(d, x, y, s.get("thought") or "", med, FG, width)
    y += 12
    args = ", ".join(f"{k}={v!r}" for k, v in (s.get("args") or {}).items())
    y = _para(d, x, y, f"→ {s['action']}({args})", med, ACCENT, width)
    y += 12
    y = _para(d, x, y, s.get("feedback") or "", small, OK, width)
    if n == total and meta.get("summary"):
        y += 18
        y = _para(d, x, y, f"Outcome: {meta['summary']}", med, FG, width)
        if meta.get("result"):
            _para(d, x, y + 6, f"Result: {meta['result']}", med, OK, width)
    return canvas


def _para(d: ImageDraw.ImageDraw, x: int, y: int, text: str, font, fill, width: int) -> int:
    approx_char = max(6, font.size * 0.55)
    for line in textwrap.wrap(text, width=max(20, int(width / approx_char))) or [""]:
        d.text((x, y), line, font=font, fill=fill)
        y += font.size + 6
    return y


_FONTS: dict[int, ImageFont.FreeTypeFont] = {}


def _font(size: int):
    if size not in _FONTS:
        for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
            try:
                _FONTS[size] = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
        else:
            _FONTS[size] = ImageFont.load_default()
    return _FONTS[size]
