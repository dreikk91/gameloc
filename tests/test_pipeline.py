from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from gameloc import (
    Completion,
    Proofreader,
    Provider,
    Store,
    TagMasker,
    Translator,
    config_from_dict,
)
from gameloc.formats import load_table
from gameloc.records import Project
from gameloc.util import extract_json


class FakeProvider(Provider):
    type = "fake"

    def __init__(self, answer: Callable[[str, str], str]) -> None:
        super().__init__({"model": "fake"})
        self.answer = answer
        self.calls = 0

    def _send(self, model: str, system: str, user: str) -> Completion:
        self.calls += 1
        return Completion(self.answer(system, user), model, {"total_tokens": 1})


def test_masking_roundtrip() -> None:
    masker = TagMasker.from_config(config_from_dict(
        {"source": {"path": "x", "langs": {"en": "t"}}}, Path(".")).tags)
    masked, tokens = masker.mask("<color=red>Hi</color>, {name}!\n%d coins")
    assert masked == "{0}Hi{1}, {2}!{3}{4} coins"
    assert masker.unmask("｛0｝Привіт{1}, { 2 }!{3}{4} монет", tokens).startswith("<color=red>Привіт</color>")
    with pytest.raises(ValueError):
        masker.unmask("{0}Привіт{1}", tokens)
    assert masker.mask_like("</color>x<color=red>", tokens) == "{1}x{0}"
    assert extract_json('```json\n[{"id":"1","text":"a"},{"id":"2","te') == [{"id": "1", "text": "a"}]


def test_formats_roundtrip(tmp_path: Path) -> None:
    csv_path = tmp_path / "s.csv"
    csv_path.write_bytes(b'Key,English\nk1,"Line one\nline two"\n')
    table = load_table(csv_path)
    table.rows[0]["Ukrainian"] = "Рядок"
    table.save(tmp_path / "out.csv")
    assert load_table(tmp_path / "out.csv").rows[0] == {
        "Key": "k1", "English": "Line one\nline two", "Ukrainian": "Рядок"}

    po_path = tmp_path / "game.po"
    po_path.write_text('msgid ""\nmsgstr ""\n"Language: uk\\n"\n\n#. Key: Hello\nmsgctxt "UI,Hello"\n'
                       'msgid "Hello \\"you\\""\nmsgstr ""\n', encoding="utf-8")
    table = load_table(po_path)
    assert table.rows[0]["msgid"] == 'Hello "you"' and table.rows[0]["extracted"] == "Key: Hello"
    table.rows[0]["msgstr"] = "Привіт\nтобі"
    table.save(tmp_path / "out.po")
    again = load_table(tmp_path / "out.po")
    assert again.rows[0]["msgstr"] == "Привіт\nтобі" and len(again.entries) == 2  # type: ignore[attr-defined]

    flat = tmp_path / "flat.json"
    flat.write_text('{"a": "Hi"}', encoding="utf-8")
    table = load_table(flat)
    table.rows[0]["text"] = "Привіт"
    table.save(flat)
    assert json.loads(flat.read_text(encoding="utf-8")) == {"a": "Привіт"}


def _project(tmp_path: Path) -> dict[str, object]:
    (tmp_path / "strings.json").write_text(json.dumps({"strings": [
        {"id": "a", "en": "Hello <b>Shion</b>!", "who": "Shion", "scene": "s1"},
        {"id": "b", "en": "Hello <b>Shion</b>!", "who": "Shion", "scene": "s1"},
        {"id": "c", "en": "See you, {name}.", "scene": "s2"},
        {"id": "d", "en": "...", "scene": "s2"},
    ]}), encoding="utf-8")
    (tmp_path / "chars.json").write_text(json.dumps(
        [{"en": "Shion", "uk": "Шіон", "gender": "female"}]), encoding="utf-8")
    return {"game": "Test", "target_lang": "uk",
            "source": {"path": "strings.json", "records": "strings", "scene": "scene", "speaker": "who",
                       "langs": {"en": "en"}},
            "output": {"field": "uk"}, "glossary": {"characters": "chars.json"}}


