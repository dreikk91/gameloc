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
from gameloc.cli import audit, grep, names, proofread_auto, set_translation, show
from gameloc.config import Config
from gameloc.formats import load_table
from gameloc.glossary import Glossary
from gameloc.providers import QuotaExhausted
from gameloc.records import Project, Record
from gameloc.text import Validator, make_validator
from gameloc.util import extract_json, pack_scenes


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


def test_cyrillic_homoglyphs() -> None:
    cfg = config_from_dict({"source": {"path": "x", "langs": {"en": "t"}}}, Path("."))
    validator = Validator(cfg, TagMasker.from_config(cfg.tags))
    assert validator.normalize("[panel=1]Несiть, сер Victor. OK, Pаз!") == "[panel=1]Несіть, сер Victor. OK, Раз!"


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
def test_scene_cast_and_earlier_lines(tmp_path: Path) -> None:
    (tmp_path / "strings.json").write_text(json.dumps({"strings": [
        {"id": "1", "en": "Rufus, wait! Odin is coming!", "who": "Alicia", "scene": "ev1"},
        {"id": "2", "en": "What now?", "who": "Rufus", "scene": "ev1"},
        {"id": "3", "en": "Where have you been?", "who": "Rufus", "scene": "ev1"},
        {"id": "4", "en": "Potion", "scene": "menu"},
    ]}), encoding="utf-8")
    (tmp_path / "chars.json").write_text(json.dumps([
        {"en": "Alicia", "uk": "Алісія", "gender": "female"},
        {"en": "Rufus", "uk": "Руфус", "gender": "male"},
        {"en": "Odin", "uk": "Одін", "gender": "male"}]), encoding="utf-8")
    data = {"game": "Test", "target_lang": "uk",
            "source": {"path": "strings.json", "records": "strings", "scene": "scene", "speaker": "who",
                       "langs": {"en": "en"}},
            "output": {"field": "uk"}, "glossary": {"characters": "chars.json", "terms": "chars.json"},
            "translate": {"context_lines": 2}}
    translator = Translator(config_from_dict(data, tmp_path),
                            provider_factory=lambda: FakeProvider(lambda s, u: "[]"))
    translator.store.put(translator.project.by_id["1"], "Руфусе, стривай!", "translated")
    prompt, _ = translator.build_prompt([translator.project.by_id["3"]])
    # Alicia only speaks outside the batch, yet she is in the cast and in the earlier lines
    assert "- Alicia → Алісія [female]" in prompt and "- Rufus → Руфус [male]" in prompt
    assert prompt.count("- Odin → Одін") == 1  # mentioned elsewhere in the scene, listed once
    earlier = prompt.split("EARLIER LINES")[1].split("ITEMS:")[0]
    assert '"speaker":"Алісія [female]","en":"Rufus, wait! Odin is coming!","uk":"Руфусе, стривай!"' in earlier
    assert '"What now?"' in earlier and "Where have you been" not in earlier
    # scenes without speakers get no cast from the rest of the scene
    menu, _ = translator.build_prompt([translator.project.by_id["4"]])
    assert "CHARACTERS" not in menu and "EARLIER LINES" not in menu


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


def test_validator_charset_bytes_fit_plugin(tmp_path: Path) -> None:
    (tmp_path / "widths.json").write_text(json.dumps({"ш": 3}), encoding="utf-8")
    (tmp_path / "gl_test_checks.py").write_text(
        "from gameloc.text import Validator\n\n"
        "class NoX(Validator):\n"
        "    def problems(self, record, text):\n"
        "        return super().problems(record, text) + (['x is banned'] if 'x' in text else [])\n",
        encoding="utf-8")
    cfg = config_from_dict({"source": {"path": "x", "langs": {"en": "t"}},
                            "validate": {"charset": "а-яіїєґА-ЯІЇЄҐ.,!", "length_encoding": "koi8-r",
                                         "latin": False, "plugin": "gl_test_checks:NoX"},
                            "fit": {"widths": "widths.json", "max_width": 5, "max_lines": 2,
                                    "rules": [{"scene": "menu*", "wrap": False, "max_lines": 1}]}}, tmp_path)
    validator = make_validator(cfg, TagMasker.from_config(cfg.tags))
    assert type(validator).__name__ == "NoX"
    line = Record(id="1", texts={"t": "Hi"}, scene="talk")
    assert validator.problems(line, "аб вг") == []
    assert validator.problems(line, "аб вг де жз ий") == ["too long: 3 lines, the box shows 2; make it shorter"]
    assert validator.problems(line, "шш") == ["line 1 is 6 wide, max 5: shorten it"]  # widths file
    assert validator.problems(line, "а@x") == ["characters missing from the game font: @x", "x is banned"]
    assert validator.problems(Record(id="2", texts={"t": "A\nB"}, scene="menu1"), "аб\nвг") == [
        "too long: 2 lines, the box shows 1; make it shorter"]  # scene rule: no wrap, one line
    assert validator.problems(Record(id="3", texts={"t": "Hi"}, max_length=3), "абв") == []
    assert validator.problems(Record(id="3", texts={"t": "Hi"}, max_length=3), "аі") == [
        "cannot be encoded in koi8-r: 'і'"]


