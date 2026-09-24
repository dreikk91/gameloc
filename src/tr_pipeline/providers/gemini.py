"""Native Google AI Studio (Gemini) API: JSON output mode and relaxed safety filters,
which game dialogue (violence, crime) often needs."""

from __future__ import annotations

from typing import Any

from .base import AuthError, Completion, Provider, ProviderError, TransientError, api_key, post_json

_SAFETY_CATEGORIES = ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                      "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")


class GeminiProvider(Provider):
    """Options: ``model(s)``, ``api_key``/``api_key_env`` (default ``GEMINI_API_KEY``),
    ``temperature``, ``safety_off`` (default true), ``generation`` (extra generationConfig).
    Free-tier friendly: set ``rpm`` and a ``models`` fallback chain."""

    type = "gemini"
    default_model = "gemini-flash-latest"

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self.base_url = str(options.get("base_url", "https://generativelanguage.googleapis.com/v1beta")).rstrip("/")
        self.api_key = api_key(options, "GEMINI_API_KEY", "GOOGLE_API_KEY")

    def _send(self, model: str, system: str, user: str) -> Completion:
        if not self.api_key:
            raise AuthError("Gemini API key missing: set api_key or the GEMINI_API_KEY variable")
        generation = {"temperature": self.options.get("temperature", 0.2),
                      "responseMimeType": "application/json", **(self.options.get("generation") or {})}
        payload: dict[str, Any] = {"contents": [{"role": "user", "parts": [{"text": user}]}],
                                   "generationConfig": generation}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if self.options.get("safety_off", True):
            payload["safetySettings"] = [{"category": c, "threshold": "BLOCK_NONE"} for c in _SAFETY_CATEGORIES]
        data = post_json(f"{self.base_url}/models/{model}:generateContent", payload,
                         {"x-goog-api-key": self.api_key}, self.timeout)
        candidates = data.get("candidates") or []
        if not candidates:
            raise ProviderError(f"Gemini returned no candidates: {data.get('promptFeedback')}")
        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
        if not text.strip():
            reason = candidate.get("finishReason", "UNKNOWN")
            if reason in ("SAFETY", "PROHIBITED_CONTENT", "RECITATION", "BLOCKLIST"):
                raise ProviderError(f"Gemini blocked the reply (finishReason={reason})")
            raise TransientError(f"Gemini returned an empty reply (finishReason={reason})")
        meta = data.get("usageMetadata") or {}
        usage = {"prompt_tokens": int(meta.get("promptTokenCount", 0)),
                 "completion_tokens": int(meta.get("candidatesTokenCount", 0)),
                 "total_tokens": int(meta.get("totalTokenCount", 0))}
        return Completion(text.strip(), model, usage)
