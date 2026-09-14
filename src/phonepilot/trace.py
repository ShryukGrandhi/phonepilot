"""Run recording: every step's screenshot, reasoning, action and outcome.

A run directory looks like

    runs/20260914-131500-set-an-alarm/
        run.json          task, session, model, outcome, timings, cost estimate
        trace.jsonl       one line per step
        step_01.png       raw screenshot the agent saw
        step_01_som.png   same screenshot with element marks
        report.html       self-contained visual walkthrough

The HTML report is what you open after a run to see *why* the agent did what
it did; `phonepilot video` turns the same data into an mp4.
"""

from __future__ import annotations

import html
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

DEFAULT_RUNS_DIR = Path("runs")


@dataclass(frozen=True)
class StepRecord:
    step: int
    t: float
    app: str | None
    elements: int
    thought: str
    action: str
    args: dict[str, Any]
    feedback: str
    screenshot: str
    marked: str
    latency_llm_s: float
    latency_act_s: float


@dataclass
class RunMeta:
    task: str
    session_id: str
    brain: str
    model: str
    started: float = field(default_factory=time.time)
    ended: float | None = None
    success: bool | None = None
    summary: str = ""
    result: str | None = None
    steps: int = 0
    error: str | None = None
    price_cents_per_minute: int | None = None
    ready_wait_s: float | None = None

    @property
    def duration_s(self) -> float:
        return (self.ended or time.time()) - self.started

    def estimated_cost_cents(self) -> int | None:
        if self.price_cents_per_minute is None:
            return None
        minutes = int(self.duration_s // 60) + 1
        return minutes * self.price_cents_per_minute


class Trace:
    def __init__(self, meta: RunMeta, root: Path | None = None):
        self.meta = meta
        slug = re.sub(r"[^a-z0-9]+", "-", meta.task.lower()).strip("-")[:40] or "run"
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(meta.started))
        self.dir = (root or DEFAULT_RUNS_DIR) / f"{stamp}-{slug}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.records: list[StepRecord] = []
        self._write_meta()

    def record(
        self,
        step: int,
        image: Image.Image,
        marked: Image.Image,
        app: str | None,
        n_elements: int,
        thought: str,
        action_name: str,
        args: dict[str, Any],
        feedback: str,
        latency_llm_s: float,
        latency_act_s: float,
    ) -> StepRecord:
        shot = f"step_{step:02d}.png"
        som = f"step_{step:02d}_som.png"
        image.save(self.dir / shot)
        marked.save(self.dir / som)
        rec = StepRecord(
            step=step, t=time.time(), app=app, elements=n_elements, thought=thought,
            action=action_name, args=args, feedback=feedback, screenshot=shot, marked=som,
            latency_llm_s=round(latency_llm_s, 2), latency_act_s=round(latency_act_s, 2),
        )
        self.records.append(rec)
        with (self.dir / "trace.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
        self.meta.steps = step
        self._write_meta()
        return rec

    def finish(self, success: bool | None, summary: str, result: str | None = None, error: str | None = None) -> None:
        self.meta.ended = time.time()
        self.meta.success = success
        self.meta.summary = summary
        self.meta.result = result
        self.meta.error = error
        self._write_meta()
        (self.dir / "report.html").write_text(render_report(self.meta, self.records), encoding="utf-8")

    def _write_meta(self) -> None:
        data = asdict(self.meta)
        data["duration_s"] = round(self.meta.duration_s, 1)
        data["estimated_cost_cents"] = self.meta.estimated_cost_cents()
        (self.dir / "run.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_run(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    lines = (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines() if (run_dir / "trace.jsonl").exists() else []
    return meta, [json.loads(line) for line in lines if line.strip()]


def render_report(meta: RunMeta, records: list[StepRecord]) -> str:
    esc = html.escape
    status = "✅ success" if meta.success else ("❌ failed" if meta.success is False else "⚠️ incomplete")
    cost = meta.estimated_cost_cents()
    cards = []
    for r in records:
        args = ", ".join(f"{k}={v!r}" for k, v in r.args.items())
        cards.append(
            f"""<article class="step">
  <img src="{esc(r.marked)}" alt="step {r.step}" loading="lazy">
  <div class="body">
    <h2>Step {r.step} <span class="app">{esc(r.app or '')}</span></h2>
    <p class="thought">{esc(r.thought) or '<em>no reasoning text</em>'}</p>
    <p class="action"><code>{esc(r.action)}({esc(args)})</code></p>
    <p class="feedback">{esc(r.feedback)}</p>
    <p class="meta">{r.elements} elements · model {r.latency_llm_s}s · phone {r.latency_act_s}s</p>
  </div>
</article>"""
        )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>PhonePilot run: {esc(meta.task)}</title>
<style>
body{{font:15px/1.45 system-ui,sans-serif;margin:0;padding:24px;background:#0f1115;color:#e6e6e6}}
h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#9aa;margin:0 0 24px}}
.step{{display:grid;grid-template-columns:270px 1fr;gap:18px;background:#181b22;border:1px solid #262a33;border-radius:12px;padding:14px;margin-bottom:14px}}
.step img{{width:270px;border-radius:8px;background:#000}}
h2{{font-size:16px;margin:0 0 8px}} .app{{font-weight:400;color:#8ab4f8;font-size:13px;margin-left:8px}}
.thought{{color:#d5d9e0;margin:0 0 8px}} .action code{{background:#101318;padding:4px 8px;border-radius:6px;color:#ffd479}}
.feedback{{color:#9be29b;margin:8px 0 0}} .meta{{color:#778;font-size:12px;margin:6px 0 0}}
.result{{background:#12251a;border:1px solid #1f4d2f;border-radius:10px;padding:12px 16px;margin-bottom:22px}}
</style></head><body>
<h1>{esc(meta.task)}</h1>
<p class="sub">{status} · {meta.steps} steps · {meta.duration_s:.0f}s · session {esc(meta.session_id)} · {esc(meta.brain)}/{esc(meta.model)}{f' · ~{cost/100:.2f} USD phone time' if cost is not None else ''}</p>
<div class="result"><strong>Summary:</strong> {esc(meta.summary)}{f'<br><strong>Result:</strong> {esc(meta.result)}' if meta.result else ''}{f'<br><strong>Error:</strong> {esc(meta.error)}' if meta.error else ''}</div>
{''.join(cards)}
</body></html>"""
