import shutil

import pytest
from PIL import Image

from phonepilot.trace import RunMeta, Trace, load_run, render_report
from phonepilot.video import render_video


def make_run(tmp_path, steps=2):
    trace = Trace(RunMeta(task="demo task", session_id="sid", brain="scripted", model="v0"), tmp_path)
    img = Image.new("RGB", (72, 128), (30, 30, 30))
    for i in range(1, steps + 1):
        trace.record(i, img, img, "com.x", 3, f"thought {i}", "tap", {"element": i}, "Tapped. Screen changed.", 1.0, 0.5)
    trace.finish(True, "did it", "42")
    return trace


def test_load_run_and_report(tmp_path):
    trace = make_run(tmp_path)
    meta, steps = load_run(trace.dir)
    assert meta["task"] == "demo task" and meta["success"] is True and len(steps) == 2
    html = (trace.dir / "report.html").read_text(encoding="utf-8")
    assert "thought 2" in html and "Result:</strong> 42" in html and "step_01_som.png" in html


def test_render_report_escapes_html():
    meta = RunMeta(task="<script>alert(1)</script>", session_id="s", brain="b", model="m")
    assert "<script>" not in render_report(meta, [])


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_render_video_produces_mp4(tmp_path):
    trace = make_run(tmp_path)
    out = render_video(trace.dir, seconds_per_step=0.5)
    assert out.exists() and out.stat().st_size > 1000


def test_render_video_requires_steps(tmp_path):
    trace = Trace(RunMeta(task="empty", session_id="s", brain="b", model="m"), tmp_path)
    trace.finish(None, "nothing")
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    with pytest.raises(ValueError):
        render_video(trace.dir)