def test_resolve_speech_replace_audit(tmp_path: Path) -> None:
    data = _project(tmp_path)
    (tmp_path / "chars.json").write_text(json.dumps(
        [{"en": "Shion", "uk": "Шіон", "gender": "male", "speaks_as": "female"}]), encoding="utf-8")
    (tmp_path / "terms.json").write_text(json.dumps([{"en": "Bye", "uk": "Бувай"}]), encoding="utf-8")
    data["source"]["langs"] = {"en": {"field": "en", "replace": [["See you", "Bye"]]}}  # type: ignore[index]
    data["glossary"]["terms"] = "terms.json"  # type: ignore[index]
    cfg = config_from_dict(data, tmp_path)
    assert Project(cfg).by_id["c"].texts["en"] == "Bye, {name}."
    assert Glossary.load(cfg).speaker_label("Shion") == "Шіон [male; speaks as female]"
    Translator(cfg, provider_factory=lambda: FakeProvider(_translation_answer())).run()
    assert audit(cfg)["issues"] == 0
    assert audit(cfg, terms=True)["issues"] == 3  # "Шіон" is lost in a and b, "Бувай" in c
    assert [row.get("this") for row in show(cfg, "b", 1)] == [None, True, None]
    assert [row["id"] for row in grep(cfg, r"привіт\{")] == ["c"]

    def reviewer(system: str, user: str) -> str:
        packet = json.loads(user.removeprefix("DATA="))
        stage, records = packet["stage"], []
        for view in packet["records"]:
            decision = {"meaning": "ok", "edit": "keep", "verify": "accept", "resolve": "final"}[stage]
            record = {"id": view["id"], "decision": decision, "note": "Добре."}
            if stage == "verify" and view.get("markers") == ["{0}"]:
                record["decision"] = "reject"
            if stage == "resolve":
                assert view["reason"] == "verify: reject" and view["review"]["verify"] == "Добре."
                record["translation"] = "Бувай, {0}."
            records.append(record)
        return json.dumps({"stage": stage, "records": records}, ensure_ascii=False)

    proofreader = Proofreader(cfg, cfg.work_dir / "proofread" / "r1", provider_factory=lambda: FakeProvider(reviewer))
    proofreader.prepare()
    proofreader.run()
    assert proofreader.export()["needs_review"] == 1
    assert proofreader.run("resolve")["resolve"]["pending"] == 0
    assert proofreader.export()["needs_review"] == 0
    entry = Store(cfg.work_dir).entries["c"]
    assert (entry["text"], entry["status"]) == ("Бувай, {name}.", "proofread") and "resolve" in entry["notes"]


def test_dialogue_scene_batches_run_in_order(tmp_path: Path) -> None:
    rows = [{"id": str(i), "en": f"Line number {i}.", "who": "Rufus", "scene": "ev1"} for i in range(6)]
    rows += [{"id": f"m{i}", "en": f"Menu item {i}", "scene": "menu"} for i in range(6)]
    (tmp_path / "strings.json").write_text(json.dumps({"strings": rows}), encoding="utf-8")
    data = {"game": "Test", "target_lang": "uk",
            "source": {"path": "strings.json", "records": "strings", "scene": "scene", "speaker": "who",
                       "langs": {"en": "en"}},
            "output": {"field": "uk"}, "translate": {"max_records": 2, "merge_scenes": False,
                                                     "context_lines": 2, "workers": 4}}
    seen: list[str] = []

    def answer(system: str, user: str) -> str:
        seen.append(user)
        items = [json.loads(line) for line in user.splitlines() if line.startswith('{"id"')]
        return json.dumps([{"id": item["id"], "text": "Рядок " + item["en"][-2]} for item in items])

    translator = Translator(config_from_dict(data, tmp_path), provider_factory=lambda: FakeProvider(answer))
    chains = translator.chains(list(enumerate(translator.batches(translator.pending()), 1)))
    assert [len(chain) for chain in chains] == [3, 1, 1, 1]  # ev1 in one chain, menu batches apart
    translator.run()
    later = [u for u in seen if '"en":"Line number 4."' in u][0]
    assert '"uk":"Рядок 3"' in later  # the previous batch was already translated


