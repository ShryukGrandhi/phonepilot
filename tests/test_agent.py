"""Agent loop against the fake phone with a scripted brain."""

from __future__ import annotations

import json

import pytest

from phonepilot.agent import Agent, AgentConfig, new_trace
from phonepilot.brain.base import Action, TOOLS, coerce_action
from phonepilot.device import Device
from tests.conftest import node


class ScriptedBrain:
    name = "scripted"
    model = "v0"

    def __init__(self, actions):
        self.actions = list(actions)
        self.seen = []

    def reset(self, task):
        self.task = task

    def decide(self, observation, feedback):
        self.seen.append((observation, feedback))
        return self.actions.pop(0)


@pytest.fixture
def device(client, phone, monkeypatch):
    phone.state = "ready"
    monkeypatch.setattr("phonepilot.agent.time.sleep", lambda s: None)
    return Device(client, client.get_session("fake123"), sleep=lambda s: None)


def run(device, brain, tmp_path, task="do the thing", **cfg):
    trace = new_trace(task, device.sid, brain, tmp_path, price_cents_per_minute=35)
    agent = Agent(device, brain, trace, AgentConfig(**cfg), log=lambda m: None)
    return agent.run(task), trace


def test_happy_path_taps_element_then_done(device, phone, tmp_path):
    phone.tree = [node("Settings", x=360, y=640)]
    brain = ScriptedBrain([
        Action("tap", {"element": 1}, "tap settings"),
        Action("done", {"success": True, "summary": "opened", "result": "Settings"}),
    ])
    outcome, trace = run(device, brain, tmp_path)
    assert outcome.success and outcome.steps == 2 and outcome.result == "Settings"
    assert ("input.tap", {"x": 360, "y": 640}) in phone.ops_log
    # feedback for step 2 reports the tap and that the screen changed
    _, feedback = brain.seen[1]
    assert feedback.startswith("Tapped [1] 'Settings' at (360,640)") and "Screen changed" in feedback
    meta = json.loads((trace.dir / "run.json").read_text())
    assert meta["success"] is True and meta["steps"] == 2 and meta["estimated_cost_cents"] == 35
    assert (trace.dir / "report.html").exists() and (trace.dir / "step_01_som.png").exists()
    lines = (trace.dir / "trace.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["action"] == "tap"


def test_unknown_element_is_rejected_not_fatal(device, phone, tmp_path):
    brain = ScriptedBrain([Action("tap", {"element": 42}), Action("done", {"success": False, "summary": "gave up"})])
    outcome, _ = run(device, brain, tmp_path)
    assert outcome.success is False
    _, feedback = brain.seen[1]
    assert feedback.startswith("Action rejected: ") and "no element [42]" in feedback


def test_no_op_repeats_get_a_nudge(device, phone, tmp_path):
    brain = ScriptedBrain([Action("navigate", {"action": "home"})] * 3 + [Action("done", {"success": False, "summary": "stuck"})])
    run(device, brain, tmp_path)
    feedbacks = [fb for _, fb in brain.seen if fb]
    assert "did NOT change" in feedbacks[0]
    assert "repeated this exact action" not in feedbacks[1]
    assert "repeated this exact action" in feedbacks[2]


def test_step_cap_ends_run_incomplete(device, phone, tmp_path):
    brain = ScriptedBrain([Action("wait", {"seconds": 1})] * 5)
    outcome, _ = run(device, brain, tmp_path, max_steps=3)
    assert outcome.success is None and "step cap" in outcome.summary and outcome.steps == 3


def test_phone_refusal_is_reported_as_feedback(device, phone, tmp_path):
    class FaultInjecting(ScriptedBrain):
        def decide(self, observation, feedback):
            # arm the fault after observe() ran tree/apps.current, so the next op (input.text) is the one refused
            phone.fail_next_op = {"status": 400, "payload": {"error": "input.text: shlut rejected the operation arguments"}}
            return super().decide(observation, feedback)

    brain = FaultInjecting([Action("type_text", {"text": "x"}), Action("done", {"success": True, "summary": "ok"})])
    run(device, brain, tmp_path)
    _, feedback = brain.seen[1]
    assert feedback.startswith("Phone refused the action")


def test_brain_crash_is_recorded(device, phone, tmp_path):
    class Boom(ScriptedBrain):
        def decide(self, observation, feedback):
            raise RuntimeError("model down")

    outcome, trace = run(device, Boom([]), tmp_path)
    assert outcome.success is False and outcome.error == "model down"
    assert json.loads((trace.dir / "run.json").read_text())["error"] == "model down"


def test_session_expiry_stops_before_acting(device, phone, tmp_path):
    device.expires_at = 0  # already expired
    outcome, _ = run(device, ScriptedBrain([]), tmp_path)
    assert outcome.success is None and "expires" in outcome.summary and outcome.steps == 0


def test_launch_and_type_dispatch(device, phone, tmp_path):
    phone.tree = [node("Search", x=300, y=450, cls="android.widget.EditText")]
    brain = ScriptedBrain([
        Action("launch_app", {"package": "com.android.settings"}),
        Action("type_text", {"text": "dark", "element": 1, "submit": True}),
        Action("scroll", {"direction": "down", "amount": 5}),   # clamped to 0.9
        Action("done", {"success": True, "summary": "ok"}),
    ])
    outcome, _ = run(device, brain, tmp_path)
    assert outcome.success
    ops = [op for op, _ in phone.ops_log]
    assert "apps.launch" in ops and ("input.text", {"s": "dark"}) in phone.ops_log and ("input.keys", {"combo": "enter"}) in phone.ops_log
    assert brain.seen[1][1].startswith("Launched com.android.settings")
    assert "Scrolled down by 0.9" in brain.seen[3][1]


def test_typed_text_is_verified_against_next_tree(device, phone, tmp_path):
    phone.tree = [node("Search", x=300, y=450, cls="android.widget.EditText")]

    class TreeUpdating(ScriptedBrain):
        def decide(self, observation, feedback):
            phone.tree = [node("dark", x=300, y=450, cls="android.widget.EditText")]  # field now shows the text
            return super().decide(observation, feedback)

    brain = TreeUpdating([
        Action("type_text", {"text": "dark", "element": 1}),
        Action("type_text", {"text": "zzz", "element": 1}),
        Action("done", {"success": True, "summary": "ok"}),
    ])
    run(device, brain, tmp_path)
    assert "Typed text is visible in element 1" in brain.seen[1][1]
    assert "WARNING: the typed text is not visible" in brain.seen[2][1]


def test_settled_capture_waits_out_a_transition(device, phone, tmp_path):
    from PIL import Image

    from phonepilot.trace import RunMeta, Trace

    def solid(v):
        return Image.new("RGB", (720, 1280), (v, v, v))

    frames = iter([solid(120), solid(200), solid(200)])  # mid-animation, then stable
    device.screenshot = lambda: next(frames)
    agent = Agent(device, ScriptedBrain([]), Trace(RunMeta("t", "s", "b", "m"), tmp_path), log=lambda m: None)
    settled = agent.settled_capture(solid(0))
    assert settled.getpixel((0, 0)) == (200, 200, 200)

    frames = iter([solid(0), solid(255)])  # small/no change: accepted at once, no second capture
    device.screenshot = lambda: next(frames)
    assert agent.settled_capture(solid(0)).getpixel((0, 0)) == (0, 0, 0)


def test_coerce_action_validates_and_extracts_reason():
    a = coerce_action("tap", {"element": "3", "reason": "it is the button", "bogus": 1}, "thinking")
    assert a.args == {"element": 3} and a.thought == "thinking it is the button"
    with pytest.raises(ValueError):
        coerce_action("fly", {})
    with pytest.raises(ValueError):
        coerce_action("tap", {})
    assert all("reason" in t.properties for t in TOOLS)
