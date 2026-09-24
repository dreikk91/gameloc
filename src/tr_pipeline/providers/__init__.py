"""Model providers. ``create_provider`` builds one from a ``[providers.NAME]`` table.

Third-party providers register under the ``tr_pipeline.providers`` entry-point group
or are referenced directly as ``type = "package.module:Class"``.
"""

from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from typing import Any

from .base import (
    AuthError,
    Completion,
    ContextOverflow,
    Provider,
    ProviderError,
    QuotaExhausted,
    RateLimited,
    TransientError,
)
from .openai import PRESETS

BUILTIN = {
    "openai": "tr_pipeline.providers.openai:OpenAIProvider",
    "gemini": "tr_pipeline.providers.gemini:GeminiProvider",
    "cli": "tr_pipeline.providers.cli:CLIProvider",
}

__all__ = ["AuthError", "BUILTIN", "Completion", "ContextOverflow", "PRESETS", "Provider", "ProviderError",
           "QuotaExhausted", "RateLimited", "TransientError", "create_provider"]


def _load(target: str) -> Any:
    module, _, attr = target.partition(":")
    return getattr(importlib.import_module(module), attr)


def create_provider(options: dict[str, Any]) -> Provider:
    options = dict(options)
    kind = str(options.get("type", "openai"))
    if kind in PRESETS:
        base_url, key_env = PRESETS[kind]
        options.setdefault("base_url", base_url)
        if key_env:
            options.setdefault("api_key_env", key_env)
        if kind == "copilot":
            options.setdefault("conversation", True)
        kind = "openai"
    if kind in BUILTIN:
        cls = _load(BUILTIN[kind])
    elif ":" in kind:
        cls = _load(kind)
    else:
        found = [ep for ep in entry_points(group="tr_pipeline.providers") if ep.name == kind]
        if not found:
            known = ", ".join(sorted({*BUILTIN, *PRESETS}))
            raise ValueError(f"unknown provider type {kind!r}; known: {known}")
        cls = found[0].load()
    provider: Provider = cls(options)
    return provider