def test_proofread_keeps_scenes_together(tmp_path: Path) -> None:
    long = "A fairly long line of dialogue that takes some room in a packet."
    rows = [{"id": f"a{i}", "en": f"{long} {i}", "who": "Rufus", "scene": "ev1"} for i in range(3)]
    rows += [{"id": f"b{i}", "en": f"{long} {i}", "who": "Alicia", "scene": "ev2"} for i in range(40)]
    (tmp_path / "strings.json").write_text(json.dumps({"strings": rows}), encoding="utf-8")
    data = {"game": "Test", "target_lang": "uk",
            "source": {"path": "strings.json", "records": "strings", "scene": "scene", "speaker": "who",
                       "langs": {"en": "en"}},
            "output": {"field": "uk"}, "proofread": {"max_chars": 8000, "context_lines": 2}}
    cfg = config_from_dict(data, tmp_path)
    store = Store(cfg.work_dir)
    for record in Project(cfg).records:
        store.put(record, "Переклад " + record.id.lstrip("ab"), "translated")
    edit_messages: list[dict[str, object]] = []

    def reviewer(system: str, user: str) -> str:
        packet = json.loads(user.removeprefix("DATA="))
        stage = packet["stage"]
        if stage == "edit":
            edit_messages.append(packet)
        records = []
        for view in packet["records"]:
            decision = {"meaning": "ok", "edit": "change", "verify": "accept"}[stage]
            item = {"id": view["id"], "decision": decision, "note": "Добре."}
            if stage == "edit":
                item["translation"] = "Виправлено " + view["translation"]
            records.append(item)
        return json.dumps({"batch_id": packet["batch_id"], "stage": stage, "records": records}, ensure_ascii=False)

    proofreader = Proofreader(cfg, cfg.work_dir / "proofread" / "r1", provider_factory=lambda: FakeProvider(reviewer))
    proofreader.prepare()
    packets = list(proofreader.packets("meaning").values())
    scenes = [{proofreader.snapshot["records"][rid]["scene"] for rid in p["ids"]} for p in packets]
    assert "ev1" in scenes[0]
    assert len(packets) > 2 and sum("ev1" in s for s in scenes) == 1  # the small scene is never split
    split = [p for p, s in zip(packets, scenes, strict=True) if s == {"ev2"}][1:]
    assert split and all(len(p["earlier_ids"]) == 2 for p in split)
    assert max(len(c) for c in proofreader.chains(packets)) == len([s for s in scenes if "ev2" in s])

    proofreader.run()
    continued = [m for m in edit_messages if m.get("earlier")]
    # a part that continues ev2 sees the previous part's corrections, not the drafts
    assert continued and all(line["translation"].startswith("Виправлено ")
                             for m in continued for line in m["earlier"])  # type: ignore[union-attr]


def test_proofread_max_records(tmp_path: Path) -> None:
    rows = [{"id": f"r{i}", "en": f"Short line {i}.", "who": "Rufus", "scene": "ev1"} for i in range(25)]
    (tmp_path / "strings.json").write_text(json.dumps({"strings": rows}), encoding="utf-8")
    data = {"game": "Test", "target_lang": "uk",
            "source": {"path": "strings.json", "records": "strings", "scene": "scene", "speaker": "who",
                       "langs": {"en": "en"}},
            "output": {"field": "uk"}, "proofread": {"max_records": 10, "context_lines": 3}}
    cfg = config_from_dict(data, tmp_path)
    store = Store(cfg.work_dir)
    for record in Project(cfg).records:
        store.put(record, "Рядок", "translated")
    proofreader = Proofreader(cfg, cfg.work_dir / "proofread" / "r1",
                              provider_factory=lambda: FakeProvider(lambda s, u: "{}"))
    proofreader.prepare()
    packets = list(proofreader.packets("meaning").values())
    assert [len(p["ids"]) for p in packets] == [10, 10, 5]
    assert [len(p.get("earlier_ids", [])) for p in packets] == [0, 3, 3]


