"""Glossary and character profiles loaded from loosely structured JSON files.

Field names differ from project to project, so each value is looked up in a
list of common candidates (``source``/``term``/``en``..., ``target``/``translation``/``uk``...).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .util import read_json

if TYPE_CHECKING:
    from .config import Config

_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")
_GENDERS = {
    "male": "male", "m": "male", "man": "male", "чоловіча": "male", "чоловічий": "male", "ч": "male",
    "female": "female", "f": "female", "woman": "female", "жіноча": "female", "жіночий": "female",
    "ж": "female", "female_program_setting": "female",
    "neutral": "neutral", "nonbinary": "neutral", "gender_neutral": "neutral",
    "nonbinary_she_her": "neutral",
}
_CONTAINERS = ("terms", "characters", "entries", "glossary", "items")


def normalize_gender(value: str) -> str:
    value = value.strip().lower()
    return _GENDERS.get(value, value)


@dataclass
class Term:
    source: str
    target: str
    aliases: list[str] = field(default_factory=list)
    gender: str = ""
    speech: str = ""
    """Grammatical gender the character uses about themselves when it differs from ``gender``."""
    note: str = ""
    key: str = ""
    character: bool = False
    forbidden: list[str] = field(default_factory=list)
    """Wrong target-language variants reported by ``audit``."""
    _patterns: list[re.Pattern[str]] | None = field(default=None, repr=False, compare=False)

    def forms(self) -> list[str]:
        return [form for form in dict.fromkeys([self.source, *self.aliases]) if len(form) >= 2]

    def matches(self, raw: str, folded: str) -> bool:
        if self._patterns is None:
            self._patterns = [re.compile(rf"(?<!\w){re.escape(form.casefold())}(?!\w)")
                              for form in self.forms() if not _CJK.search(form)]
        if any(form in raw for form in self.forms() if _CJK.search(form)):
            return True
        return any(pattern.search(folded) for pattern in self._patterns)

    @property
    def gender_label(self) -> str:
        """``male``, or ``male; speaks as female`` for a character who talks about themselves differently."""
        if self.speech and self.speech != self.gender:
            return f"{self.gender or '?'}; speaks as {self.speech}"
        return self.gender

    def line(self) -> str:
        gender = f" [{self.gender_label}]" if self.gender_label else ""
        if self.character:
            return f"- {self.source} → {self.target}{gender}" + (f": {self.note}" if self.note else "")
        return f"- {self.source} → {self.target}{gender}" + (f" ({self.note})" if self.note else "")


def _first(entry: dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _entries(data: Any) -> Iterator[dict[str, Any]]:
    if isinstance(data, list):
        yield from (item for item in data if isinstance(item, dict))
        return
    if not isinstance(data, dict):
        return
    for key in _CONTAINERS:
        if isinstance(data.get(key), list):
            yield from (item for item in data[key] if isinstance(item, dict))
            return
    for name, value in data.items():  # {"Shion": "Шіон"} or {"Shion": {...}}
        if isinstance(value, str):
            yield {"source": name, "target": value}
        elif isinstance(value, dict):
            yield {"source": name, **value}


class Glossary:
    def __init__(self, terms: list[Term], max_terms: int = 30, max_characters: int = 12) -> None:
        self.terms = terms
        self.max_terms = max_terms
        self.max_characters = max_characters
        self._by_name: dict[str, Term] = {}
        for term in terms:
            if term.character:
                for name in (term.key, term.target, *term.forms()):
                    self._by_name.setdefault(name.casefold(), term)

    @classmethod
    def load(cls, cfg: Config) -> Glossary:
        target = cfg.target_lang
        langs = cfg.langs
        source_keys = ["source", "term", "original",
                       *(key for lang in langs for key in (lang, f"name_{lang}")),
                       "en", "name_en", "name", "key"]
        target_keys = [target, f"name_{target}", f"preferred_{target}",
                       "target", "translation", "translated"]
        alias_keys = [key for lang in langs for key in (lang, f"name_{lang}")] + ["ja", "jp", "en"]
        note_keys = ["description", f"description_{target}", "note", "role", "voice", "category"]

        terms: list[Term] = []
        for patterns, character in ((cfg.glossary.characters, True), (cfg.glossary.terms, False)):
            for pattern in patterns:
                for path in cfg.glob(pattern):
                    if not path.exists():
                        raise FileNotFoundError(f"glossary file not found: {path}")
                    for entry in _entries(read_json(path)):
                        source = _first(entry, source_keys)
                        translation = _first(entry, [k for k in target_keys if k not in source_keys])
                        if not source or not translation:
                            continue
                        aliases = [v for k in alias_keys
                                   if isinstance(v := entry.get(k), str) and v.strip() and v != source]
                        for key in ("aliases", "alias"):
                            aliases += [str(a) for a in entry.get(key) or [] if a]
                        if " / " in source:
                            aliases += [part.strip() for part in source.split(" / ")]
                        notes = [n for n in (_first(entry, [k]) for k in note_keys) if n][:2]
                        gender = normalize_gender(_first(entry, ["gender", "sex"]))
                        speech = normalize_gender(_first(entry, ["speech_gender", "speaks_as"]))
                        forbidden = [str(v) for v in entry.get("forbidden") or []]
                        forbidden += [str(rule["variant"]) for rule in entry.get("consistency_rules") or []
                                      if isinstance(rule, dict) and rule.get("variant")]
                        terms.append(Term(
                            source=source, target=translation, aliases=list(dict.fromkeys(aliases)),
                            gender=gender, speech=speech, note="; ".join(notes)[:200],
                            key=str(entry.get("key") or entry.get("id") or source),
                            character=character or bool(gender or speech), forbidden=forbidden))
        return cls(terms, cfg.glossary.max_terms, cfg.glossary.max_characters)

    def find_character(self, name: str) -> Term | None:
        return self._by_name.get(name.strip().casefold()) if name else None

    def speaker_label(self, name: str) -> str:
        term = self.find_character(name)
        if term is None:
            return name
        return f"{term.target} [{term.gender_label}]" if term.gender_label else term.target

    def relevant(self, texts: Iterable[str], speakers: Iterable[str] = ()) -> tuple[list[Term], list[Term]]:
        """Characters and terms that appear in the given texts (speakers first)."""
        raw = "\n".join(texts)
        folded = raw.casefold()
        characters: list[Term] = []
        for name in speakers:
            term = self.find_character(name)
            if term and term not in characters:
                characters.append(term)
        terms: list[Term] = []
        # ponytail: linear scan of every term per batch; build one alternation regex if glossaries reach ~50k entries
        for term in self.terms:
            if term in characters or not term.matches(raw, folded):
                continue
            (characters if term.character else terms).append(term)
        return characters[: self.max_characters], terms[: self.max_terms]
