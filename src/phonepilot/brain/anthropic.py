"""Anthropic Claude adapter (Messages API tool use) for the Brain contract."""

from __future__ import annotations

import base64
import io
import os
from dataclasses import dataclass
from typing import Any

import anthropic

from ..observe import Observation
from .base import MAX_IMAGES_IN_CONTEXT, SYSTEM_PROMPT, TOOLS, Action, coerce_action, user_turn_text

DEFAULT_MODEL = "claude-sonnet-5"
OMITTED = "[earlier screenshot omitted to save context]"
MAX_TOKENS = 1024


@dataclass(frozen=True)
class _UserTurn:
    text: str
    png_b64: str | None
    tool_use_id: str | None
    tool_result: str | None


class AnthropicBrain:
    name = "anthropic"

    def __init__(self, model: str | None = None, api_key: str | None = None, temperature: float = 0.2):
        self.model = model or os.environ.get("PHONEPILOT_MODEL") or DEFAULT_MODEL
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ValueError("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.Anthropic(api_key=key)
        self._temperature = temperature
        self._tools = [{"name": t.name, "description": t.description, "input_schema": t.json_schema()} for t in TOOLS]
        self._task = ""
        self._turns: list[tuple[str, Any]] = []  # ("user", _UserTurn) | ("assistant", list[block])
        self._pending_tool_use_id: str | None = None

    def reset(self, task: str) -> None:
        self._task = task
        self._turns = []
        self._pending_tool_use_id = None

    def decide(self, observation: Observation, feedback: str | None) -> Action:
        turn = _UserTurn(
            text=user_turn_text(observation, feedback, self._task),
            png_b64=_png_b64(observation.marked),
            tool_use_id=self._pending_tool_use_id,
            tool_result=feedback if self._pending_tool_use_id else None,
        )
        self._turns.append(("user", turn))
        response = self._client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            temperature=self._temperature,
            system=SYSTEM_PROMPT,
            tools=self._tools,
            tool_choice={"type": "any"},
            messages=self._messages(),
        )
        blocks = [b.model_dump() for b in response.content]
        self._turns.append(("assistant", blocks))
        tool_use = next((b for b in blocks if b.get("type") == "tool_use"), None)
        if tool_use is None:
            raise RuntimeError(f"model returned no tool call: {blocks!r}")
        self._pending_tool_use_id = tool_use["id"]
        thought = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        return coerce_action(tool_use["name"], dict(tool_use.get("input") or {}), thought)

    # ------------------------------------------------------------ internals
    def _messages(self) -> list[dict[str, Any]]:
        user_indices = [i for i, (role, _) in enumerate(self._turns) if role == "user"]
        keep_images = set(user_indices[-MAX_IMAGES_IN_CONTEXT:])
        out: list[dict[str, Any]] = []
        for i, (role, item) in enumerate(self._turns):
            if role == "assistant":
                out.append({"role": "assistant", "content": item})
                continue
            content: list[dict[str, Any]] = []
            if item.tool_use_id:
                content.append({"type": "tool_result", "tool_use_id": item.tool_use_id, "content": item.tool_result or ""})
            content.append({"type": "text", "text": item.text})
            if item.png_b64 and i in keep_images:
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": item.png_b64}})
            elif item.png_b64:
                content.append({"type": "text", "text": OMITTED})
            out.append({"role": "user", "content": content})
        return out


def _png_b64(image) -> str:
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")
