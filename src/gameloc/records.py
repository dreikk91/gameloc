"""Loading game strings into uniform records and writing translations back."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .formats import Table, detect_format, load_table
from .text import TagMasker
from .util import dumps, get_path, set_path, sha256

if TYPE_CHECKING:
    from .config import Config, LangSource
    from .store import Store

log = logging.getLogger("gameloc")


@dataclass
class Record:
    id: str
    texts: dict[str, str]
    """Source texts by language code, primary language first."""
    file: str = ""
    scene: str = ""
    speaker: str = ""
    context: dict[str, str] = field(default_factory=dict)
    max_length: int | None = None
    existing: str | None = None
    translatable: bool = True
    row: dict[str, Any] | None = field(default=None, repr=False, compare=False)

    @property
    def source(self) -> str:
        return next(iter(self.texts.values()), "")

    @property
    def hash(self) -> str:
        """Changes whenever any source text changes: stale translations are redone."""
        return sha256(dumps(self.texts))

    @property
    def content_key(self) -> str:
        """Identical lines said by the same speaker share one translation."""
        return sha256(dumps([self.texts, self.speaker]))


class Project:
    """All records of a game plus the loaded tables needed to write them back."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.masker = TagMasker.from_config(cfg.tags)
        self.tables: list[tuple[str, Table]] = []
        self.records: list[Record] = []
        self._load()
        self.by_id = {record.id: record for record in self.records}

    def _relpath(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.cfg.root.resolve()).as_posix()
        except ValueError:
            return path.name

    def _side_texts(self, lang: LangSource, path: Path, rel: str) -> dict[str, str]:
        """``id -> text`` of a language stored in its own file."""
        assert lang.path is not None
        side = self.cfg.resolve(lang.path.format(relpath=rel, stem=path.stem, name=path.name))
        if not side.exists():
            return {}
        src = self.cfg.source
        table = load_table(side, lang.format, records=lang.records, encoding=src.encoding,
                           delimiter=src.delimiter, options=src.options)
        id_spec = lang.id or src.id
        result: dict[str, str] = {}
        for index, row in enumerate(table.rows):
            key = get_path(row, id_spec)
            if key is None:
                key = table.keys[index] if table.keys else index
            value = get_path(row, lang.field)
            if isinstance(value, str):
                result[str(key)] = value
        return result

    def _load(self) -> None:
        src = self.cfg.source
        files = [path for path in self.cfg.glob(src.path) if path.is_file()]
        if not files:
            raise FileNotFoundError(f"no source files match {src.path!r}")
        multi = len(files) > 1
        skip = re.compile(src.skip_pattern) if src.skip_pattern else None
        scene_pattern = re.compile(src.scene_pattern) if src.scene_pattern else None
        for path in files:
            rel = self._relpath(path)
            fmt = detect_format(path, src.format)
            table = load_table(path, fmt, records=src.records, encoding=src.encoding,
                               delimiter=src.delimiter, options=src.options)
            self.tables.append((rel, table))
            id_spec = "msgctxt|msgid" if fmt == "po" and src.id == "id" else src.id
            side = {code: self._side_texts(lang, path, rel)
                    for code, lang in src.langs.items() if lang.path}
            for index, row in enumerate(table.rows):
                raw_id = get_path(row, id_spec)
                if raw_id is None:
                    raw_id = table.keys[index] if table.keys else index
                local_id = str(raw_id)
                texts: dict[str, str] = {}
                for code, lang in src.langs.items():
                    value = side[code].get(local_id) if lang.path else get_path(row, lang.field)
                    if isinstance(value, str) and value.strip():
                        texts[code] = value
                record = Record(id=f"{rel}::{local_id}" if multi else local_id, texts=texts,
                                file=rel, row=row)
                record.scene = self._scene(row, rel, local_id, scene_pattern, multi)
                if src.speaker:
                    record.speaker = str(get_path(row, src.speaker) or "")
                for spec in src.context:
                    value = get_path(row, spec)
                    if value not in (None, ""):
                        record.context[spec.split("|")[0].split(".")[-1]] = str(value)
                if src.max_length:
                    value = get_path(row, src.max_length)
                    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
                        record.max_length = int(value)
                if src.target:
                    existing = get_path(row, src.target)
                    if isinstance(existing, str) and existing.strip() and existing != record.source:
                        record.existing = existing
                record.translatable = bool(texts) and self.masker.has_text(record.source) and not (
                    skip and skip.search(record.source))
                self.records.append(record)
        seen: set[str] = set()
        duplicates: list[str] = []
        for record in self.records:
            if record.id in seen:
                duplicates.append(record.id)
            seen.add(record.id)
        if duplicates:
            raise ValueError(f"duplicate record ids (check source.id): {duplicates[:5]}")

    def _scene(self, row: dict[str, Any], rel: str, local_id: str,
               pattern: re.Pattern[str] | None, multi: bool) -> str:
        spec = self.cfg.source.scene
        if spec == "@file":
            value = Path(rel).stem
        else:
            value = str(get_path(row, spec) or "") if spec else ""
        if pattern:
            match = pattern.search(value or local_id)
            if match:
                value = match.group(1) if match.groups() else match.group(0)
        return value or (Path(rel).stem if multi else "")

    def output_field(self) -> str:
        """Where translations are written: output.field, source.target or the primary text field."""
        src = self.cfg.source
        if self.cfg.output.field or src.target:
            return str(self.cfg.output.field or src.target)
        first = next(iter(src.langs.values()))
        if first.path:
            raise ValueError("set output.field: the primary language lives in a separate file")
        return first.field

    def export(self, store: Store) -> list[Path]:
        """Write every up-to-date translation into copies of the source files."""
        target_field = self.output_field()
        by_file: dict[str, list[Record]] = {}
        for record in self.records:
            by_file.setdefault(record.file, []).append(record)
        written: list[Path] = []
        for rel, table in self.tables:
            count = 0
            for record in by_file.get(rel, []):
                text = store.translation(record)
                if text is not None and record.row is not None:
                    set_path(record.row, target_field, text)
                    count += 1
            dest = self.cfg.resolve(self.cfg.output.path.format(
                workdir=self.cfg.work_dir.as_posix(), relpath=rel, stem=Path(rel).stem,
                name=Path(rel).name))
            table.save(dest)
            log.info("%s: %d translations -> %s", rel, count, dest)
            written.append(dest)
        return written
