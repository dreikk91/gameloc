"""Batch translation engine: scene batching, dedupe, masking, partial acceptance,
validation with error feedback, overflow splitting, parallel workers, checkpoints."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from .config import Config
from .glossary import Glossary
from .prompts import translate_system
from .providers import AuthError, ContextOverflow, Provider, ProviderError, QuotaExhausted, create_provider
from .records import Project, Record
from .store import Store
from .text import TagMasker, Validator
from .util import dumps, extract_json

log = logging.getLogger("gameloc")

_HEADER_RESERVE = 2000  # characters kept free for the scene, character and glossary header


@dataclass
class Report:
    selected: int = 0
    imported: int = 0
    reused: int = 0
    translated: int = 0
    failed: int = 0
    batches: int = 0
    stopped: str | None = None


class Translator:
    """Translate every pending record of a project.

    ``provider_factory`` lets library users inject any :class:`Provider`; one
    instance is created per worker thread.
    """

    def __init__(self, cfg: Config, *, project: Project | None = None, store: Store | None = None,
                 provider_factory: Callable[[], Provider] | None = None, provider: str | None = None,
                 model: str | None = None) -> None:
        self.cfg = cfg
        self.project = project or Project(cfg)
        self.store = store or Store(cfg.work_dir)
        self.masker = TagMasker.from_config(cfg.tags)
        self.validator = Validator(cfg, self.masker)
        self.glossary = Glossary.load(cfg)
        options = cfg.provider_options(provider or cfg.translate.provider, model)
        self.provider_factory = provider_factory or (lambda: create_provider(options))
        self.system = translate_system(cfg)
        self._local = threading.local()
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # -- selection -------------------------------------------------------

    def pending(self, *, scenes: list[str] | None = None, ids: set[str] | None = None,
                retranslate: bool = False) -> list[Record]:
        result = []
        for record in self.project.records:
            if not record.translatable:
                continue
            if scenes and not any(record.scene == s or record.scene.startswith(s) for s in scenes):
                continue
            if ids and record.id not in ids:
                continue
            if not retranslate and self.store.translation(record) is not None:
                continue
            result.append(record)
        return result

    def batches(self, records: list[Record]) -> list[list[Record]]:
        """Split records into prompts, keeping scenes together and in file order."""
        opts = self.cfg.translate
        if opts.group_by_scene:
            order: dict[str, int] = {}
            records = sorted(records, key=lambda r: order.setdefault(r.scene, len(order)))
        budget = max(500, opts.max_chars - len(self.system) - _HEADER_RESERVE)
        result: list[list[Record]] = []
        current: list[Record] = []
        size = 0
        for record in records:
            cost = len(dumps(self._item(record, "0", False)[0])) + 1
            new_scene = bool(current) and record.scene != current[-1].scene
            if current and (len(current) >= opts.max_records or size + cost > budget
                            or (new_scene and not opts.merge_scenes)):
                result.append(current)
                current, size = [], 0
            current.append(record)
            size += cost
        if current:
            result.append(current)
        return result

    # -- prompt ----------------------------------------------------------

    def _item(self, record: Record, local_id: str, with_scene: bool) -> tuple[dict[str, Any], list[str]]:
        item: dict[str, Any] = {"id": local_id}
        if with_scene and record.scene:
            item["scene"] = record.scene
        if record.speaker:
            item["speaker"] = self.glossary.speaker_label(record.speaker)
        tag_lang = self.cfg.tag_lang if self.cfg.tag_lang in record.texts else next(iter(record.texts))
        tokens: list[str] = []
        for lang, text in record.texts.items():
            if lang == tag_lang:
                item[lang], tokens = self.masker.mask(text)
            else:
                item[lang] = self.masker.strip(text)
        if tokens:
            item["markers"] = [f"{{{index}}}" for index in range(len(tokens))]
        if record.context:
            item["context"] = record.context
        if record.max_length:
            item["max_len"] = record.max_length
        return item, tokens

    def build_prompt(self, records: list[Record], errors: dict[str, str] | None = None
                     ) -> tuple[str, dict[str, tuple[Record, list[str]]]]:
        scenes = list(dict.fromkeys(r.scene for r in records if r.scene))
        lines: list[str] = []
        if len(scenes) == 1:
            lines.append(f"SCENE: {scenes[0]}")
        elif scenes:
            lines.append(f"SCENES: {', '.join(scenes)} (keep dialogue flow within each scene only)")
        characters, terms = self.glossary.relevant(
            (text for r in records for text in r.texts.values()), (r.speaker for r in records))
        if characters:
            lines += ["CHARACTERS:", *(term.line() for term in characters)]
        if terms:
            lines += ["GLOSSARY:", *(term.line() for term in terms)]
        lines.append("ITEMS:")
        local: dict[str, tuple[Record, list[str]]] = {}
        for index, record in enumerate(records, 1):
            item, tokens = self._item(record, str(index), len(scenes) > 1)
            lines.append(dumps(item))
            local[str(index)] = (record, tokens)
        if errors:
            lines.append("YOUR PREVIOUS ANSWER WAS REJECTED BY VALIDATION. Fix these items:")
            lines += [f"- {lid}: {errors[rec.id]}" for lid, (rec, _) in local.items() if rec.id in errors]
        lines.append('Answer with a JSON array only: [{"id":"1","text":"..."}]')
        return "\n".join(lines), local

    def parse(self, reply: str) -> dict[str, str]:
        """``local id -> text``; raises ValueError when the reply holds no usable JSON."""
        data = extract_json(reply)
        if isinstance(data, dict):
            data = next((data[k] for k in ("items", "translations", "results", "records", "data")
                         if isinstance(data.get(k), list)), [data])
        if not isinstance(data, list):
            raise ValueError("expected a JSON array")
        result: dict[str, str] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            local_id = str(item.get("id", "")).strip()
            for key in ("text", "translation", self.cfg.target_lang, "t"):
                if isinstance(item.get(key), str):
                    result.setdefault(local_id, item[key])
                    break
        return result

    # -- execution -------------------------------------------------------

    def _provider(self) -> Provider:
        if not hasattr(self._local, "provider"):
            self._local.provider = self.provider_factory()
        provider: Provider = self._local.provider
        return provider

    def _count(self, report: Report, field: str, amount: int = 1) -> None:
        with self._lock:
            setattr(report, field, getattr(report, field) + amount)

    def translate_records(self, provider: Provider, records: list[Record]) -> tuple[int, int]:
        """Translate one batch; returns ``(translated, failed)`` record counts."""
        groups: dict[str, list[Record]] = {}
        for record in records:
            key = record.content_key if self.cfg.translate.dedupe else record.id
            groups.setdefault(key, []).append(record)
        members = {group[0].id: group for group in groups.values()}
        return self._translate_unique(provider, [group[0] for group in groups.values()], members)

    def _translate_unique(self, provider: Provider, remaining: list[Record],
                          members: dict[str, list[Record]]) -> tuple[int, int]:
        errors: dict[str, str] = {}
        translated = 0
        for attempt in range(1, self.cfg.translate.attempts + 1):
            if self._stop.is_set():
                return translated, 0
            prompt, local = self.build_prompt(remaining, errors if attempt > 1 else None)
            errors = {}
            try:
                completion = provider.complete(self.system, prompt)
            except ContextOverflow:
                if len(remaining) > 1:
                    middle = len(remaining) // 2
                    log.warning("prompt too large, splitting %d records", len(remaining))
                    first = self._translate_unique(provider, remaining[:middle], members)
                    second = self._translate_unique(provider, remaining[middle:], members)
                    return translated + first[0] + second[0], first[1] + second[1]
                errors = {remaining[0].id: "prompt too large for the model even for one line"}
                break
            except (AuthError, QuotaExhausted):
                raise
            except ProviderError as exc:
                errors = {record.id: str(exc) for record in remaining}
                provider.reset()
                continue
            self.store.add_usage(provider.type, completion.model, completion.usage, len(prompt) + len(self.system))
            if self.cfg.translate.log_responses:
                self.store.log_response({"ids": [r.id for r in remaining], "model": completion.model,
                                         "reply": completion.text})
            try:
                results = self.parse(completion.text)
            except ValueError as exc:
                results, errors = {}, {record.id: str(exc) for record in remaining}
            failed: list[Record] = []
            for local_id, (record, tokens) in local.items():
                raw = results.get(local_id)
                if raw is None:
                    errors[record.id] = errors.get(record.id) or "missing from the answer"
                    failed.append(record)
                    continue
                try:
                    text = self.masker.unmask(raw.strip(), tokens, self.cfg.tags.strict_order)
                except ValueError as exc:
                    errors[record.id] = str(exc)
                    failed.append(record)
                    continue
                problems = self.validator.problems(record, text)
                if problems:
                    errors[record.id] = "; ".join(problems)
                    failed.append(record)
                    continue
                for member in members[record.id]:
                    self.store.put(member, text, "translated", provider=provider.type, model=completion.model)
                    translated += 1
            remaining = failed
            if not remaining:
                return translated, 0
            provider.reset()
        failed_count = 0
        for record in remaining:
            for member in members[record.id]:
                self.store.error(member, errors.get(record.id, "not translated"))
                failed_count += 1
        return translated, failed_count

    def _run_batch(self, batch: list[Record], index: int, total: int, report: Report) -> None:
        if self._stop.is_set():
            return
        try:
            translated, failed = self.translate_records(self._provider(), batch)
        except (AuthError, QuotaExhausted) as exc:
            if not self._stop.is_set():
                log.error("stopping: %s", exc)
                report.stopped = str(exc)
            self._stop.set()
            return
        self._count(report, "translated", translated)
        self._count(report, "failed", failed)
        scenes = ", ".join(dict.fromkeys(r.scene for r in batch if r.scene))[:60]
        log.info("[%d/%d] %s%d/%d translated", index, total, f"{scenes}: " if scenes else "",
                 translated, len(batch))

    def run(self, *, scenes: list[str] | None = None, ids: set[str] | None = None,
            limit: int | None = None, retranslate: bool = False, dry_run: bool = False) -> Report:
        report = Report()
        todo: list[Record] = []
        for record in self.pending(scenes=scenes, ids=ids, retranslate=retranslate):
            if not retranslate and record.existing:
                if not dry_run:
                    self.store.put(record, record.existing, "imported")
                report.imported += 1
                continue
            reused = self.store.reusable(record) if self.cfg.translate.reuse and not retranslate else None
            if reused and not self.validator.problems(record, reused):
                if not dry_run:
                    self.store.put(record, reused, "reused")
                report.reused += 1
                continue
            todo.append(record)
        if limit:
            todo = todo[:limit]
        report.selected = len(todo)
        batches = self.batches(todo)
        report.batches = len(batches)
        if dry_run:
            if batches:
                prompt = self.build_prompt(batches[0])[0]
                print(f"=== SYSTEM ({len(self.system)} chars) ===\n{self.system}\n")
                print(f"=== USER, batch 1/{len(batches)} ({len(prompt)} chars) ===\n{prompt}")
            return report
        log.info("%d records to translate in %d batches", len(todo), len(batches))
        workers = max(1, self.cfg.translate.workers)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="translate") as pool:
            futures = [pool.submit(self._run_batch, batch, index, len(batches), report)
                       for index, batch in enumerate(batches, 1)]
            for future in futures:
                future.result()
        return report
