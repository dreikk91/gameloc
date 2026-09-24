"""Command line interface: ``tr-pipeline --help``."""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config, load_config
from .glossary import Glossary
from .proofread import STAGES, Proofreader
from .providers import create_provider
from .records import Project
from .store import Store
from .text import TagMasker, Validator
from .translate import Translator
from .util import read_json, read_text, write_json, write_text_atomic

log = logging.getLogger("tr_pipeline")

TEMPLATE = '''# tr-pipeline project file. Paths are relative to this file.
game = "My Game"
target_lang = "uk"
workdir = "tr_work"            # checkpoints, logs, proofreading runs, output
provider = "openai"            # a [providers.NAME] profile below or a bare type

[source]
path = "strings/*.json"        # file or glob: .json .jsonl .csv .tsv .po
id = "id"
scene = "@file"                # field, or @file to group by file name
# speaker = "speaker"
# context = ["comment"]
# max_length = "max_len"
# target = "uk"                # existing translations to import
# tag_lang = "en"              # language whose tags are the contract

[source.langs]                 # order = priority, first is the primary source
en = "text"
# ja = "ja"

[output]
path = "{workdir}/output/{relpath}"
# field = "uk"                 # default: source.target or the primary text field

[glossary]
# characters = ["glossary/characters.json"]
# terms = ["glossary/*.json"]

[tags]
# patterns = ['\\[[^\\]]+\\]']   # engine-specific tags on top of the defaults

[translate]
max_chars = 6000
workers = 1

[proofread]
max_chars = 12000

[prompt]
# rules = "Use the informal 'ти' between friends."

[providers.openai]
type = "openai"
model = "gpt-5-mini"

[providers.gemini]
type = "gemini"
models = ["gemini-flash-latest", "gemini-flash-lite-latest"]
rpm = 10

[providers.local]
type = "ollama"
model = "qwen3:14b"
'''


def _status(cfg: Config) -> dict[str, Any]:
    project, store = Project(cfg), Store(cfg.work_dir)
    translatable = [r for r in project.records if r.translatable]
    statuses: Counter[str] = Counter()
    pending_by_scene: Counter[str] = Counter()
    stale = 0
    for record in translatable:
        status = store.status(record)
        if status and store.translation(record) is not None:
            statuses[status] += 1
        else:
            if store.entries.get(record.id, {}).get("text"):
                stale += 1
            pending_by_scene[record.scene or "-"] += 1
    errors = sum(1 for _ in open(store.errors_path, encoding="utf-8")) if store.errors_path.exists() else 0
    return {"records": len(project.records), "translatable": len(translatable),
            "done": sum(statuses.values()), "by_status": dict(statuses),
            "pending": sum(pending_by_scene.values()), "stale_source_changed": stale,
            "error_log_lines": errors, "pending_top_scenes": dict(pending_by_scene.most_common(10))}


def audit(cfg: Config, requeue: bool = False) -> dict[str, Any]:
    """Re-validate every stored translation and look for forbidden glossary variants."""
    project, store = Project(cfg), Store(cfg.work_dir)
    masker = TagMasker.from_config(cfg.tags)
    validator = Validator(cfg, masker)
    forbidden = [(term, variant) for term in Glossary.load(cfg).terms for variant in term.forbidden]
    issues: list[dict[str, Any]] = []
    for record in project.records:
        text = store.translation(record)
        if text is None or not record.translatable:
            continue
        problems = validator.problems(record, text)
        folded = text.casefold()
        problems += [f"'{variant}' should be '{term.target}'" for term, variant in forbidden
                     if variant.casefold() in folded]
        if problems:
            issues.append({"id": record.id, "scene": record.scene, "source": record.source,
                           "translation": text, "problems": problems})
            if requeue:
                store.requeue(record, "; ".join(problems))
    path = cfg.work_dir / "audit.json"
    write_json(path, issues)
    return {"issues": len(issues), "requeued": len(issues) if requeue else 0, "report": str(path)}


