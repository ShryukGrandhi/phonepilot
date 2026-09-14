"""Provider adapters, exercised with stubbed SDK clients (no network)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PIL import Image

from phonepilot.brain import detect_provider, make_brain
from phonepilot.brain.base import MAX_IMAGES_IN_CONTEXT, TOOLS
from phonepilot.observe import Observation


def obs(step: int) -> Observation:
    img = Image.new("RGB", (72, 128), (step, step, step))
    return Observation(step, img, img, (), "com.x", (720, 1280), 100.0)


# ------------------------------------------------------------- factory
def test_detect_provider_prefers_explicit_then_keys(monkeypatch):
    for k in ("PHONEPILOT_BRAIN", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ValueError):
        detect_provider()
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    assert detect_provider() == "gemini"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    assert detect_provider() == "anthropic"
    monkeypatch.setenv("PHONEPILOT_BRAIN", "Gemini")
    assert detect_provider() == "gemini"


def test_make_brain_rejects_unknown_and_missing_keys(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError):
        make_brain("nope")
    with pytest.raises(ValueError):
        make_brain("anthropic")


# ----------------------------------------------------------- anthropic
class FakeAnthropicMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        n = len(self.calls)
        return SimpleNamespace(content=[
            SimpleNamespace(model_dump=lambda: {"type": "text", "text": f"thinking {n}"}),
            SimpleNamespace(model_dump=lambda: {"type": "tool_use", "id": f"tu_{n}", "name": "tap", "input": {"element": n, "reason": "because"}}),
        ])


def test_anthropic_brain_builds_tool_results_and_prunes_images(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    from phonepilot.brain.anthropic import AnthropicBrain, OMITTED

    brain = AnthropicBrain(model="claude-test")
    fake = FakeAnthropicMessages()
    brain._client = SimpleNamespace(messages=fake)
    brain.reset("task")

    a1 = brain.decide(obs(1), None)
    assert a1.name == "tap" and a1.args == {"element": 1} and a1.thought == "thinking 1 because"
    first = fake.calls[0]
    assert first["tool_choice"] == {"type": "any"} and {t["name"] for t in first["tools"]} == {t.name for t in TOOLS}
    assert first["messages"][0]["content"][0]["type"] == "text"

    for step in range(2, MAX_IMAGES_IN_CONTEXT + 3):
        brain.decide(obs(step), f"fb {step - 1}")
    msgs = fake.calls[-1]["messages"]
    user_msgs = [m for m in msgs if m["role"] == "user"]
    assert user_msgs[1]["content"][0] == {"type": "tool_result", "tool_use_id": "tu_1", "content": "fb 1"}
    images = sum(1 for m in user_msgs for b in m["content"] if b["type"] == "image")
    omitted = sum(1 for m in user_msgs for b in m["content"] if b.get("text") == OMITTED)
    assert images == MAX_IMAGES_IN_CONTEXT and omitted == len(user_msgs) - MAX_IMAGES_IN_CONTEXT
    assert msgs[1]["role"] == "assistant" and msgs[1]["content"][1]["type"] == "tool_use"


# -------------------------------------------------------------- gemini
class FakeGeminiModels:
    def __init__(self):
        self.calls = []

    def generate_content(self, **kw):
        from google.genai import types

        self.calls.append(kw)
        n = len(self.calls)
        content = types.Content(role="model", parts=[
            types.Part(text=f"look {n}"),
            types.Part(function_call=types.FunctionCall(name="scroll", args={"direction": "down", "amount": 0.5, "reason": "find it"})),
        ])
        return SimpleNamespace(candidates=[SimpleNamespace(content=content, finish_reason="STOP")], text=None)


def test_gemini_brain_roundtrip_and_pruning(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    from phonepilot.brain.gemini import GeminiBrain, OMITTED

    brain = GeminiBrain(model="gemini-test")
    fake = FakeGeminiModels()
    brain._client = SimpleNamespace(models=fake)
    brain.reset("task")

    a = brain.decide(obs(1), None)
    assert a.name == "scroll" and a.args == {"direction": "down", "amount": 0.5} and a.thought == "look 1 find it"
    for step in range(2, MAX_IMAGES_IN_CONTEXT + 3):
        brain.decide(obs(step), f"fb {step - 1}")
    contents = fake.calls[-1]["contents"]
    users = [c for c in contents if c.role == "user"]
    assert users[1].parts[0].function_response.name == "scroll"
    assert users[1].parts[0].function_response.response == {"outcome": "fb 1"}
    with_image = sum(1 for c in users if any(p.inline_data for p in c.parts))
    omitted = sum(1 for c in users if any(p.text == OMITTED for p in c.parts))
    assert with_image == MAX_IMAGES_IN_CONTEXT and omitted == len(users) - MAX_IMAGES_IN_CONTEXT
    assert fake.calls[0]["config"].tool_config.function_calling_config.mode == "ANY"


def test_gemini_brain_errors_when_no_tool_call(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    from google.genai import types

    from phonepilot.brain.gemini import GeminiBrain

    brain = GeminiBrain(model="gemini-test")
    content = types.Content(role="model", parts=[types.Part(text="I refuse")])
    brain._client = SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda **kw: SimpleNamespace(candidates=[SimpleNamespace(content=content)], text="I refuse")))
    brain.reset("task")
    with pytest.raises(RuntimeError):
        brain.decide(obs(1), None)
