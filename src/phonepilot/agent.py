"""The observe → decide → act → verify loop.

One step:
1. observe: fresh screenshot + accessibility tree + foreground app → Observation
2. decide: the Brain picks one tool call
3. act: run it on the Device
4. verify: capture again, measure how much the screen changed, and hand that
   verdict back to the Brain as feedback for the next decision

The verify capture doubles as the next step's observation image, so each step
costs one screenshot, not two. Repeated identical actions that change nothing
get an explicit nudge; the run stops on `done`, on the step cap, or when the
session deadline is near.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image

from .brain.base import Action, Brain
from .cloud import CloudError
from .device import Device, changed_fraction
from .observe import Observation, draw_marks, select_elements
from .trace import RunMeta, Trace

CHANGE_THRESHOLD = 0.001  # fraction of pixels that must move for "screen changed" (AM/PM toggle ≈ 0.15%, clock tick ≈ 0.01%)
SETTLE_S = {"tap": 0.8, "tap_xy": 0.8, "long_press": 0.6, "type_text": 0.6, "press_key": 0.6,
            "scroll": 1.0, "swipe": 1.0, "launch_app": 1.5, "navigate": 0.8, "wait": 0.0}
REPEAT_LIMIT = 3
MIN_SECONDS_LEFT = 25
TRANSITION_FRACTION = 0.25   # more than this much of the screen moved -> maybe still animating
TRANSITION_SETTLE_S = 1.0


@dataclass(frozen=True)
class AgentConfig:
    max_steps: int = 25
    min_seconds_left: int = MIN_SECONDS_LEFT
    change_threshold: float = CHANGE_THRESHOLD
    max_extra_captures: int = 2


@dataclass(frozen=True)
class Outcome:
    success: bool | None
    summary: str
    result: str | None
    steps: int
    run_dir: Path
    error: str | None = None


class Agent:
    def __init__(
        self,
        device: Device,
        brain: Brain,
        trace: Trace,
        config: AgentConfig = AgentConfig(),
        log: Callable[[str], None] = print,
    ):
        self.device = device
        self.brain = brain
        self.trace = trace
        self.config = config
        self.log = log
        self._next_image: Image.Image | None = None

    # ------------------------------------------------------------------ run
    def run(self, task: str) -> Outcome:
        self.brain.reset(task)
        feedback: str | None = None
        recent: deque[str] = deque(maxlen=REPEAT_LIMIT)
        unchanged_streak = 0
        step = 0
        last_action: Action | None = None
        try:
            for step in range(1, self.config.max_steps + 1):
                left = self.device.seconds_left()
                if left is not None and left < self.config.min_seconds_left:
                    return self._finish(None, f"stopped: session expires in {int(left)}s", None, step - 1)

                obs = self.observe(step)
                if last_action is not None and feedback:
                    feedback = self.verify_typed(last_action, feedback, obs)
                t0 = time.monotonic()
                action = self.brain.decide(obs, feedback)
                last_action = action
                llm_s = time.monotonic() - t0
                self.log(f"[{step}] {action.signature()}  — {action.thought}")

                if action.name == "done":
                    self.trace.record(step, obs.image, obs.marked, obs.app, len(obs.elements), action.thought,
                                      action.name, action.args, "task finished", llm_s, 0.0)
                    return self._finish(bool(action.args.get("success")), action.args.get("summary", ""),
                                        action.args.get("result"), step)

                t1 = time.monotonic()
                feedback, changed = self.execute(action, obs)
                act_s = time.monotonic() - t1

                recent.append(action.signature())
                unchanged_streak = 0 if changed else unchanged_streak + 1
                if len(recent) == REPEAT_LIMIT and len(set(recent)) == 1 and unchanged_streak >= REPEAT_LIMIT:
                    feedback += (" You have repeated this exact action several times with no effect. "
                                 "Stop repeating it: pick a different element, scroll, go back, or report failure.")
                self.log(f"    -> {feedback}")
                self.trace.record(step, obs.image, obs.marked, obs.app, len(obs.elements), action.thought,
                                  action.name, action.args, feedback, llm_s, act_s)
            return self._finish(None, f"stopped: reached step cap ({self.config.max_steps})", None, step)
        except CloudError as exc:
            return self._finish(False, "phone API error", None, step, error=str(exc))
        except KeyboardInterrupt:
            return self._finish(None, "interrupted by user", None, step)
        except Exception as exc:  # noqa: BLE001 — the run must always be recorded
            return self._finish(False, f"agent crashed: {type(exc).__name__}", None, step, error=str(exc))

    # -------------------------------------------------------------- observe
    def observe(self, step: int) -> Observation:
        image = self._next_image or self.device.screenshot()
        self._next_image = None
        try:
            nodes = self.device.tree()
        except CloudError as exc:
            self.log(f"    tree failed ({exc}); continuing with screenshot only")
            nodes = ()
        try:
            app = self.device.current_app()
        except CloudError:
            app = None
        screen = (self.device.width, self.device.height)
        elements = select_elements(nodes, screen)
        return Observation(
            step=step, image=image, marked=draw_marks(image, elements), elements=elements,
            app=app, screen=screen, seconds_left=self.device.seconds_left(),
        )

    # -------------------------------------------------------------- execute
    def execute(self, action: Action, obs: Observation) -> tuple[str, bool]:
        try:
            description = self._dispatch(action, obs)
        except (KeyError, ValueError) as exc:
            return f"Action rejected: {exc}", False
        except CloudError as exc:
            if exc.status in (400, 409):
                return f"Phone refused the action ({exc.payload.get('error', exc)})", False
            raise
        time.sleep(SETTLE_S.get(action.name, 0.8))
        after = self.settled_capture(obs.image)
        self._next_image = after
        moved = changed_fraction(obs.image, after)
        changed = moved > self.config.change_threshold
        verdict = f"Screen changed ({moved:.0%} of pixels)." if changed else "Screen did NOT change (possible no-op)."
        return f"{description}. {verdict}", changed

    def settled_capture(self, before: Image.Image) -> Image.Image:
        """Capture after an action; if a big transition is still in flight, capture again.

        Small changes (a typed word, a toggled switch) are accepted at once. When most
        of the screen moved, the phone is probably mid-animation (crossfade, slide),
        so wait and re-capture until two consecutive frames agree, bounded by
        `max_extra_captures`.
        """
        frame = self.device.screenshot()
        extra = 0
        while changed_fraction(before, frame) > TRANSITION_FRACTION and extra < self.config.max_extra_captures:
            time.sleep(TRANSITION_SETTLE_S)
            nxt = self.device.screenshot()
            extra += 1
            if changed_fraction(frame, nxt) <= self.config.change_threshold:
                return nxt
            before, frame = frame, nxt
        return frame

    @staticmethod
    def verify_typed(action: Action, feedback: str, obs: Observation) -> str:
        """After type_text, confirm the text is actually visible in the new hierarchy."""
        if action.name != "type_text" or not obs.elements:
            return feedback
        wanted = str(action.args.get("text", "")).strip().lower()
        if not wanted:
            return feedback
        hits = [e.index for e in obs.elements if wanted in e.node.text.lower()]
        if hits:
            return f"{feedback} Typed text is visible in element {hits[0]}."
        return f"{feedback} WARNING: the typed text is not visible in any element; the field may not have been focused."

    def _dispatch(self, action: Action, obs: Observation) -> str:
        a, d = action.args, self.device
        name = action.name
        if name == "tap":
            e = obs.element(a["element"])
            d.tap(e.node.x, e.node.y)
            return f"Tapped [{e.index}] {e.node.label!r} at ({e.node.x},{e.node.y})"
        if name == "tap_xy":
            d.tap(a["x"], a["y"])
            return f"Tapped ({a['x']},{a['y']})"
        if name == "long_press":
            e = obs.element(a["element"])
            d.long_press(e.node.x, e.node.y, a.get("duration", 0.8))
            return f"Long-pressed [{e.index}] {e.node.label!r}"
        if name == "type_text":
            target = ""
            if a.get("element") is not None:
                e = obs.element(a["element"])
                d.tap(e.node.x, e.node.y)
                time.sleep(0.5)
                target = f" into [{e.index}] {e.node.label!r}"
            sent = d.type_text(a["text"], submit=bool(a.get("submit")))
            return f"Typed {sent!r}{target}{' and pressed Enter' if a.get('submit') else ''}"
        if name == "press_key":
            d.key(a["key"])
            return f"Pressed key {a['key']}"
        if name == "scroll":
            amount = _clamp(a.get("amount", 0.5), 0.2, 0.9)
            d.scroll(a["direction"], amount)
            return f"Scrolled {a['direction']} by {amount:.1f} screen"
        if name == "swipe":
            d.swipe(a["direction"])
            return f"Swiped {a['direction']}"
        if name == "launch_app":
            launched = d.launch(a["package"])
            return f"Launched {launched}"
        if name == "navigate":
            {"home": d.home, "back": d.back, "recents": d.recents}[a["action"]]()
            return f"Navigated {a['action']}"
        if name == "wait":
            secs = _clamp(a.get("seconds", 2.0), 0.5, 8.0)
            d.wait(secs)
            return f"Waited {secs:.1f}s"
        raise ValueError(f"unknown action {name}")

    def _finish(self, success: bool | None, summary: str, result: str | None, steps: int, error: str | None = None) -> Outcome:
        self.trace.finish(success, summary, result, error)
        return Outcome(success, summary, result, steps, self.trace.dir, error)


def _clamp(v: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return lo


def new_trace(task: str, session_id: str, brain: Brain, runs_dir: Path | None = None,
              price_cents_per_minute: int | None = None, ready_wait_s: float | None = None) -> Trace:
    meta = RunMeta(task=task, session_id=session_id, brain=brain.name, model=brain.model,
                   price_cents_per_minute=price_cents_per_minute, ready_wait_s=ready_wait_s)
    return Trace(meta, runs_dir)
