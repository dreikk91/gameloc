"""Three-pass, file-based, resumable proofreading: meaning -> edit -> verify.

A run directory holds an immutable snapshot of the drafts plus, per stage,
``packets.jsonl`` (prompts) and ``responses/<batch>.json`` (validated answers).
Any packet can be answered by an API worker or by hand (``next`` / ``submit``),
and ``export`` writes accepted lines back to the translation store.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Config
from .glossary import Glossary
from .prompts import proofread_system
from .providers import AuthError, Provider, ProviderError, QuotaExhausted, create_provider
from .records import Project, Record
from .store import Store
from .text import TagMasker, Validator
from .util import dumps, extract_json, iter_jsonl, pack_scenes, read_json, sha256, write_json, write_text_atomic

log = logging.getLogger("gameloc")

STAGES = ("meaning", "edit", "verify")
DECISIONS = {
    "meaning": {"ok", "issue", "needs_review"},
    "edit": {"keep", "change", "needs_review"},
    "verify": {"accept", "reject", "needs_review"},
}
_HEADER_RESERVE = 2500


class Proofreader:
    def __init__(self, cfg: Config, run_dir: Path, *, project: Project | None = None,
                 store: Store | None = None, provider_factory: Callable[[], Provider] | None = None,
                 provider: str | None = None, model: str | None = None) -> None:
        self.cfg = cfg
        self.run_dir = Path(run_dir)
        self._project = project
        self.store = store or Store(cfg.work_dir)
        self.masker = TagMasker.from_config(cfg.tags)
        self.validator = Validator(cfg, self.masker)
        self.glossary = Glossary.load(cfg)
        options = cfg.provider_options(provider or cfg.proofread.provider or cfg.translate.provider, model)
        self.provider_factory = provider_factory or (lambda: create_provider(options))
        self._snapshot: dict[str, Any] | None = None
        self._local = threading.local()
        self._stop = threading.Event()

    @property
    def project(self) -> Project:
        if self._project is None:
            self._project = Project(self.cfg)
        return self._project

    @property
    def snapshot(self) -> dict[str, Any]:
        if self._snapshot is None:
            self._snapshot = read_json(self.run_dir / "snapshot.json")
        return self._snapshot

    # -- records and views -------------------------------------------------

    def record(self, record_id: str) -> Record:
        item = self.snapshot["records"][record_id]
        return Record(id=record_id, texts=item["texts"], scene=item["scene"], speaker=item["speaker"],
                      context=item["context"], max_length=item["max_length"])

    def _tokens(self, record: Record) -> list[str]:
        return self.masker.mask(self.validator.contract(record))[1]

    def _view(self, record_id: str, stage: str, extra: dict[str, Any]) -> dict[str, Any]:
        record = self.record(record_id)
        draft = self.snapshot["records"][record_id]["draft"]
        tag_lang = self.cfg.tag_lang if self.cfg.tag_lang in record.texts else next(iter(record.texts))
        tokens = self._tokens(record)
        view: dict[str, Any] = {}
        if record.scene:
            view["scene"] = record.scene
        if record.speaker:
            view["speaker"] = self.glossary.speaker_label(record.speaker)
        for lang, text in record.texts.items():
            view[lang] = self.masker.mask(text)[0] if lang == tag_lang else self.masker.strip(text)
        if tokens:
            view["markers"] = [f"{{{index}}}" for index in range(len(tokens))]
        if record.context:
            view["context"] = record.context
        if record.max_length:
            view["max_len"] = record.max_length
        if stage == "verify":
            view["before"] = self.masker.mask_like(draft, tokens)
            view["translation"] = self.masker.mask_like(extra.get("proposal") or draft, tokens)
        else:
            view["translation"] = self.masker.mask_like(draft, tokens)
        for key in ("semantic_note", "edit_note"):
            if extra.get(key):
                view[key] = extra[key]
        return view

    def _packet(self, stage: str, items: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
        records = [self.record(record_id) for record_id, _ in items]
        characters, terms = self.glossary.relevant(
            (text for r in records for text in r.texts.values()), (r.speaker for r in records))
        packet: dict[str, Any] = {
            "stage": stage,
            "snapshot_id": self.snapshot["snapshot_id"],
            "records": [{"id": str(index), **{k: v for k, v in view.items()
                                              if k != "scene" or index == 1 or items[index - 2][1].get("scene") != v}}
                        for index, (_, view) in enumerate(items, 1)],
            "characters": [term.line()[2:] for term in characters],
            "glossary": [term.line()[2:] for term in terms],
            "ids": [record_id for record_id, _ in items],
        }
        packet["batch_id"] = sha256(dumps(packet))[:16]
        packet["prompt_sha256"] = sha256(self.render(packet))
        return packet

    @staticmethod
    def data(packet: dict[str, Any]) -> str:
        return "DATA=" + dumps({k: v for k, v in packet.items() if k not in ("ids", "prompt_sha256")})

    def render(self, packet: dict[str, Any]) -> str:
        system: str = self.snapshot["prompts"][packet["stage"]]
        return system + "\n\n" + self.data(packet)

    def _pack(self, stage: str, items: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
        budget = max(1000, self.snapshot["max_chars"] - len(self.snapshot["prompts"][stage]) - _HEADER_RESERVE)
        opts = self.cfg.translate
        groups = pack_scenes(items, lambda item: str(item[1].get("scene", "")), lambda item: len(dumps(item[1])) + 8,
                             budget, merge=opts.merge_scenes, merge_max=opts.merge_max_lines)
        return [self._packet(stage, group) for group in groups]

    # -- files -------------------------------------------------------------

    def packets(self, stage: str) -> dict[str, dict[str, Any]]:
        return {p["batch_id"]: p for p in iter_jsonl(self.run_dir / stage / "packets.jsonl")}

    def _response_path(self, stage: str, batch_id: str) -> Path:
        return self.run_dir / stage / "responses" / f"{batch_id}.json"

    def pending_packets(self, stage: str) -> list[dict[str, Any]]:
        return [p for batch, p in self.packets(stage).items() if not self._response_path(stage, batch).exists()]

    def _save_packets(self, stage: str, packets: list[dict[str, Any]]) -> None:
        (self.run_dir / stage / "responses").mkdir(parents=True, exist_ok=True)
        write_text_atomic(self.run_dir / stage / "packets.jsonl", "".join(dumps(p) + "\n" for p in packets))

    def responses(self, stage: str) -> dict[str, dict[str, Any]]:
        """Validated decisions of a stage by record id."""
        packets = self.packets(stage)
        result: dict[str, dict[str, Any]] = {}
        folder = self.run_dir / stage / "responses"
        for path in sorted(folder.glob("*.json")) if folder.exists() else []:
            saved = read_json(path)
            packet = packets.get(saved.get("batch_id"))
            if not packet or saved.get("prompt_sha256") != packet["prompt_sha256"]:
                raise ValueError(f"response does not match its packet: {path}")
            for item in saved["records"]:
                result[item["id"]] = item
        return result

    # -- stages ------------------------------------------------------------

    def prepare(self, *, scenes: list[str] | None = None, include_proofread: bool = False) -> dict[str, Any]:
        if (self.run_dir / "snapshot.json").exists():
            raise ValueError(f"run already prepared: {self.run_dir}")
        records: dict[str, Any] = {}
        missing = 0
        for record in self.project.records:
            if not record.translatable:
                continue
            if scenes and not any(record.scene == s or record.scene.startswith(s) for s in scenes):
                continue
            draft = self.store.translation(record)
            if draft is None:
                missing += 1
                continue
            if not include_proofread and self.store.status(record) == "proofread":
                continue
            records[record.id] = {"texts": record.texts, "scene": record.scene, "speaker": record.speaker,
                                  "context": record.context, "max_length": record.max_length,
                                  "draft": draft, "draft_hash": sha256(draft)}
        snapshot = {"schema": 1, "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "target_lang": self.cfg.target_lang, "max_chars": self.cfg.proofread.max_chars,
                    "prompts": {stage: proofread_system(self.cfg, stage) for stage in STAGES},
                    "records": records}
        snapshot["snapshot_id"] = sha256(dumps(snapshot))[:16]
        self.run_dir.mkdir(parents=True, exist_ok=True)
        write_json(self.run_dir / "snapshot.json", snapshot)
        self._snapshot = snapshot
        packets = self._pack("meaning", [(rid, self._view(rid, "meaning", {})) for rid in records])
        self._save_packets("meaning", packets)
        report = {"run": str(self.run_dir), "records": len(records), "untranslated": missing,
                  "meaning_packets": len(packets)}
        log.info("prepared %s", report)
        return report

    def advance(self, stage: str) -> dict[str, Any]:
        """Build the packets of ``edit`` or ``verify`` from the previous stage's answers."""
        index = STAGES.index(stage)
        if index == 0:
            raise ValueError("meaning packets are created by prepare")
        if self.packets(stage):
            raise ValueError(f"{stage} packets already exist")
        previous = STAGES[index - 1]
        waiting = len(self.pending_packets(previous))
        if waiting:
            raise ValueError(f"finish stage {previous} first: {waiting} packets pending")
        prior = self.responses(previous)
        meaning = self.responses("meaning")
        items = []
        for record_id in self.snapshot["records"]:
            decision = prior.get(record_id)
            if not decision or decision["decision"] == "needs_review":
                continue
            extra = {"semantic_note": meaning[record_id]["note"]}
            if stage == "verify":
                extra["proposal"] = decision.get("translation")
                extra["edit_note"] = decision["note"]
            items.append((record_id, self._view(record_id, stage, extra)))
        packets = self._pack(stage, items)
        self._save_packets(stage, packets)
        log.info("%s: %d packets for %d records", stage, len(packets), len(items))
        return {"stage": stage, "packets": len(packets), "records": len(items)}

    def check(self, packet: dict[str, Any], reply: str, *, verify_batch: bool = True) -> list[dict[str, Any]]:
        """Validate an answer; returns decisions keyed by real record ids.

        ``verify_batch`` guards manual submits against pasting another packet's answer; automatic runs
        skip it because models often garble the echoed hex id while the rest of the answer is fine.
        """
        stage = packet["stage"]
        data = extract_json(reply)
        if isinstance(data, dict):
            for key in ("batch_id", "stage") if verify_batch else ("stage",):
                if data.get(key) not in (None, packet[key]):
                    raise ValueError(f"{key} mismatch: {data.get(key)!r}")
            data = data.get("records")
        if not isinstance(data, list):
            raise ValueError("answer must contain a records array")
        expected = {str(index): record_id for index, record_id in enumerate(packet["ids"], 1)}
        result: dict[str, dict[str, Any]] = {}
        for item in data:
            local_id = str(item.get("id")) if isinstance(item, dict) else None
            if local_id not in expected:
                raise ValueError(f"unexpected id {local_id!r}")
            if local_id in result:
                raise ValueError(f"duplicate id {local_id}")
            decision = item.get("decision")
            if decision not in DECISIONS[stage]:
                raise ValueError(f"{local_id}: decision must be one of {sorted(DECISIONS[stage])}")
            note = item.get("note")
            if not isinstance(note, str) or not note.strip():
                raise ValueError(f"{local_id}: every decision needs a note")
            clean: dict[str, Any] = {"id": expected[local_id], "decision": decision, "note": note.strip()}
            if stage == "edit" and decision == "change":
                if not isinstance(item.get("translation"), str):
                    raise ValueError(f"{local_id}: change requires translation")
                record = self.record(expected[local_id])
                text = self.masker.unmask(item["translation"].strip(), self._tokens(record),
                                          self.cfg.tags.strict_order)
                text = self.validator.normalize(text)
                problems = self.validator.problems(record, text)
                if problems:
                    raise ValueError(f"{local_id}: {'; '.join(problems)}")
                clean["translation"] = text
            result[local_id] = clean
        missing = sorted(set(expected) - set(result), key=int)
        if missing:
            raise ValueError(f"answer omitted ids {missing}")
        return list(result.values())

    def submit(self, stage: str, batch_id: str, reply: str, reviewer: str = "manual", *,
               verify_batch: bool = True) -> int:
        packet = self.packets(stage).get(batch_id)
        if packet is None:
            raise ValueError(f"unknown batch {batch_id}")
        path = self._response_path(stage, batch_id)
        if path.exists():
            raise ValueError(f"batch {batch_id} already answered")
        records = self.check(packet, reply, verify_batch=verify_batch)
        write_json(path, {"stage": stage, "batch_id": batch_id, "reviewer": reviewer,
                          "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                          "prompt_sha256": packet["prompt_sha256"], "response_sha256": sha256(reply),
                          "records": records})
        return len(records)

    def next_prompt(self, stage: str) -> tuple[str, Path] | None:
        """Write the next unanswered packet to a text file for manual review in any chat UI."""
        pending = self.pending_packets(stage)
        if not pending:
            return None
        packet = pending[0]
        path = self.run_dir / stage / f"next-{packet['batch_id']}.txt"
        write_text_atomic(path, self.render(packet))
        return packet["batch_id"], path

    def _provider(self) -> Provider:
        if not hasattr(self._local, "provider"):
            self._local.provider = self.provider_factory()
        provider: Provider = self._local.provider
        return provider

    def _work(self, stage: str, packet: dict[str, Any]) -> bool:
        if self._stop.is_set():
            return False
        provider = self._provider()
        system = self.snapshot["prompts"][stage]
        error = reply = ""
        for attempt in range(1, self.cfg.proofread.attempts + 1):
            prompt = system if not error else (
                f"{system}\n\nYOUR PREVIOUS ANSWER WAS REJECTED: {error}. Fix only that problem.")
            try:
                completion = provider.complete(prompt, self.data(packet))
                reply = completion.text
                self.store.add_usage(provider.type, completion.model, completion.usage,
                                     len(prompt) + len(self.data(packet)))
                self.submit(stage, packet["batch_id"], completion.text, f"{provider.type}:{completion.model}",
                            verify_batch=False)
                return True
            except (AuthError, QuotaExhausted) as exc:
                if not self._stop.is_set():
                    log.error("stopping: %s", exc)
                self._stop.set()
                return False
            except (ProviderError, ValueError) as exc:
                error = str(exc)[:500]
                invalid = self.run_dir / stage / "invalid" / f"{packet['batch_id']}-{attempt}.txt"
                write_text_atomic(invalid, f"{error}\n\n{reply}")
                provider.reset()
        log.warning("%s %s failed: %s", stage, packet["batch_id"], error)
        return False

    def run(self, stage: str | None = None, *, limit: int | None = None) -> dict[str, Any]:
        """Answer pending packets; without ``stage`` walk through all stages, advancing automatically."""
        report: dict[str, Any] = {}
        for current in [stage] if stage else STAGES:
            if not self.packets(current):
                if current == "meaning":
                    raise ValueError("run prepare first")
                self.advance(current)
            pending = self.pending_packets(current)[: limit or None]
            workers = max(1, min(self.cfg.proofread.workers, len(pending) or 1))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="proofread") as pool:
                done = sum(pool.map(self._work, [current] * len(pending), pending))
            left = len(self.pending_packets(current))
            report[current] = {"answered": done, "pending": left}
            log.info("%s: %d packets answered, %d pending", current, done, left)
            if left or self._stop.is_set():
                break
        return report

    def status(self) -> dict[str, Any]:
        report: dict[str, Any] = {"records": len(self.snapshot["records"])}
        for stage in STAGES:
            decisions = self.responses(stage)
            report[stage] = {"packets": len(self.packets(stage)), "pending": len(self.pending_packets(stage)),
                             "decisions": dict(Counter(d["decision"] for d in decisions.values()))}
        return report

    def export(self) -> dict[str, Any]:
        """Store accepted lines as ``proofread``; list everything else in ``review.json``."""
        unfinished = [stage for stage in STAGES if not self.packets(stage) or self.pending_packets(stage)]
        if unfinished:
            raise ValueError(f"proofreading is not finished (stage {unfinished[0]}): run `proofread run` again")
        snapshot_id = self.snapshot["snapshot_id"]
        notes = {stage: self.responses(stage) for stage in STAGES}
        exported = changed = 0
        review: list[dict[str, Any]] = []
        for record_id, item in self.snapshot["records"].items():
            record = self.record(record_id)
            entry = self.store.entries.get(record_id, {})
            if entry.get("status") == "proofread" and entry.get("snapshot") == snapshot_id:
                continue
            reached = {stage: notes[stage][record_id] for stage in STAGES if record_id in notes[stage]}
            current = self.store.translation(record)
            verdict = reached.get("verify", {}).get("decision")
            final = reached.get("edit", {}).get("translation") or item["draft"]
            reason = ""
            if current is None or sha256(current) != item["draft_hash"]:
                reason = "draft or source changed since the snapshot"
            elif verdict != "accept":
                last = next(iter(reversed(reached.values())), None)
                reason = f"{list(reached)[-1]}: {last['decision']}" if last else "not reviewed"
            elif problems := self.validator.problems(record, final):
                reason = "; ".join(problems)
            if reason:
                review.append({"id": record_id, "scene": record.scene, "reason": reason,
                               "source": record.source, "draft": item["draft"], "proposal": final,
                               "notes": {stage: d["note"] for stage, d in reached.items()}})
                continue
            if final != item["draft"]:
                changed += 1
            self.store.put(record, final, "proofread", snapshot=snapshot_id, previous=item["draft"],
                           notes={stage: d["note"] for stage, d in reached.items()})
            exported += 1
        write_json(self.run_dir / "review.json", review)
        report = {"exported": exported, "changed": changed, "needs_review": len(review),
                  "review_file": str(self.run_dir / "review.json")}
        log.info("export %s", report)
        return report
