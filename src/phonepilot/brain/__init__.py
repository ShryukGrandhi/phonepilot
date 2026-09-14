"""Brain factory: pick a model provider from flags or environment."""

from __future__ import annotations

import os

from .base import Action, Brain, ToolSpec, TOOLS, SYSTEM_PROMPT, coerce_action

__all__ = ["Action", "Brain", "ToolSpec", "TOOLS", "SYSTEM_PROMPT", "coerce_action", "make_brain", "detect_provider"]


def detect_provider() -> str:
    explicit = os.environ.get("PHONEPILOT_BRAIN")
    if explicit:
        return explicit.lower()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "gemini"
    raise ValueError("no model provider configured: set ANTHROPIC_API_KEY or GEMINI_API_KEY (or PHONEPILOT_BRAIN)")


def make_brain(provider: str | None = None, model: str | None = None) -> Brain:
    provider = (provider or detect_provider()).lower()
    if provider == "anthropic":
        from .anthropic import AnthropicBrain

        return AnthropicBrain(model=model)
    if provider == "gemini":
        from .gemini import GeminiBrain

        return GeminiBrain(model=model)
    raise ValueError(f"unknown brain provider {provider!r}; use 'anthropic' or 'gemini'")
