"""Provider base class: retries, backoff, rate limiting and model fallback live here,
so a new API only implements ``_send``."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("tr_pipeline")


class ProviderError(RuntimeError):
    """Request failed and retrying the same request will not help."""


class AuthError(ProviderError):
    """Missing or rejected credentials: the run stops."""


class QuotaExhausted(ProviderError):
    """Daily quota, credits or chat limit exhausted: the run stops (after model fallback)."""


class ContextOverflow(ProviderError):
    """Prompt too large for the model: the batch is split."""


class RateLimited(ProviderError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TransientError(ProviderError):
    """Network error, timeout or overloaded server: retried with backoff."""

    def __init__(self, message: str, overloaded: bool = False) -> None:
        super().__init__(message)
        self.overloaded = overloaded


@dataclass
class Completion:
    text: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)


_OVERFLOW = ("context length", "context window", "context limit", "maximum context", "too many tokens",
             "prompt is too long", "context_length_exceeded", "request too large", "payload too large",
             "text too long", "text-too-long")
_AUTH = ("invalid api key", "api_key_invalid", "api key not valid", "unauthorized", "incorrect api key",
         "clearance_required", "permission denied")
_QUOTA = ("per day", "perday", "daily", "insufficient_quota", "quota exceeded", "credits",
          "too-many-messages", "too_many_messages", "message limit reached", "session message limit")
_RATE = ("rate limit", "rate_limit", "too many requests", "resource_exhausted")
_OVERLOADED = ("overloaded", "unavailable", "high demand", "try again later")


def classify(status: int, body: str, retry_after: str | None = None) -> ProviderError:
    """Map an HTTP status / error text to the exception type that drives retry policy."""
    low = body.lower()
    message = f"HTTP {status}: {body[:500]}" if status else body[:500]
    if any(hint in low for hint in _OVERFLOW) or status == 413:
        return ContextOverflow(message)
    if status in (401, 403) or any(hint in low for hint in _AUTH):
        return AuthError(message)
    if status == 402 or any(hint in low for hint in _QUOTA):
        return QuotaExhausted(message)
    if status == 429 or any(hint in low for hint in _RATE):
        seconds: float | None = None
        if retry_after and re.fullmatch(r"\d+(\.\d+)?", retry_after.strip()):
            seconds = float(retry_after)
        elif match := re.search(r"retry (?:in|after)\D{0,3}([\d.]+)\s*s", low):
            seconds = float(match.group(1))
        return RateLimited(message, seconds)
    if status >= 500 or any(hint in low for hint in _OVERLOADED):
        return TransientError(message, overloaded=True)
    if status in (0, 408, 409):
        return TransientError(message)
    return ProviderError(message)


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json; charset=utf-8", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        raise classify(exc.code, text, exc.headers.get("Retry-After") if exc.headers else None) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransientError(f"network error: {exc}") from exc
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise TransientError(f"API returned invalid JSON: {body[:200]}") from exc
    if isinstance(data, dict) and data.get("error"):
        error = data["error"]
        raise classify(0, str(error.get("message", error) if isinstance(error, dict) else error))
    if not isinstance(data, dict):
        raise TransientError("API returned a non-object JSON value")
    return data


def api_key(options: dict[str, Any], *fallback_env: str) -> str:
    """``api_key`` option, else the variable named by ``api_key_env``, else fallbacks."""
    if options.get("api_key"):
        return str(options["api_key"])
    for name in (options.get("api_key_env"), *fallback_env):
        if name and os.environ.get(name):
            return os.environ[name]
    return ""


class RateLimiter:
    """Minimum interval + requests-per-minute window, shared by all workers of one endpoint."""

    def __init__(self, min_interval: float, rpm: int) -> None:
        self.min_interval = min_interval
        self.rpm = rpm
        self.last = 0.0
        self.stamps: deque[float] = deque()
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            while True:
                now = time.monotonic()
                while self.stamps and now - self.stamps[0] >= 60:
                    self.stamps.popleft()
                if self.rpm and len(self.stamps) >= self.rpm:
                    time.sleep(60 - (now - self.stamps[0]) + 0.05)
                    continue
                gap = self.min_interval - (now - self.last)
                if gap > 0:
                    time.sleep(gap)
                self.last = time.monotonic()
                self.stamps.append(self.last)
                return


_LIMITERS: dict[str, RateLimiter] = {}
_LIMITERS_LOCK = threading.Lock()


def shared_limiter(key: str, min_interval: float, rpm: int) -> RateLimiter:
    with _LIMITERS_LOCK:
        if key not in _LIMITERS:
            _LIMITERS[key] = RateLimiter(min_interval, rpm)
        return _LIMITERS[key]


class Provider:
    """Stateless ``system + user -> text`` completion with a robust retry policy.

    Options common to every provider: ``model`` or ``models`` (fallback chain),
    ``timeout``, ``retries``, ``retry_delay``, ``min_interval``, ``rpm``.
    """

    type = "base"
    default_model = ""

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options
        models = options.get("models") or [options.get("model") or self.default_model]
        self.models = [str(model) for model in models]
        self.model_index = 0
        self.timeout = float(options.get("timeout", 180))
        self.retries = max(1, int(options.get("retries", 4)))
        self.retry_delay = float(options.get("retry_delay", 3))
        key = f"{self.type}:{options.get('base_url', '')}:{options.get('command', '')}"
        self.limiter = shared_limiter(key, float(options.get("min_interval", 0)), int(options.get("rpm", 0)))

    @property
    def model(self) -> str:
        return self.models[self.model_index]

    def reset(self) -> None:
        """Forget conversation state (only stateful providers override this)."""

    def _send(self, model: str, system: str, user: str) -> Completion:
        raise NotImplementedError

    def _next_model(self, reason: Exception) -> bool:
        if self.model_index + 1 >= len(self.models):
            return False
        self.model_index += 1
        log.warning("%s: switching to fallback model %s (%s)", self.type, self.model, str(reason)[:120])
        return True

    def complete(self, system: str, user: str) -> Completion:
        last: Exception | None = None
        rate_limited = 0
        for attempt in range(1, self.retries + 1):
            self.limiter.wait()
            try:
                return self._send(self.model, system, user)
            except (AuthError, ContextOverflow):
                raise
            except QuotaExhausted as exc:
                if not self._next_model(exc):
                    raise
                last = exc
                continue
            except RateLimited as exc:
                last = exc
                rate_limited += 1
                if rate_limited >= 2 and self._next_model(exc):
                    rate_limited = 0
                    continue
                delay = exc.retry_after + 1 if exc.retry_after else self.retry_delay * 2 ** attempt
            except TransientError as exc:
                last = exc
                if exc.overloaded and self._next_model(exc):
                    continue
                delay = self.retry_delay * 2 ** (attempt - 1)
            if attempt < self.retries:
                delay = min(delay, 300)
                log.warning("%s: %s; retry %d/%d in %.0fs", self.type, str(last)[:200], attempt,
                            self.retries - 1, delay)
                time.sleep(delay)
        raise TransientError(f"{self.type}: failed after {self.retries} attempts: {last}")
