"""Technical tag masking and translation validation."""

from __future__ import annotations

import fnmatch
import importlib
import re
import sys
from collections import Counter
from typing import TYPE_CHECKING, Any

from .util import read_json

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
# Latin letters models slip into Cyrillic words ("Несiть"); fixed only inside words that contain Cyrillic
_HOMOGLYPHS = str.maketrans("aceiopxyABCEHIKMOPTX", "асеіорхуАВСЕНІКМОРТХ")
_CYRILLIC_WORD = re.compile(r"\w*[Ѐ-ӿ]\w*")
_LETTER = re.compile(r"[^\W\d_]")
_BREAKS = {"\n", "\r\n", "\r", "\\n"}  # line-break tags, incl. a literal backslash-n


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
        self.cyrillic = target in CYRILLIC_TARGETS
        self.length_encoding = cfg.validate.length_encoding
        charset = cfg.validate.charset
        self.outside_charset = re.compile(rf"[^\s{charset}]") if charset else None
        self.cfg = cfg
        self._widths: dict[str, dict[str, int]] = {}

    def normalize(self, text: str) -> str:
        """Replace Latin look-alike letters inside Cyrillic words (Cyrillic targets only)."""
        if not self.cyrillic:
            return text
        return _CYRILLIC_WORD.sub(lambda m: m.group(0).translate(_HOMOGLYPHS), text)

    def fit_options(self, record: Record) -> dict[str, Any]:
        """``[fit]`` settings of the record's scene: the first matching rule overrides the defaults."""
        fit = self.cfg.fit
        options: dict[str, Any] = {"widths": fit.widths, "default_width": fit.default_width,
                                   "max_width": fit.max_width, "max_lines": fit.max_lines, "wrap": fit.wrap}
        for rule in fit.rules:
            if fnmatch.fnmatchcase(record.scene, rule["scene"]):
                options.update({k: v for k, v in rule.items() if k != "scene"})
                break
        return options

    def width(self, line: str, options: dict[str, Any]) -> int:
        path = options["widths"]
        if path and path not in self._widths:
            self._widths[path] = {str(k): int(v) for k, v in read_json(self.cfg.resolve(path)).items()}
        table = self._widths.get(path, {}) if path else {}
        default = options["default_width"]
        return sum(table.get(char, default) for char in line)

    def lines(self, text: str, options: dict[str, Any]) -> list[str]:
        """Screen lines of a text: tags removed, line-break tags kept, word-wrapped when the game wraps."""
        if self.masker.regex:
            text = self.masker.regex.sub(lambda m: "\n" if m.group(0) in _BREAKS else "", text)
        hard = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if not options["wrap"] or not options["max_width"]:
            return hard
        result: list[str] = []
        for line in hard:
            current = ""
            for word in line.split(" "):
                candidate = f"{current} {word}" if current else word
                if current and self.width(candidate, options) > options["max_width"]:
                    result.append(current)
                    current = word
                else:
                    current = candidate
            result.append(current)
        return result

    def fit_problems(self, record: Record, text: str) -> list[str]:
        options = self.fit_options(record)
        max_width, max_lines = options["max_width"], options["max_lines"]
        if not max_width and not max_lines:
            return []
        lines = self.lines(text, options)
        issues: list[str] = []
        if max_lines and len(lines) > max_lines:
            issues.append(f"too long: {len(lines)} lines, the box shows {max_lines}; make it shorter")
        if max_width:
            for number, line in enumerate(lines, 1):
                if (width := self.width(line, options)) > max_width:
                    issues.append(f"line {number} is {width} wide, max {max_width}: shorten it"
                                  + ("" if options["wrap"] else " or re-break the lines"))
        return issues

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
        if self.outside_charset:
            bad = self.outside_charset.findall(visible)
            if bad:
                issues.append(f"characters missing from the game font: {''.join(dict.fromkeys(bad))}")
        if self.check_length and record.max_length:
            if self.length_encoding:  # a storage limit: the whole stored text, tags included
                try:
                    length = len(text.encode(self.length_encoding))
                except UnicodeEncodeError as exc:
                    length = 0
                    issues.append(f"cannot be encoded in {self.length_encoding}: {exc.object[exc.start:exc.end]!r}")
                unit = " bytes"
            else:
                length, unit = self.masker.visible_length(text), ""
            if length > record.max_length:
                issues.append(f"too long: {length} > {record.max_length}{unit}")
        issues += self.fit_problems(record, text)
        return issues


def make_validator(cfg: Config, masker: TagMasker) -> Validator:
    """The project's validator: ``[validate] plugin`` (a Validator subclass) or the built-in one."""
    target = cfg.validate.plugin
    if not target:
        return Validator(cfg, masker)
    module, _, attr = target.partition(":")
    root = str(cfg.root)
    if root not in sys.path:  # project-local modules such as tools/checks.py
        sys.path.insert(0, root)
    cls = getattr(importlib.import_module(module), attr)
    if not (isinstance(cls, type) and issubclass(cls, Validator)):
        raise ValueError(f"validate.plugin {target!r} is not a gameloc.text.Validator subclass")
    validator: Validator = cls(cfg, masker)
    return validator
