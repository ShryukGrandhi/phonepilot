"""The contract between the agent loop and whichever model drives it.

A Brain sees an Observation (numbered elements + marked screenshot) plus the
outcome of its previous action, and answers with exactly one Action from the
tool vocabulary below. The vocabulary is provider-neutral JSON schema; each
provider adapter (anthropic.py, gemini.py) translates it into its own
function-calling format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..observe import Observation

MAX_IMAGES_IN_CONTEXT = 3  # older screenshots are dropped from the model's context


@dataclass(frozen=True)
class Action:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    thought: str = ""

    def signature(self) -> str:
        return f"{self.name}({', '.join(f'{k}={v!r}' for k, v in sorted(self.args.items()))})"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    properties: dict[str, Any]
    required: tuple[str, ...] = ()

    def json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": self.properties, "required": list(self.required)}


KEY_NAMES = [
    "enter", "tab", "space", "backspace", "delete", "escape", "home", "back", "recents",
    "menu", "up", "down", "left", "right", "power", "volumeup", "volumedown", "search",
]

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "tap",
        "Tap the center of a numbered on-screen element. Preferred over tap_xy.",
        {"element": {"type": "integer", "description": "Index shown as [n] in the element list and drawn on the screenshot."}},
        ("element",),
    ),
    ToolSpec(
        "tap_xy",
        "Tap at raw device-pixel coordinates. Only when the target has no element number (icons, custom views).",
        {"x": {"type": "integer"}, "y": {"type": "integer"}},
        ("x", "y"),
    ),
    ToolSpec(
        "long_press",
        "Press and hold an element (context menus, selection, drag handles).",
        {
            "element": {"type": "integer"},
            "duration": {"type": "number", "description": "Seconds, default 0.8."},
        },
        ("element",),
    ),
    ToolSpec(
        "type_text",
        "Type text into a field. Taps the element first if given. Printable ASCII only; set submit=true to press Enter after.",
        {
            "text": {"type": "string"},
            "element": {"type": "integer", "description": "Optional field to tap first."},
            "submit": {"type": "boolean", "description": "Press Enter after typing."},
        },
        ("text",),
    ),
    ToolSpec(
        "press_key",
        "Press one Android key.",
        {"key": {"type": "string", "enum": KEY_NAMES}},
        ("key",),
    ),
    ToolSpec(
        "scroll",
        "Scroll to reveal more content in a direction: scroll 'down' shows what is further down the page.",
        {
            "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
            "amount": {"type": "number", "description": "Fraction of the screen, 0.2-0.9. Default 0.5."},
        },
        ("direction",),
    ),
    ToolSpec(
        "swipe",
        "Move the finger in a direction (feeds, carousels, pull-to-refresh, dismiss). swipe 'up' = thumb moves up = next item.",
        {"direction": {"type": "string", "enum": ["up", "down", "left", "right"]}},
        ("direction",),
    ),
    ToolSpec(
        "launch_app",
        "Launch an app by Android package name, e.g. com.android.settings, com.android.contacts, com.android.deskclock.",
        {"package": {"type": "string"}},
        ("package",),
    ),
    ToolSpec(
        "navigate",
        "System navigation: home, back, or recents.",
        {"action": {"type": "string", "enum": ["home", "back", "recents"]}},
        ("action",),
    ),
    ToolSpec(
        "wait",
        "Wait for the screen to change (loading, animations). Use sparingly.",
        {"seconds": {"type": "number", "description": "1-5 seconds."}},
        ("seconds",),
    ),
    ToolSpec(
        "done",
        "Finish the task. Use success=false if the task is impossible on this phone; explain why.",
        {
            "success": {"type": "boolean"},
            "summary": {"type": "string", "description": "One or two sentences on what was done."},
            "result": {"type": "string", "description": "The answer or extracted data the user asked for, if any."},
        },
        ("success", "summary"),
    ),
)

REASON_PROP = {
    "reason": {"type": "string", "description": "One short sentence: what you see and why this action."}
}


def _with_reason(spec: ToolSpec) -> ToolSpec:
    return ToolSpec(spec.name, spec.description, {**REASON_PROP, **spec.properties}, spec.required)


TOOLS = tuple(_with_reason(t) for t in TOOLS)
TOOLS_BY_NAME = {t.name: t for t in TOOLS}


SYSTEM_PROMPT = """You are PhonePilot, an agent operating a real Android phone to complete a user's task.

