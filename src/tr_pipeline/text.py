"""Technical tag masking and translation validation."""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config, TagConfig
    from .records import Record

DEFAULT_TAG_PATTERNS = [
    r"<[^<>\n]+>",  # HTML / Unity rich text / TextMeshPro / UE rich text (incl. </>)
    r"\{[^{}\n]*\}",  # {0}, {name}: C# string.Format, UE FText arguments
    r"%(?:\d+\$)?[-+#0]*\d*(?:\.\d+)?(?:ll|l|h)?[sdifuxXoeEgGcp@]",  # printf
    r"\\[nrt]",  # escaped line breaks stored literally
]
NEWLINE_PATTERN = r"\r\n|\r|\n"
_MARKER = re.compile(r"[{｛]\s*(\d+)\s*[}｝]")

CJK = r"[぀-ヿ㐀-䶿一-鿿가-힯ｦ-ﾟ]"
CJK_TARGETS = {"ja", "zh", "zh-hans", "zh-hant", "ko"}
CYRILLIC_TARGETS = {"uk", "ru", "be", "bg", "sr", "mk", "kk", "ky", "tg", "mn"}
EXTRA_FORBID = {
    "uk": [r"[ыэёъЫЭЁЪ]"],  # Russian-only letters
    "ru": [r"[іїєґІЇЄҐ]"],
    "be": [r"[іїєґІЇЄҐ]"],
}
_LATIN_WORD = re.compile(r"[A-Za-z]+")
_ROMAN = re.compile(r"[IVXLCDM]+")
_LETTER = re.compile(r"[^\W\d_]")


class TagMasker:
    """Replace tags with ``{0}``, ``{1}``... so the model cannot damage them."""

    def __init__(self, patterns: list[str]) -> None:
        self.regex = re.compile("|".join(f"(?:{p})" for p in patterns)) if patterns else None

    @classmethod
    def from_config(cls, tags: TagConfig) -> TagMasker:
        patterns = list(tags.patterns)
        if tags.defaults:
            patterns += DEFAULT_TAG_PATTERNS
        if tags.newlines:
            patterns.append(NEWLINE_PATTERN)
        return cls(patterns)

    def tags(self, text: str) -> list[str]:
        return [m.group(0) for m in self.regex.finditer(text)] if self.regex else []

    def mask(self, text: str) -> tuple[str, list[str]]:
        tokens: list[str] = []
        if not self.regex:
            return text, tokens

        def replace(match: re.Match[str]) -> str:
            tokens.append(match.group(0))
            return f"{{{len(tokens) - 1}}}"

        return self.regex.sub(replace, text), tokens

    def mask_like(self, text: str, tokens: list[str]) -> str:
        """Mask a translation with the marker numbers of its source tokens."""
        if not self.regex:
            return text
        used: set[int] = set()

        def replace(match: re.Match[str]) -> str:
            tag = match.group(0)
            for index, token in enumerate(tokens):
                if token == tag and index not in used:
                    used.add(index)
                    return f"{{{index}}}"
            return tag

        return self.regex.sub(replace, text)

    def strip(self, text: str) -> str:
        """Visible text only: tags removed, whitespace collapsed (for reference languages)."""
        return " ".join((self.regex.sub(" ", text) if self.regex else text).split())

    def visible_length(self, text: str) -> int:
        return len(self.regex.sub("", text) if self.regex else text)

    def has_text(self, text: str) -> bool:
        return bool(_LETTER.search(self.strip(text)))

    @staticmethod
    def unmask(text: str, tokens: list[str], strict_order: bool = False) -> str:
        """Restore tags; every marker must appear exactly once."""
        found = [int(m.group(1)) for m in _MARKER.finditer(text)]
        expected = list(range(len(tokens)))
        if sorted(found) != expected:
            missing = sorted(set(expected) - set(found))
            extra = sorted(set(found) - set(expected) | {i for i in found if found.count(i) > 1})
            raise ValueError(f"marker mismatch: missing {missing}, unexpected/duplicated {extra}")
        if strict_order and found != expected:
            raise ValueError(f"marker order changed: {found}")
        return _MARKER.sub(lambda m: tokens[int(m.group(1))], text)


class Validator:
    """Checks a finished translation against its record."""

    def __init__(self, cfg: Config, masker: TagMasker) -> None:
        self.masker = masker
        self.tag_lang = cfg.tag_lang
        self.strict_order = cfg.tags.strict_order
        self.check_length = cfg.validate.max_length
        target = cfg.target_lang.lower()
        forbid = cfg.validate.forbid
        if forbid is None:
            forbid = ([] if target in CJK_TARGETS else [CJK]) + EXTRA_FORBID.get(target, [])
        self.forbid = [re.compile(pattern) for pattern in forbid]
        latin = cfg.validate.latin
        self.latin = target in CYRILLIC_TARGETS if latin is None else latin
        self.allowed_latin = set(cfg.validate.allowed_latin)

    def contract(self, record: Record) -> str:
        """Source text whose tags the translation must reproduce."""
        return record.texts.get(self.tag_lang) or record.source

    def problems(self, record: Record, text: str) -> list[str]:
        if not isinstance(text, str) or not text.strip():
            return ["empty translation"]
        issues: list[str] = []
        visible = self.masker.strip(text)
        for regex in self.forbid:
            match = regex.search(visible)
            if match:
                issues.append(f"forbidden characters {match.group(0)!r}")
        if self.latin:
            allowed = set(self.allowed_latin)
            for source in record.texts.values():
                allowed.update(_LATIN_WORD.findall(self.masker.strip(source)))
            bad = [w for w in _LATIN_WORD.findall(visible)
                   if w not in allowed and not _ROMAN.fullmatch(w)]
            if bad:
                issues.append(f"untranslated Latin words: {', '.join(dict.fromkeys(bad))}")
        source_tags = self.masker.tags(self.contract(record))
        target_tags = self.masker.tags(text)
        if Counter(source_tags) != Counter(target_tags):
            missing = list((Counter(source_tags) - Counter(target_tags)).elements())
            extra = list((Counter(target_tags) - Counter(source_tags)).elements())
            issues.append(f"tags differ: missing {missing}, extra {extra}")
        elif self.strict_order and source_tags != target_tags:
            issues.append("tag order changed")
        if self.check_length and record.max_length:
            length = self.masker.visible_length(text)
            if length > record.max_length:
                issues.append(f"too long: {length} > {record.max_length}")
        return issues