def sheet_export(cfg: Config, path: Path, scenes: list[str] | None = None) -> int:
    """Spreadsheet for human translators/editors: edit the ``translation`` column, then sheet-import."""
    project, store = Project(cfg), Store(cfg.work_dir)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["id", "scene", "speaker", *cfg.langs, "translation", "status"])
    count = 0
    for record in project.records:
        if not record.translatable:
            continue
        if scenes and not any(record.scene.startswith(s) for s in scenes):
            continue
        writer.writerow([record.id, record.scene, record.speaker, *(record.texts.get(lang, "") for lang in cfg.langs),
                         store.translation(record) or "", store.status(record) or ""])
        count += 1
    write_text_atomic(path, buffer.getvalue(), "utf-8-sig")
    return count


def sheet_import(cfg: Config, path: Path) -> dict[str, int]:
    project, store = Project(cfg), Store(cfg.work_dir)
    validator = Validator(cfg, TagMasker.from_config(cfg.tags))
    changed = rejected = 0
    for row in csv.DictReader(io.StringIO(read_text(path)[0], newline="")):
        record = project.by_id.get(row.get("id", ""))
        text = (row.get("translation") or "").strip()
        if record is None or not text or text == store.translation(record):
            continue
        problems = validator.problems(record, text)
        if problems:
            log.warning("%s: %s", record.id, "; ".join(problems))
            rejected += 1
            continue
        store.put(record, text, "manual")
        changed += 1
    return {"imported": changed, "rejected": rejected}


def test_provider(cfg: Config, name: str | None, model: str | None) -> str:
    provider = create_provider(cfg.provider_options(name or cfg.translate.provider, model))
    completion = provider.complete(
        f"Translate the item into {cfg.target_name}. Reply with a JSON array only: "
        '[{"id":"1","text":"..."}]',
        '{"id":"1","en":"Hello, {0}! Ready to go?","markers":["{0}"]}')
    return f"{provider.type}:{completion.model} -> {completion.text}"


def _latest_run(cfg: Config) -> Path:
    runs = sorted(p for p in (cfg.work_dir / "proofread").glob("*") if (p / "snapshot.json").exists())
    if not runs:
        raise ValueError("no proofreading run found; use: proofread prepare")
    return runs[-1]


