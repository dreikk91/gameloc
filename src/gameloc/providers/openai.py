"""OpenAI-compatible Chat Completions: OpenAI, Anthropic, OpenRouter, DeepSeek, Mistral,
Groq, Gemini (OpenAI endpoint), Ollama, LM Studio, vLLM, Copilot proxies..."""

from __future__ import annotations

import threading
from typing import Any

from .base import Completion, Provider, QuotaExhausted, TransientError, api_key, post_json

# type -> (base_url, api key environment variable)
PRESETS: dict[str, tuple[str, str | None]] = {
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "anthropic": ("https://api.anthropic.com/v1", "ANTHROPIC_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "gemini-openai": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    "ollama": ("http://localhost:11434/v1", None),
    "lmstudio": ("http://localhost:1234/v1", None),
    "copilot": ("http://127.0.0.1:8000/v1", None),
}

_CHATS: dict[str, int] = {}
_CHATS_LOCK = threading.Lock()


class OpenAIProvider(Provider):
    """Options: ``base_url``, ``api_key``/``api_key_env``, ``model(s)``, ``temperature``,
    ``max_tokens``, ``reasoning_effort``, ``headers``, ``extra`` (merged into the payload).

    ``conversation = true`` reuses the ``conversation_id`` returned by proxies that
    support it (e.g. Copilot bridges): the system prompt is sent once per chat, a new
    chat starts after ``max_chat_chars``; ``max_chats`` caps chats across all workers.
    """

    type = "openai"

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self.url = str(options.get("base_url", PRESETS["openai"][0])).rstrip("/") + "/chat/completions"
        self.api_key = api_key(options)
        self.conversation = bool(options.get("conversation", False))
        self.max_chat_chars = int(options.get("max_chat_chars", 45000))
        self.max_chats = int(options.get("max_chats", 0))
        self._conversation_id: str | None = None
        self._system: str | None = None
        self._chat_chars = 0

    def reset(self) -> None:
        self._conversation_id = None
        self._chat_chars = 0

    def _open_chat(self) -> None:
        with _CHATS_LOCK:
            used = _CHATS.get(self.url, 0)
            if self.max_chats and used >= self.max_chats:
                raise QuotaExhausted(f"chat limit reached ({self.max_chats}); run again later")
            _CHATS[self.url] = used + 1

    def _send(self, model: str, system: str, user: str) -> Completion:
        continuing = (self.conversation and self._conversation_id is not None and system == self._system
                      and self._chat_chars + len(user) <= self.max_chat_chars)
        if continuing:
            messages = [{"role": "user", "content": user}]
        else:
            self.reset()
            self._open_chat()
            messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        payload: dict[str, Any] = {"model": model, "messages": messages}
        for key in ("temperature", "max_tokens", "reasoning_effort"):
            if self.options.get(key) is not None:
                payload[key] = self.options[key]
        payload.update(self.options.get("extra") or {})
        if continuing:
            payload["conversation_id"] = self._conversation_id
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        headers.update(self.options.get("headers") or {})

        data = post_json(self.url, payload, headers, self.timeout)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise TransientError(f"malformed completion: {str(data)[:300]}") from exc
        if isinstance(content, list):
            content = "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part)
                              for part in content)
        if not isinstance(content, str) or not content.strip():
            raise TransientError("API returned an empty completion")
        if self.conversation:
            self._conversation_id = data.get("conversation_id") or None
            self._system = system
            self._chat_chars += sum(len(m["content"]) for m in messages) + len(content)
        usage = {key: value for key, value in (data.get("usage") or {}).items() if isinstance(value, int)}
        return Completion(content.strip(), model, usage)