def test_pack_scenes_continued_budget() -> None:
    items = [("ev", i) for i in range(6)] + [("menu", i) for i in range(6)]
    groups = pack_scenes(items, lambda item: item[0], lambda item: 10, 30,
                         continued=lambda first: 20 if first[0] == "ev" else 30)
    # the first part of a split dialogue scene gets the full budget, its continuations less room
    assert [len(group) for group in groups] == [3, 2, 1, 3, 3]


def _proofread_project(tmp_path: Path) -> Config:
    cfg = config_from_dict(_project(tmp_path), tmp_path)
    Translator(cfg, provider_factory=lambda: FakeProvider(_translation_answer())).run()
    return cfg


def _approver(system: str, user: str) -> str:
    packet = json.loads(user.removeprefix("DATA="))
    stage = packet["stage"]
    decision = {"meaning": "ok", "edit": "keep", "verify": "accept"}[stage]
    records = [{"id": view["id"], "decision": decision, "note": "Добре."} for view in packet["records"]]
    return json.dumps({"batch_id": packet["batch_id"], "stage": stage, "records": records}, ensure_ascii=False)


def test_hand_edits_skip_proofreading(tmp_path: Path) -> None:
    cfg = _proofread_project(tmp_path)
    with pytest.raises(ValueError, match="markers|tag"):
        set_translation(cfg, "c", "Бувай.")  # {name} is lost
    assert set_translation(cfg, "c", "Бувай, {name}.")["previous"] == "Привіт{name}"
    proofreader = Proofreader(cfg, cfg.work_dir / "proofread" / "r1",
                              provider_factory=lambda: FakeProvider(_approver))
    report = proofreader.prepare()
    assert (report["records"], report["manual_skipped"]) == (2, 1)
    proofreader.run()
    proofreader.export()
    entry = Store(cfg.work_dir).entries["c"]
    assert (entry["text"], entry["status"]) == ("Бувай, {name}.", "manual")


def test_proofread_auto_continues_then_starts_new(tmp_path: Path) -> None:
    cfg = _proofread_project(tmp_path)
    calls = {"n": 0}

    def flaky(system: str, user: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise QuotaExhausted("stop")
        return _approver(system, user)

    first = proofread_auto(cfg, provider_factory=lambda: FakeProvider(flaky))
    assert first["prepare"]["records"] == 3 and first["result"].startswith("not finished")
    second = proofread_auto(cfg, provider_factory=lambda: FakeProvider(_approver))
    assert "prepare" not in second and second["export"]["exported"] == 3  # the same run continued
    third = proofread_auto(cfg, provider_factory=lambda: FakeProvider(_approver))
    assert third["result"] == "nothing to proofread"


def test_names_one_spelling(tmp_path: Path) -> None:
    rows = [{"id": f"m{i}", "en": "Lost Forest", "scene": f"menu{i}"} for i in range(3)]
    rows += [{"id": "p", "en": "Potion", "scene": "items"}, {"id": "q", "en": "Potion", "scene": "shop"},
             {"id": "t", "en": "Lost Forest", "who": "Rufus", "scene": "ev1"}]
    (tmp_path / "strings.json").write_text(json.dumps({"strings": rows}), encoding="utf-8")
    (tmp_path / "terms.json").write_text(json.dumps([{"en": "Potion", "uk": "Зілля"}]), encoding="utf-8")
    cfg = config_from_dict({"game": "Test", "target_lang": "uk",
                            "source": {"path": "strings.json", "records": "strings", "scene": "scene",
                                       "speaker": "who", "langs": {"en": "en"}},
                            "output": {"field": "uk"}, "glossary": {"terms": "terms.json"}}, tmp_path)
    store = Store(cfg.work_dir)
    project = Project(cfg)
    for rid, text in {"m0": "Загублений ліс", "m1": "Забутий ліс", "m2": "Загублений ліс",
                      "p": "Мікстура", "q": "Зілля", "t": "Забутий ліс"}.items():
        store.put(project.by_id[rid], text, "translated")
    assert names(cfg)["would_change"] == 2
    assert names(cfg, apply=True)["changed"] == 2
    store = Store(cfg.work_dir)
    assert {rid: store.entries[rid]["text"] for rid in ("m1", "p", "t")} == {
        "m1": "Загублений ліс", "p": "Зілля", "t": "Забутий ліс"}  # dialogue lines are left alone
    assert store.entries["m1"]["status"] == "manual"
