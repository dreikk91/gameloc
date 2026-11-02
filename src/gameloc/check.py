"""One-pass check of stored translations: the model reports only lines with a concrete problem and
returns the corrected line. Valid fixes are stored at once with status ``checked``; ``term`` proposals
are only logged. ``check.jsonl`` remembers each line by its wording, so a rerun skips what was checked
and comes back to a line only when its text changes."""

from __future__ import annotations

import csv
import io
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .config import Config
from .prompts import check_system
from .providers import AuthError, Provider, ProviderError, QuotaExhausted
from .records import Record
from .store import HAND_EDITED
from .translate import Translator
from .util import append_jsonl, chain_by_scene, dumps, extract_json, iter_jsonl, pack_scenes, sha256, write_text_atomic

log = logging.getLogger("gameloc")

REVIEW_ONLY = {"term"}
"""Unattended glossary fixes broke case and nicknames (17 of 65 on a read sample): a person applies them."""
_HEADER_RESERVE = 2500
_MAX_RECORDS = 40


def _key(text: str) -> str:
    return sha256(text)[:16]


class Checker:
    def __init__(self, cfg: Config, *, provider_factory: Callable[[], Provider] | None = None,
                 provider: str | None = None, model: str | None = None) -> None:
        self.cfg = cfg
        self.translator = Translator(cfg, provider_factory=provider_factory, model=model,
                                     provider=provider or cfg.proofread.provider)
        self.store = self.translator.store
        self.system = check_system(cfg)
        self.log_path = cfg.work_dir / "check.jsonl"
        self.checked = {(row["id"], key) for row in iter_jsonl(self.log_path) for key in row.get("checked", [])}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def pending(self, scenes: list[str] | None = None) -> list[Record]:
        """Translated lines not checked in their current wording; hand edits are left alone."""
        result = []
        for record in self.translator.project.records:
            if not record.translatable or (scenes and not any(record.scene.startswith(s) for s in scenes)):
                continue
            text = self.store.translation(record)
            if text is None or self.store.status(record) in HAND_EDITED or (record.id, _key(text)) in self.checked:
                continue
            result.append(record)
        return result

    def batches(self, records: list[Record]) -> list[list[Record]]:
        opts = self.cfg.proofread
        budget = opts.batch_chars or max(1000, opts.max_chars - len(self.system) - _HEADER_RESERVE)

        def cost(record: Record) -> int:
            return len(dumps(self.translator._item(record, "0", False)[0])) + len(self.store.translation(record) or "")

        return pack_scenes(records, lambda r: r.scene, cost, budget, max_items=opts.max_records or _MAX_RECORDS,
                           merge=False)

    def prompt(self, records: list[Record], full: bool) -> tuple[str, dict[str, tuple[Record, list[str]]]]:
        """``full`` opens a scene: a conversation continues without repeating its header."""
        translator = self.translator
        lines = [f"SCENE: {records[0].scene}" if full else "Next lines of the same scene:"]
        if full:
            rows = translator._scene_rows([records[0].scene])
            characters, terms = translator.glossary.relevant(
                (text for r in records for text in r.texts.values()), (r.speaker for r in records),
                (text for r in rows for text in r.texts.values()), (r.speaker for r in rows))
            if characters:
                lines += ["CHARACTERS:", *(term.line() for term in characters)]
            if terms:
                lines += ["GLOSSARY:", *(term.line() for term in terms)]
            earlier = translator._earlier_lines(records)
            if earlier:
                lines += ["EARLIER LINES (context only, do not check):", *earlier]
        local: dict[str, tuple[Record, list[str]]] = {}
        for index, record in enumerate(records, 1):
            item, tokens = translator._item(record, str(index), False)
            item["translation"] = translator.masker.mask_like(self.store.translation(record) or "", tokens)
            lines.append(dumps(item))
            local[str(index)] = (record, tokens)
        return "\n".join(lines), local

    def _parse(self, reply: str, local: dict[str, tuple[Record, list[str]]]) -> list[dict[str, Any]]:
        """Reported lines, each with its validated fix; nothing is stored yet."""
        data = extract_json(reply)
        if not isinstance(data, list):
            raise ValueError("expected a JSON array")
        found = []
        for item in data:
            if not isinstance(item, dict) or str(item.get("id")) not in local:
                continue
            record, tokens = local[str(item["id"])]
            problems: list[str] = []
            try:
                masker = self.translator.masker
                fix = masker.unmask(str(item.get("fix", "")).strip(), tokens, self.cfg.tags.strict_order)
                fix = self.translator.validator.normalize(fix)
                problems = self.translator.validator.problems(record, fix)
            except ValueError as exc:
                fix, problems = "", [str(exc)]
            found.append({"record": record, "type": str(item.get("type", "")), "note": str(item.get("note", "")),
                          "fix": fix, "problems": problems})
        return found

    def _review(self, provider: Provider, batch: list[Record], full: bool) -> list[dict[str, Any]]:
        attempts = max(1, self.cfg.proofread.attempts)
        for attempt in range(1, attempts + 1):
            prompt, local = self.prompt(batch, full)
            try:
                completion = provider.complete(self.system, prompt)
                self.store.add_usage(provider.type, completion.model, completion.usage,
                                     len(prompt) + len(self.system))
                self.store.log_response({"check": batch[0].id, "lines": len(batch), "reply": completion.text})
                found = self._parse(completion.text, local)
            except (AuthError, QuotaExhausted):
                raise
            except (ProviderError, ValueError):
                if attempt == attempts:
                    raise
                provider.reset()
                full = True
                continue
            ids = [item["record"].id for item in found]
            lost = len(ids) != len(set(ids)) or any("marker mismatch" in p for item in found for p in item["problems"])
            if not lost or attempt == attempts:
                return list({item["record"].id: item for item in reversed(found)}.values())
            # A repeated id or another line's markers mean the model lost the numbering: ask once more afresh.
            provider.reset()
            full = True
        return []

    def _store(self, batch: list[Record], found: list[dict[str, Any]]) -> dict[str, int]:
        keys = {record.id: [_key(self.store.translation(record) or "")] for record in batch}
        counts = {"fixed": 0, "rejected": 0}
        changes = []
        for item in found:
            record, fix = item["record"], item["fix"]
            before = self.store.translation(record) or ""
            change = {"id": record.id, "scene": record.scene, "type": item["type"], "note": item["note"],
                      "source": record.source, "before": before}
            if item["type"] in REVIEW_ONLY:
                change["rejected"] = f"review: {fix}"
            elif fix and fix != before and not item["problems"]:
                self.store.put(record, fix, "checked", previous=before, check=item["type"])
                keys[record.id].append(_key(fix))
                change["after"] = fix
            else:
                change["rejected"] = "; ".join(item["problems"]) or "no change"
            counts["fixed" if "after" in change else "rejected"] += 1
            changes.append(change)
        for record_id, hashes in keys.items():
            append_jsonl(self.log_path, {"id": record_id, "checked": hashes})
        for change in changes:
            append_jsonl(self.log_path, {"id": change["id"], "change": change})
        return counts

    def _run_chain(self, chain: list[list[Record]], report: dict[str, Any]) -> None:
        """One worker per scene: its parts continue the conversation and see the earlier fixes."""
        provider = self.translator._provider()
        provider.reset()
        for index, batch in enumerate(chain):
            if self._stop.is_set():
                return
            try:
                # ponytail: a session lost in a provider retry sends one batch without its header; fixes still validate
                full = index == 0 or not getattr(provider, "conversation", False)
                counts = self._store(batch, self._review(provider, batch, full))
            except (AuthError, QuotaExhausted) as exc:
                if not self._stop.is_set():
                    log.error("stopping: %s", exc)
                    report["stopped"] = str(exc)
                self._stop.set()
                return
            except (ProviderError, ValueError) as exc:  # one batch must not stop the others; rerun resumes it
                log.warning("%s: %s", batch[0].scene, exc)
                provider.reset()
                counts = {"failed": len(batch)}
            with self._lock:
                for key, value in counts.items():
                    report[key] += value
            log.info("%s: %s", batch[0].scene or "-", counts)

    def write_changes(self) -> Path:
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(["id", "scene", "type", "note", "source", "before", "after", "rejected"])
        for row in iter_jsonl(self.log_path):
            change = row.get("change")
            if change:
                writer.writerow([change[key] for key in ("id", "scene", "type", "note", "source", "before")]
                                + [change.get("after", ""), change.get("rejected", "")])
        path = self.cfg.work_dir / "check_changes.csv"
        write_text_atomic(path, buffer.getvalue(), "utf-8-sig")
        return path

    def run(self, *, scenes: list[str] | None = None, limit: int | None = None,
            dry_run: bool = False) -> dict[str, Any]:
        todo = self.pending(scenes)[: limit or None]
        batches = self.batches(todo)
        report: dict[str, Any] = {"selected": len(todo), "batches": len(batches), "fixed": 0, "rejected": 0,
                                  "failed": 0, "stopped": None}
        if dry_run:
            if batches:
                print(f"=== SYSTEM ({len(self.system)} chars) ===\n{self.system}\n")
                print(f"=== USER, batch 1/{len(batches)} ===\n{self.prompt(batches[0], True)[0]}")
            return report
        chains = chain_by_scene(batches, lambda batch: {batch[0].scene})
        workers = max(1, min(self.cfg.proofread.workers, len(chains) or 1))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="check") as pool:
            for future in [pool.submit(self._run_chain, chain, report) for chain in chains]:
                future.result()
        return {**report, "changes": str(self.write_changes())}
