"""Stitch several run videos into one demo: title card, each run, closing summary.

    python scripts/make_demo_video.py runs/demo-20260914-130000 --out docs/demo.mp4
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from phonepilot.video import BG, CANVAS, DIM, FG, FPS, OK, ACCENT, _font, _para, render_video  # noqa: E402


def card(lines: list[tuple[str, int, tuple]], hold_s: float, out: Path) -> Path:
    img = Image.new("RGB", CANVAS, BG)
    d = ImageDraw.Draw(img)
    y = 90
    for text, size, color in lines:
        y = _para(d, 80, y, text, _font(size), color, CANVAS[0] - 160) + 10
    png = out.with_suffix(".png")
    img.save(png)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-framerate", str(FPS), "-t", str(hold_s), "-i", str(png),
         "-vf", "format=yuv420p", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
        check=True,
    )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("demo_dir")
    ap.add_argument("--out", default="docs/demo.mp4")
    ap.add_argument("--seconds", type=float, default=2.5)
    args = ap.parse_args()
    demo = Path(args.demo_dir)
    runs = sorted(p for p in demo.iterdir() if (p / "run.json").exists())
    if not runs:
        raise SystemExit(f"no runs in {demo}")

    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        parts: list[Path] = []
        parts.append(card([
            ("PhonePilot", 52, ACCENT),
            ("A natural-language phone agent on Phone Harness Cloud", 28, FG),
            ("Give it a sentence. It drives a real cloud Android phone: observe → decide → act → verify.", 22, DIM),
            ("Each step below shows the screenshot the model saw (numbered elements), its reasoning, the action, and what the phone did.", 22, DIM),
        ], 6, tmpd / "intro.mp4"))
        summary_lines: list[tuple[str, int, tuple]] = [("Results", 44, ACCENT)]
        for i, run in enumerate(runs, 1):
            meta = json.loads((run / "run.json").read_text(encoding="utf-8"))
            parts.append(card([
                (f"Task {i}", 40, ACCENT),
                (meta["task"], 30, FG),
                (f"model {meta['brain']}/{meta['model']} · session {meta['session_id']}", 20, DIM),
            ], 3.5, tmpd / f"title{i}.mp4"))
            parts.append(render_video(run, tmpd / f"run{i}.mp4", seconds_per_step=args.seconds))
            status = "success" if meta.get("success") else ("failed" if meta.get("success") is False else "incomplete")
            line = f"{i}. [{status}] {meta['task']}  —  {meta['steps']} steps, {meta.get('duration_s', 0):.0f}s"
            if meta.get("result"):
                line += f"  →  {meta['result']}"
            summary_lines.append((line, 22, OK if status == "success" else FG))
        summary_lines.append(("github.com/ShryukGrandhi/phonepilot", 22, DIM))
        parts.append(card(summary_lines, 8, tmpd / "outro.mp4"))

        concat = tmpd / "concat.txt"
        concat.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(FPS), str(out)], check=True)
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