def _print(value: Any) -> None:
    print(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tr-pipeline", description="AI translation and proofreading of game text.")
    parser.add_argument("-c", "--config", type=Path, default=Path("tr_pipeline.toml"),
                        help="project file (default: tr_pipeline.toml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="write a commented project file template")
    commands.add_parser("status", help="translation progress")
    commands.add_parser("stats", help="API usage per provider/model")

    p = commands.add_parser("translate", help="translate pending records")
    p.add_argument("--scene", nargs="+", help="only these scenes (prefix match)")
    p.add_argument("--ids", nargs="+", help="only these record ids")
    p.add_argument("--limit", type=int, help="translate at most N records")
    p.add_argument("--provider", help="provider profile name")
    p.add_argument("--model", help="override the model")
    p.add_argument("--workers", type=int, help="parallel requests")
    p.add_argument("--retranslate", action="store_true", help="redo already translated records")
    p.add_argument("--dry-run", action="store_true", help="print the first prompt, call nothing")

    p = commands.add_parser("test", help="send a tiny request to check a provider")
    p.add_argument("--provider")
    p.add_argument("--model")

    p = commands.add_parser("audit", help="re-validate stored translations")
    p.add_argument("--requeue", action="store_true", help="mark failing lines for re-translation")

    commands.add_parser("export", help="write translations into copies of the game files")

    p = commands.add_parser("sheet-export", help="CSV for human review")
    p.add_argument("path", type=Path)
    p.add_argument("--scene", nargs="+")
    p = commands.add_parser("sheet-import", help="import edited translations from a review CSV")
    p.add_argument("path", type=Path)

    proof = commands.add_parser("proofread", help="three-pass proofreading").add_subparsers(
        dest="action", required=True)
    p = proof.add_parser("prepare", help="snapshot translated lines and create meaning packets")
    p.add_argument("run", type=Path, nargs="?")
    p.add_argument("--scene", nargs="+")
    p.add_argument("--include-proofread", action="store_true")
    p = proof.add_parser("run", help="answer packets with the provider (all stages by default)")
    p.add_argument("run", type=Path, nargs="?")
    p.add_argument("--stage", choices=STAGES)
    p.add_argument("--limit", type=int, help="at most N packets per stage")
    p.add_argument("--provider")
    p.add_argument("--model")
    p.add_argument("--workers", type=int)
    for name, help_text in (("advance", "create packets of the next stage"),
                            ("next", "write the next packet prompt for manual review")):
        p = proof.add_parser(name, help=help_text)
        p.add_argument("stage", choices=STAGES[1:] if name == "advance" else STAGES)
        p.add_argument("run", type=Path, nargs="?")
    p = proof.add_parser("submit", help="submit a manual answer file")
    p.add_argument("stage", choices=STAGES)
    p.add_argument("batch")
    p.add_argument("response", type=Path)
    p.add_argument("run", type=Path, nargs="?")
    p.add_argument("--reviewer", default="manual")
    for name in ("status", "export"):
        proof.add_parser(name).add_argument("run", type=Path, nargs="?")
    return parser


def run(args: argparse.Namespace) -> Any:
    if args.command == "init":
        if args.config.exists():
            raise ValueError(f"{args.config} already exists")
        write_text_atomic(args.config, TEMPLATE)
        return f"created {args.config}"
    cfg = load_config(args.config)
    if args.command == "status":
        return _status(cfg)
    if args.command == "stats":
        path = cfg.work_dir / "stats.json"
        return read_json(path) if path.exists() else {}
    if args.command == "translate":
        if args.workers:
            cfg.translate.workers = args.workers
        report = Translator(cfg, provider=args.provider, model=args.model).run(
            scenes=args.scene, ids=set(args.ids) if args.ids else None, limit=args.limit,
            retranslate=args.retranslate, dry_run=args.dry_run)
        return vars(report)
    if args.command == "test":
        return test_provider(cfg, args.provider, args.model)
    if args.command == "audit":
        return audit(cfg, args.requeue)
    if args.command == "export":
        return [str(path) for path in Project(cfg).export(Store(cfg.work_dir))]
    if args.command == "sheet-export":
        return f"{sheet_export(cfg, args.path, args.scene)} rows -> {args.path}"
    if args.command == "sheet-import":
        return sheet_import(cfg, args.path)

    # proofread
    if args.action == "prepare":
        run_dir = args.run or cfg.work_dir / "proofread" / datetime.now().strftime("%Y%m%d-%H%M%S")
        return Proofreader(cfg, run_dir).prepare(scenes=args.scene, include_proofread=args.include_proofread)
    run_dir = args.run or _latest_run(cfg)
    if args.action == "run":
        if args.workers:
            cfg.proofread.workers = args.workers
        return Proofreader(cfg, run_dir, provider=args.provider, model=args.model).run(args.stage, limit=args.limit)
    proofreader = Proofreader(cfg, run_dir)
    if args.action == "advance":
        return proofreader.advance(args.stage)
    if args.action == "next":
        found = proofreader.next_prompt(args.stage)
        return {"batch": found[0], "prompt": str(found[1])} if found else "no pending packets"
    if args.action == "submit":
        reply = read_text(args.response)[0]
        return {"records": proofreader.submit(args.stage, args.batch, reply, args.reviewer)}
    if args.action == "status":
        return {"run": str(run_dir), **proofreader.status()}
    return proofreader.export()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="backslashreplace")
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    try:
        _print(run(args))
    except KeyboardInterrupt:
        print("interrupted; progress is saved, run the same command to resume", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        if args.verbose:
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
