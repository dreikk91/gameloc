"""Project configuration: one TOML (or JSON) file describes a game."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, TypeVar

LANGUAGE_NAMES = {
    "ar": "Arabic", "be": "Belarusian", "bg": "Bulgarian", "cs": "Czech", "de": "German",
    "en": "English", "es": "Spanish", "fr": "French", "hu": "Hungarian", "it": "Italian",
    "ja": "Japanese", "jp": "Japanese", "kk": "Kazakh", "ko": "Korean", "nl": "Dutch",
    "pl": "Polish", "pt": "Portuguese", "pt-br": "Brazilian Portuguese", "ro": "Romanian",
    "ru": "Russian", "sr": "Serbian", "sv": "Swedish", "th": "Thai", "tr": "Turkish",
    "uk": "Ukrainian", "vi": "Vietnamese", "zh": "Chinese", "zh-hans": "Simplified Chinese",
    "zh-hant": "Traditional Chinese",
}


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code.lower(), code)


class ConfigError(ValueError):
    """Invalid or incomplete configuration."""


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


@dataclass
class LangSource:
    """Where the text of one source language lives."""

    field: str
    path: str | None = None
    """Separate file joined by id (placeholders: ``{relpath}``, ``{stem}``, ``{name}``)."""
    format: str = "auto"
    records: str = ""
    id: str | None = None


@dataclass
class SourceConfig:
    path: str = ""
    """File or glob relative to the config file, e.g. ``text/scene/*.json``."""
    format: str = "auto"
    """``json``, ``jsonl``, ``csv``, ``tsv``, ``po`` or ``package.module:factory``."""
    records: str = ""
    """Dotted path to the record list/map inside a JSON document."""
    id: str = "id"
    langs: dict[str, LangSource] = field(default_factory=dict)
    """Ordered ``language code -> field``; the first language is the primary source."""
    tag_lang: str | None = None
    """Language whose tags are the technical contract (default: first language)."""
    scene: str | None = None
    """Field grouping lines into scenes; ``@file`` uses the file name."""
    scene_pattern: str | None = None
    speaker: str | None = None
    context: list[str] = field(default_factory=list)
    max_length: str | None = None
    target: str | None = None
    """Field holding an existing translation (imported instead of re-translated)."""
    skip_pattern: str | None = None
    encoding: str = "utf-8-sig"
    delimiter: str | None = None
    options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.context = _as_list(self.context)


@dataclass
class OutputConfig:
    path: str = "{workdir}/output/{relpath}"
    field: str | None = None


@dataclass
class GlossaryConfig:
    terms: list[str] = field(default_factory=list)
    characters: list[str] = field(default_factory=list)
    max_terms: int = 30
    max_characters: int = 12

    def __post_init__(self) -> None:
        self.terms = _as_list(self.terms)
        self.characters = _as_list(self.characters)


@dataclass
class TagConfig:
    patterns: list[str] = field(default_factory=list)
    defaults: bool = True
    newlines: bool = True
    strict_order: bool = False

    def __post_init__(self) -> None:
        self.patterns = _as_list(self.patterns)


@dataclass
class ValidateConfig:
    forbid: list[str] | None = None
    """Regexes that must not match the visible translation (default: per target language)."""
    latin: bool | None = None
    """Reject Latin words absent from the sources (default: on for Cyrillic targets)."""
    allowed_latin: list[str] = field(default_factory=list)
    max_length: bool = True


@dataclass
class TranslateConfig:
    provider: str | None = None
    max_chars: int = 6000
    max_records: int = 40
    attempts: int = 3
    workers: int = 1
    group_by_scene: bool = True
    merge_scenes: bool = True
    merge_max_lines: int = 0
    """Only scenes with at most this many lines share a batch with other scenes (0 = any size)."""
    dedupe: bool = True
    reuse: bool = True
    log_responses: bool = True


@dataclass
class ProofreadConfig:
    provider: str | None = None
    max_chars: int = 12000
    attempts: int = 2
    workers: int = 1


@dataclass
class PromptConfig:
    rules: str = ""
    rules_file: str | None = None
    translate_file: str | None = None
    proofread_file: str | None = None
    note_language: str | None = None


@dataclass
class Config:
    root: Path
    source: SourceConfig
    game: str = ""
    target_lang: str = "uk"
    workdir: str = "gameloc_work"
    provider: str = "openai"
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    output: OutputConfig = field(default_factory=OutputConfig)
    glossary: GlossaryConfig = field(default_factory=GlossaryConfig)
    tags: TagConfig = field(default_factory=TagConfig)
    validate: ValidateConfig = field(default_factory=ValidateConfig)
    translate: TranslateConfig = field(default_factory=TranslateConfig)
    proofread: ProofreadConfig = field(default_factory=ProofreadConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)

    @property
    def target_name(self) -> str:
        return language_name(self.target_lang)

    @property
    def work_dir(self) -> Path:
        return self.resolve(self.workdir)

    @property
    def langs(self) -> list[str]:
        return list(self.source.langs)

    @property
    def tag_lang(self) -> str:
        return self.source.tag_lang or self.langs[0]

    def resolve(self, path: str | Path) -> Path:
        path = Path(path)
        return path if path.is_absolute() else self.root / path

    def glob(self, pattern: str) -> list[Path]:
        """Resolve a file name or glob pattern relative to the config directory."""
        direct = self.resolve(pattern)
        if direct.exists() or not any(char in pattern for char in "*?["):
            return [direct]
        path = Path(pattern)
        if path.is_absolute():
            anchor = Path(path.anchor)
            return sorted(anchor.glob(str(path.relative_to(anchor))))
        return sorted(self.root.glob(pattern))

    def provider_options(self, name: str | None = None, model: str | None = None) -> dict[str, Any]:
        """Options of a named ``[providers.NAME]`` profile, or of a bare provider type."""
        name = name or self.provider
        options = dict(self.providers.get(name, {}))
        options.setdefault("type", name)
        if model:
            options["model"] = model
            options["models"] = [model]
        return options


T = TypeVar("T")


def _section(cls: type[T], data: Any, where: str) -> T:
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ConfigError(f"[{where}] must be a table")
    known = {item.name for item in fields(cls)}  # type: ignore[arg-type]
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"[{where}] unknown keys: {', '.join(unknown)}")
    return cls(**data)


def config_from_dict(data: dict[str, Any], root: Path) -> Config:
    data = dict(data)
    source = dict(data.pop("source", None) or {})
    langs_raw = source.pop("langs", None) or {}
    if not langs_raw:
        raise ConfigError("[source.langs] must map at least one language code to a field")
    langs = {
        code: _section(LangSource, {"field": value} if isinstance(value, str) else value,
                       f"source.langs.{code}")
        for code, value in langs_raw.items()
    }
    src = _section(SourceConfig, source, "source")
    src.langs = langs
    if not src.path:
        raise ConfigError("source.path is required")
    if src.tag_lang and src.tag_lang not in langs:
        raise ConfigError(f"source.tag_lang {src.tag_lang!r} is not in source.langs")

    sections: dict[str, Any] = {}
    for name, cls in (("output", OutputConfig), ("glossary", GlossaryConfig), ("tags", TagConfig),
                      ("validate", ValidateConfig), ("translate", TranslateConfig),
                      ("proofread", ProofreadConfig), ("prompt", PromptConfig)):
        sections[name] = _section(cls, data.pop(name, None), name)
    providers = data.pop("providers", None) or {}
    top = {"game", "target_lang", "workdir", "provider"}
    unknown = sorted(set(data) - top)
    if unknown:
        raise ConfigError(f"unknown top-level keys: {', '.join(unknown)}")
    return Config(root=root, source=src, providers=providers, **sections, **data)


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    text = path.read_text(encoding="utf-8-sig")
    data = json.loads(text) if path.suffix.lower() == ".json" else tomllib.loads(text)
    return config_from_dict(data, path.parent)
