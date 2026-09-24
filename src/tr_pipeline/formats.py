"""File formats: every loader exposes mutable ``rows`` and writes them back with ``save``.

Custom engines plug in with ``format = "package.module:factory"`` where
``factory(path: Path, options: dict) -> Table``.
"""

from __future__ import annotations

import csv
import importlib
import io
import json
import re
from pathlib import Path
from typing import Any, Protocol

from .util import dumps, get_path, read_text, write_text_atomic


class Table(Protocol):
    rows: list[dict[str, Any]]
    keys: list[str] | None
    """Record ids taken from JSON object keys (parallel to ``rows``), if any."""

    def save(self, dest: Path) -> None: ...


class JsonTable:
    """A JSON document holding a list of objects, an ``id -> object`` map or an ``id -> text`` map.

    Flat ``id -> text`` maps are exposed as rows ``{"id": key, "text": value}``.
    """

    def __init__(self, path: Path, records: str = "", encoding: str = "utf-8-sig") -> None:
        text, self.encoding = read_text(path, encoding)
        self.doc: Any = json.loads(text)
        self.indent = 2 if "\n" in text.strip() else None
        node = get_path(self.doc, records) if records else self.doc
        self.keys: list[str] | None = None
        self._flat: dict[str, Any] | None = None
        if isinstance(node, list):
            self.rows = [row for row in node if isinstance(row, dict)]
        elif isinstance(node, dict) and all(isinstance(v, dict) for v in node.values()):
            self.keys = list(node)
            self.rows = list(node.values())
        elif isinstance(node, dict) and all(isinstance(v, str) for v in node.values()):
            self._flat = node
            self.rows = [{"id": key, "text": value} for key, value in node.items()]
        else:
            raise ValueError(f"{path}: records not found; set source.records to the list/map path")

    def save(self, dest: Path) -> None:
        if self._flat is not None:
            for row in self.rows:
                self._flat[row["id"]] = row["text"]
        write_text_atomic(dest, json.dumps(self.doc, ensure_ascii=False, indent=self.indent) + "\n",
                          self.encoding)


class JsonlTable:
    def __init__(self, path: Path, encoding: str = "utf-8-sig") -> None:
        text, self.encoding = read_text(path, encoding)
        self.keys: list[str] | None = None
        self.rows = [json.loads(line) for line in text.splitlines() if line.strip()]

    def save(self, dest: Path) -> None:
        write_text_atomic(dest, "".join(dumps(row) + "\n" for row in self.rows), self.encoding)


class CsvTable:
    """CSV/TSV with a header row. New columns (e.g. the translation) are appended on save."""

    def __init__(self, path: Path, delimiter: str | None = None, encoding: str = "utf-8-sig") -> None:
        text, self.encoding = read_text(path, encoding)
        self.delimiter = delimiter or ("\t" if path.suffix.lower() == ".tsv" else ",")
        self.newline = "\r\n" if "\r\n" in text else "\n"
        reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=self.delimiter)
        self.fieldnames = list(reader.fieldnames or [])
        self.keys: list[str] | None = None
        self.rows: list[dict[str, Any]] = [dict(row) for row in reader]

    def save(self, dest: Path) -> None:
        names = list(self.fieldnames)
        for row in self.rows:
            names.extend(key for key in row if key is not None and key not in names)
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, names, delimiter=self.delimiter,
                                lineterminator=self.newline, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(self.rows)
        write_text_atomic(dest, buffer.getvalue(), self.encoding)


_PO_KEYWORD = re.compile(r'^(msgctxt|msgid_plural|msgid|msgstr(?:\[\d+\])?)\s+"(.*)"\s*$')
_PO_CONTINUATION = re.compile(r'^"(.*)"\s*$')
_PO_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


def _po_unescape(value: str) -> str:
    return re.sub(r"\\(.)", lambda m: _PO_ESCAPES.get(m.group(1), m.group(1)), value)


def _po_escape(value: str) -> str:
    return (value.replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r"))


class PoTable:
    """Gettext PO (Unreal Engine localization export, many Unity plugins).

    Rows expose ``msgctxt``, ``msgid``, ``msgstr`` plus ``extracted`` (``#.``) and
    ``reference`` (``#:``) comments for context. The header entry is kept but not a row.
    """

    def __init__(self, path: Path, encoding: str = "utf-8-sig") -> None:
        text, self.encoding = read_text(path, encoding)
        self.entries: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        last = ""

        def flush() -> None:
            nonlocal current
            if current is not None:
                self.entries.append(current)
            current = None

        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped:
                flush()
                continue
            if stripped.startswith("#"):
                if current is not None and current["_order"]:
                    flush()
                if current is None:
                    current = {"_comments": [], "_order": []}
                current["_comments"].append(line)
                for prefix, key in (("#.", "extracted"), ("#:", "reference")):
                    if stripped.startswith(prefix):
                        body = stripped[2:].strip()
                        current[key] = f"{current[key]}\n{body}" if key in current else body
                continue
            match = _PO_KEYWORD.match(stripped)
            if match:
                keyword = match.group(1)
                if current is not None and keyword in current["_order"]:
                    flush()
                if current is None:
                    current = {"_comments": [], "_order": []}
                current[keyword] = _po_unescape(match.group(2))
                current["_order"].append(keyword)
                last = keyword
                continue
            match = _PO_CONTINUATION.match(stripped)
            if match and current is not None and last:
                current[last] += _po_unescape(match.group(1))
                continue
            raise ValueError(f"{path}:{number}: unparsable PO line")
        flush()
        self.keys: list[str] | None = None
        self.rows = [entry for entry in self.entries if entry.get("msgid")]

    def save(self, dest: Path) -> None:
        blocks = []
        for entry in self.entries:
            if "msgstr" in entry and "msgstr" not in entry["_order"]:
                entry["_order"].append("msgstr")
            lines = list(entry["_comments"])
            for keyword in entry["_order"]:
                value = str(entry[keyword])
                if "\n" in value.rstrip("\n"):
                    lines.append(f'{keyword} ""')
                    lines.extend(f'"{_po_escape(part)}"' for part in value.splitlines(keepends=True))
                else:
                    lines.append(f'{keyword} "{_po_escape(value)}"')
            blocks.append("\n".join(lines))
        write_text_atomic(dest, "\n\n".join(blocks) + "\n", self.encoding)


_EXTENSIONS = {".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl", ".csv": "csv",
               ".tsv": "tsv", ".po": "po", ".pot": "po"}


def detect_format(path: Path, fmt: str = "auto") -> str:
    if fmt != "auto":
        return fmt
    try:
        return _EXTENSIONS[path.suffix.lower()]
    except KeyError:
        raise ValueError(f"{path}: cannot detect format; set source.format") from None


def load_table(path: Path, fmt: str = "auto", *, records: str = "", encoding: str = "utf-8-sig",
               delimiter: str | None = None, options: dict[str, Any] | None = None) -> Table:
    fmt = detect_format(path, fmt)
    if ":" in fmt:
        module_name, _, attr = fmt.partition(":")
        factory = getattr(importlib.import_module(module_name), attr)
        table: Table = factory(path, options or {})
        return table
    if fmt == "json":
        return JsonTable(path, records, encoding)
    if fmt == "jsonl":
        return JsonlTable(path, encoding)
    if fmt in ("csv", "tsv"):
        return CsvTable(path, delimiter or ("\t" if fmt == "tsv" else None), encoding)
    if fmt == "po":
        return PoTable(path, encoding)
    raise ValueError(f"unknown format {fmt!r}")
