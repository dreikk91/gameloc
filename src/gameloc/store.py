"""Append-only checkpoint of translations; the latest line per id wins."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .util import append_jsonl, iter_jsonl, read_json, repair_jsonl_tail, write_json

if TYPE_CHECKING:
    from .records import Record


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    """``translations.jsonl`` is the source of truth; everything else is derived.

    Every accepted translation is appended and fsynced immediately, so an
    interrupted run resumes exactly where it stopped.
    """

    def __init__(self, workdir: Path) -> None:
        self.dir = Path(workdir)
        self.path = self.dir / "translations.jsonl"
        self.errors_path = self.dir / "errors.jsonl"
        self.responses_path = self.dir / "responses.jsonl"
        self.stats_path = self.dir / "stats.json"
        self._lock = threading.Lock()
        self.entries: dict[str, dict[str, Any]] = {}
        self._by_content: dict[str, str] = {}
        repair_jsonl_tail(self.path)
        for entry in iter_jsonl(self.path):
            self._remember(entry)

    def _remember(self, entry: dict[str, Any]) -> None:
        self.entries[str(entry["id"])] = entry
        if entry.get("key") and entry.get("text"):
            self._by_content[entry["key"]] = entry["text"]

    def translation(self, record: Record) -> str | None:
        """Current translation, or ``None`` if missing or made for an older source text."""
        entry = self.entries.get(record.id)
        if entry and entry.get("text") and entry.get("hash") == record.hash:
            return str(entry["text"])
        return None

    def status(self, record: Record) -> str | None:
        entry = self.entries.get(record.id)
        return entry.get("status") if entry and entry.get("hash") == record.hash else None

    def reusable(self, record: Record) -> str | None:
        """Translation of an identical line (same texts and speaker) stored under another id."""
        return self._by_content.get(record.content_key)

    def put(self, record: Record, text: str, status: str, **meta: Any) -> None:
        entry = {"id": record.id, "hash": record.hash, "key": record.content_key, "text": text,
                 "status": status, "time": now(), **meta}
        with self._lock:
            append_jsonl(self.path, entry)
            self._remember(entry)

    def requeue(self, record: Record, reason: str) -> None:
        """Invalidate a translation so the next ``translate`` run redoes it."""
        with self._lock:
            entry = {"id": record.id, "hash": record.hash, "text": "", "status": "requeued",
                     "reason": reason, "time": now()}
            append_jsonl(self.path, entry)
            self.entries[record.id] = entry

    def error(self, record: Record, message: str, **meta: Any) -> None:
        append_jsonl(self.errors_path, {"id": record.id, "scene": record.scene, "source": record.source,
                                        "error": message, "time": now(), **meta})

    def log_response(self, data: dict[str, Any]) -> None:
        append_jsonl(self.responses_path, {"time": now(), **data})

    def add_usage(self, provider: str, model: str, usage: dict[str, int], chars: int) -> None:
        with self._lock:
            stats: dict[str, Any] = read_json(self.stats_path) if self.stats_path.exists() else {}
            bucket = stats.setdefault(f"{provider}:{model}", {})
            bucket["requests"] = bucket.get("requests", 0) + 1
            bucket["prompt_chars"] = bucket.get("prompt_chars", 0) + chars
            for key, value in usage.items():
                bucket[key] = bucket.get(key, 0) + value
            bucket["updated"] = now()
            write_json(self.stats_path, stats)
