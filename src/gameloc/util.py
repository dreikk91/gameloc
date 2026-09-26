"""Small I/O and JSON helpers shared by every module."""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import re
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

_APPEND_LOCK = threading.Lock()


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def dumps(value: Any) -> str:
    """Compact, UTF-8 friendly JSON used in prompts and JSONL files."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def read_text(path: Path, encoding: str = "utf-8-sig") -> tuple[str, str]:
    """Read a file and return ``(text, encoding_to_write_back)``.

    UTF-8 files keep their BOM (or lack of it) when written back.
    """
    data = Path(path).read_bytes()
    if encoding.lower().replace("_", "-") in ("utf-8", "utf8", "utf-8-sig"):
        bom = data.startswith(codecs.BOM_UTF8)
        return data.decode("utf-8-sig"), "utf-8-sig" if bom else "utf-8"
    return data.decode(encoding), encoding


def read_json(path: Path) -> Any:
    return json.loads(read_text(path)[0])


def write_text_atomic(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write through a temporary file so a crash never leaves a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with tmp.open("w", encoding=encoding, newline="") as handle:
            handle.write(text)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def write_json(path: Path, value: Any, indent: int | None = 2) -> None:
    write_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=indent) + "\n")


def append_jsonl(path: Path, value: Any) -> None:
    """Append one line and fsync it: JSONL files are the crash-safe checkpoints."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _APPEND_LOCK, path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(dumps(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def repair_jsonl_tail(path: Path) -> None:
    """Terminate a line cut by a crash so the next append starts on a fresh line."""
    path = Path(path)
    if path.exists() and path.stat().st_size:
        with path.open("rb+") as handle:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                handle.write(b"\n")


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield JSON objects, silently skipping blank or corrupt (truncated) lines."""
    path = Path(path)
    if not path.exists():
        return
    with path.open(encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def get_path(obj: Any, spec: str) -> Any:
    """Read a dotted field path; ``a|b`` returns the first non-empty alternative.

    ``get_path(row, "context.speaker.id|speaker")``
    """
    for alternative in spec.split("|"):
        current = obj
        for part in alternative.strip().split("."):
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
                current = current[int(part)]
            else:
                current = None
                break
        if current not in (None, ""):
            return current
    return None


def set_path(obj: dict[str, Any], spec: str, value: Any) -> None:
    """Write a dotted field path (first alternative), creating tables as needed."""
    parts = spec.split("|")[0].strip().split(".")
    current = obj
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def extract_json(text: str) -> Any:
    """Find the first JSON value in a model reply (fences, chatter, truncation tolerated)."""
    cleaned = text.strip().lstrip("﻿")
    cleaned = re.sub(r"^```[\w-]*\s*", "", cleaned)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char not in "[{":
            continue
        try:
            return decoder.raw_decode(cleaned, index)[0]
        except json.JSONDecodeError:
            if char == "[" and (repaired := _repair_array(cleaned[index:])) is not None:
                return repaired
    raise ValueError("reply contains no valid JSON")


def _repair_array(text: str) -> list[Any] | None:
    """Keep the complete objects of an array cut off mid-way (token limit)."""
    ends: list[int] = []
    depth = 0
    in_string = escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if char == "}" and depth == 1:
                ends.append(index)
    for end in reversed(ends):
        try:
            value = json.loads(text[: end + 1] + "]")
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            return value
    return None


def pack_scenes(items: list[T], scene: Callable[[T], str], cost: Callable[[T], int], budget: int, *,
                max_items: int = 0, merge: bool = True, merge_max: int = 0) -> list[list[T]]:
    """Group consecutive items of one scene and pack whole scenes into batches.

    A scene is split only when it alone exceeds ``budget`` or ``max_items``; scenes longer than
    ``merge_max`` (0 = no limit) or all scenes when ``merge`` is off never share a batch.
    """
    scenes: list[list[T]] = []
    for item in items:
        if scenes and scene(scenes[-1][-1]) == scene(item):
            scenes[-1].append(item)
        else:
            scenes.append([item])
    limit = max_items or len(items) or 1
    result: list[list[T]] = []
    current: list[T] = []
    size = 0
    for group in scenes:
        costs = [cost(item) for item in group]
        total = sum(costs) + len(scene(group[0])) + 10  # + scene boundary
        mergeable = merge and (not merge_max or len(group) <= merge_max)
        if current and (not mergeable or size + total > budget or len(current) + len(group) > limit):
            result.append(current)
            current, size = [], 0
        if mergeable and total <= budget and len(group) <= limit:
            current += group
            size += total
            continue
        chunk: list[T] = []
        chunk_size = 0
        for item, item_cost in zip(group, costs, strict=True):
            if chunk and (len(chunk) >= limit or chunk_size + item_cost > budget):
                result.append(chunk)
                chunk, chunk_size = [], 0
            chunk.append(item)
            chunk_size += item_cost
        result.append(chunk)
    if current:
        result.append(current)
    return result