Each turn you receive:
- the foreground app and a numbered list of on-screen elements from the accessibility tree
- a screenshot with those same numbers drawn as labels (red = clickable, blue = text only)
- the outcome of your previous action, including whether the screen changed

You reply with exactly one tool call per turn. Think briefly, then act.

Rules
1. Prefer `tap` with an element number. Use `tap_xy` only for things with no number.
2. Verify from the new screenshot that the previous action did what you expected before moving on. If the screen did not change, do not repeat the same action blindly; try another element, scroll, or a different route.
3. To type, make sure a text field is focused: pass its element number to `type_text`. Keep text ASCII.
4. Use `scroll` to find content that is off screen; check the element list after scrolling. On the home screen, `swipe` up opens the app drawer and `swipe` down opens the notification shade; prefer `launch_app` with a package name when you know it.
5. Before confirming a dialog (OK, Save, Done), check every field in it against the task, including AM/PM on time pickers and any default the dialog pre-filled.
6. Apps available on this phone are stock AOSP: Settings (com.android.settings), Contacts (com.android.contacts), Clock (com.android.deskclock), Calendar (com.android.calendar), Camera (com.android.camera2), Gallery (com.android.gallery3d), Files (com.android.documentsui), Music (com.android.music), Phone (com.android.dialer / com.android.phone). There is no Google account, Play Store access, or Chrome. If the task needs something the phone cannot do, finish with success=false and say why.
7. Stay within the task. Do not change unrelated settings, delete data, or make purchases. Never try to solve a CAPTCHA or "verify you are human" check, and never enter passwords or payment details. If the task needs information you were not given (an address, an account, a card), do not invent it: finish with success=false and say exactly what is missing.
8. Before calling `done`, make sure the current screenshot shows the end state the task asked for (the saved item in its list, the toggle in its new position, the value you were asked to read). A toast or a transition frame is not confirmation; navigate to the list or reopen the item if needed.
9. When the task is complete, call `done` with success=true, a short summary, and the concrete result if the user asked for information. Read values from the screen; never guess.
10. You have limited steps and limited session time. Be decisive."""


class Brain(Protocol):
    name: str
    model: str

    def reset(self, task: str) -> None: ...

    def decide(self, observation: Observation, feedback: str | None) -> Action: ...


def user_turn_text(observation: Observation, feedback: str | None, task: str) -> str:
    parts = []
    if feedback:
        parts.append(f"Previous action outcome: {feedback}")
    parts.append(f"Task: {task}")
    parts.append(observation.summary())
    parts.append("Choose the single next tool call.")
    return "\n\n".join(parts)


def coerce_action(name: str, args: dict[str, Any] | None, thought: str = "") -> Action:
    """Validate a raw function call against the tool specs and normalise arg types."""
    spec = TOOLS_BY_NAME.get(name)
    if spec is None:
        raise ValueError(f"unknown tool {name!r}; valid: {sorted(TOOLS_BY_NAME)}")
    args = dict(args or {})
    missing = [k for k in spec.required if k not in args]
    if missing:
        raise ValueError(f"{name} missing required args {missing}")
    clean: dict[str, Any] = {}
    reason = str(args.pop("reason", "") or "")
    for key, value in args.items():
        prop = spec.properties.get(key)
        if prop is None:
            continue
        clean[key] = _coerce(value, prop.get("type"))
    full_thought = " ".join(t for t in (thought.strip(), reason.strip()) if t)
    return Action(name=name, args=clean, thought=full_thought)


def _coerce(value: Any, typ: str | None) -> Any:
    try:
        if typ == "integer":
            return int(round(float(value)))
        if typ == "number":
            return float(value)
        if typ == "boolean":
            if isinstance(value, str):
                return value.strip().lower() in ("true", "1", "yes")
            return bool(value)
        if typ == "string":
            return str(value)
    except (TypeError, ValueError):
        pass
    return value