def _translation_answer() -> Callable[[str, str], str]:
    state = {"calls": 0}

    def answer(system: str, user: str) -> str:
        state["calls"] += 1
        items = [json.loads(line) for line in user.splitlines() if line.startswith('{"id"')]
        # the first reply drops a marker to exercise validation feedback and retry
        broken = state["calls"] == 1
        return json.dumps([{"id": item["id"], "text": "Привіт" + ("" if broken else "".join(item.get("markers", [])))}
                           for item in items], ensure_ascii=False)

    return answer


def test_translate_resume_export(tmp_path: Path) -> None:
    cfg = config_from_dict(_project(tmp_path), tmp_path)
    provider = FakeProvider(_translation_answer())
    report = Translator(cfg, provider_factory=lambda: provider).run()
    assert (report.translated, report.failed) == (3, 0)  # "a" and "b" deduplicated, "d" has no text
    assert provider.calls == 2  # small scenes merged into one prompt, retried once after validation

    again = Translator(cfg, provider_factory=lambda: provider).run()
    assert again.selected == 0 and provider.calls == 2  # resumed from the checkpoint

    Project(cfg).export(Store(cfg.work_dir))
    out = json.loads((tmp_path / "gameloc_work/output/strings.json").read_text(encoding="utf-8"))["strings"]
    assert [row.get("uk") for row in out] == ["Привіт<b></b>", "Привіт<b></b>", "Привіт{name}", None]


def test_scene_packing(tmp_path: Path) -> None:
    rows = ([{"id": f"s{s}_{i}", "en": f"Line {i}", "scene": f"small{s}"} for s in range(3) for i in range(2)]
            + [{"id": f"big_{i}", "en": f"Line {i}", "scene": "big"} for i in range(5)]
            + [{"id": "tail", "en": "Bye", "scene": "small3"}])
    (tmp_path / "strings.json").write_text(json.dumps({"strings": rows}), encoding="utf-8")
    cfg = config_from_dict({"source": {"path": "strings.json", "records": "strings", "scene": "scene",
                                       "langs": {"en": "en"}},
                            "translate": {"merge_max_lines": 3, "max_records": 5}}, tmp_path)
    translator = Translator(cfg, provider_factory=lambda: FakeProvider(lambda s, u: "[]"))
    batches = translator.batches(translator.pending())
    assert [[r.scene for r in b] for b in batches] == [
        ["small0"] * 2 + ["small1"] * 2,  # a third small scene would exceed max_records
        ["small2"] * 2,                   # big scene (> merge_max_lines) goes alone
        ["big"] * 5,
        ["small3"],
    ]
    prompt = translator.build_prompt(batches[0])[0]
    assert "SCENE: small0" in prompt and "SCENE: small1" in prompt and '"scene"' not in prompt


def test_three_pass_proofread(tmp_path: Path) -> None:
    cfg = config_from_dict(_project(tmp_path), tmp_path)
    Translator(cfg, provider_factory=lambda: FakeProvider(_translation_answer())).run()

    def reviewer(system: str, user: str) -> str:
        packet = json.loads(user.removeprefix("DATA="))
        stage = packet["stage"]
        records = []
        for view in packet["records"]:
            decision = {"meaning": "ok", "edit": "keep", "verify": "accept"}[stage]
            record = {"id": view["id"], "decision": decision, "note": "Добре."}
            if stage == "edit" and len(view.get("markers", [])) == 2:
                record.update(decision="change", translation="Вітаю{0}{1}!")
            records.append(record)
        return json.dumps({"batch_id": packet["batch_id"], "stage": stage, "records": records}, ensure_ascii=False)

    proofreader = Proofreader(cfg, cfg.work_dir / "proofread" / "r1", provider_factory=lambda: FakeProvider(reviewer))
    assert proofreader.prepare()["records"] == 3
    report = proofreader.run()
    assert all(stage["pending"] == 0 for stage in report.values()) and len(report) == 3
    result = proofreader.export()
    assert (result["exported"], result["changed"], result["needs_review"]) == (3, 2, 0)
    store = Store(cfg.work_dir)
    assert store.entries["a"]["text"] == "Вітаю<b></b>!" and store.entries["a"]["status"] == "proofread"
