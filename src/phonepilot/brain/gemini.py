"""Gemini adapter (google-genai SDK) for the Brain contract."""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from typing import Any

from google import genai
from google.genai import types

from ..observe import Observation
from .base import MAX_IMAGES_IN_CONTEXT, SYSTEM_PROMPT, TOOLS, Action, ToolSpec, coerce_action, user_turn_text

DEFAULT_MODEL = "gemini-2.5-flash"
OMITTED = "[earlier screenshot omitted to save context]"


@dataclass(frozen=True)
class _UserTurn:
    text: str
    png: bytes | None
    fn_name: str | None
    fn_response: str | None


class GeminiBrain:
    name = "gemini"

    def __init__(self, model: str | None = None, api_key: str | None = None, temperature: float = 0.2):
        self.model = model or os.environ.get("PHONEPILOT_MODEL") or DEFAULT_MODEL
        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise ValueError("GEMINI_API_KEY is not set")
        self._client = genai.Client(api_key=key)
        self._config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=temperature,
            tools=[types.Tool(function_declarations=[_declaration(t) for t in TOOLS])],
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="ANY")),
        )
        self._task = ""
        self._turns: list[tuple[str, Any]] = []  # ("user", _UserTurn) | ("model", types.Content)
        self._pending_call: str | None = None

    def reset(self, task: str) -> None:
        self._task = task
        self._turns = []
        self._pending_call = None

    def decide(self, observation: Observation, feedback: str | None) -> Action:
        text = user_turn_text(observation, feedback, self._task)
        turn = _UserTurn(
            text=text,
            png=_png_bytes(observation.marked),
            fn_name=self._pending_call,
            fn_response=feedback if self._pending_call else None,
        )
        self._turns.append(("user", turn))
        response = self._client.models.generate_content(
            model=self.model, contents=self._contents(), config=self._config
        )
        content = _first_content(response)
        self._turns.append(("model", content))
        call = next((p.function_call for p in content.parts or [] if p.function_call), None)
        if call is None:
            raise RuntimeError(f"model returned no tool call: {response.text!r}")
        self._pending_call = call.name
        thought = " ".join(p.text for p in content.parts or [] if p.text)
        return coerce_action(call.name, dict(call.args or {}), thought)

    # ------------------------------------------------------------ internals
    def _contents(self) -> list[types.Content]:
        user_indices = [i for i, (role, _) in enumerate(self._turns) if role == "user"]
        keep_images = set(user_indices[-MAX_IMAGES_IN_CONTEXT:])
        out: list[types.Content] = []
        for i, (role, item) in enumerate(self._turns):
            if role == "model":
                out.append(item)
                continue
            parts: list[types.Part] = []
            if item.fn_name:
                parts.append(types.Part.from_function_response(name=item.fn_name, response={"outcome": item.fn_response or ""}))
            parts.append(types.Part.from_text(text=item.text))
            if item.png and i in keep_images:
                parts.append(types.Part.from_bytes(data=item.png, mime_type="image/png"))
            elif item.png:
                parts.append(types.Part.from_text(text=OMITTED))
            out.append(types.Content(role="user", parts=parts))
        return out


def _declaration(spec: ToolSpec) -> types.FunctionDeclaration:
    return types.FunctionDeclaration(
        name=spec.name,
        description=spec.description,
        parameters=types.Schema(
            type="OBJECT",
            properties={k: _schema(v) for k, v in spec.properties.items()},
            required=list(spec.required),
        ),
    )


def _schema(prop: dict[str, Any]) -> types.Schema:
    kind = {"integer": "INTEGER", "number": "NUMBER", "boolean": "BOOLEAN", "string": "STRING"}.get(prop.get("type"), "STRING")
    return types.Schema(type=kind, description=prop.get("description"), enum=prop.get("enum"))


def _first_content(response) -> types.Content:
    cands = response.candidates or []
    if not cands or cands[0].content is None:
        raise RuntimeError(f"empty Gemini response: finish={getattr(cands[0], 'finish_reason', None) if cands else None}")
    return cands[0].content


def _png_bytes(image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
