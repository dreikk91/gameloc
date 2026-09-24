# Engines and formats: recipes

**English** | [Українська](../uk/engines.md)

The library does not unpack game archives — it works with already extracted text (JSON/JSONL/CSV/PO)
and writes the translation back in the same format. Unpacking and repacking stay with the engine's tools
or your own scripts; `gameloc` sits between them and works the same way for every engine.

## Unity

### I2 Localization / CSV tables

```csv
Key,Type,Desc,English,Japanese
Dialog/Intro_01,Text,Hero greets the guard,"Hello, <b>{PLAYER}</b>!",こんにちは
```

```toml
[source]
path = "Localization/I2Languages.csv"
id = "Key"
scene = "Key"
scene_pattern = '^([^/]+)'        # Dialog/Intro_01 -> Dialog
context = ["Desc"]

[source.langs]
en = "English"
ja = "Japanese"

[output]
field = "Ukrainian"               # the column is added if missing
```

### JSON (Localization package, custom tables, TextAsset)

```toml
[source]
path = "Assets/StreamingAssets/lang/en/*.json"
records = "entries"               # {"entries":[{"key":..,"value":..}]}
id = "key"
scene = "@file"
[source.langs]
en = "value"
[output]
path = "{workdir}/output/uk/{name}"
```

A flat dictionary `{"menu.start": "Start"}` works without `records`: its fields are `id` and `text`.

TextMeshPro tags (`<color>`, `<sprite=3>`, `<link="x">`) are covered by the built-in `<...>` pattern.

## Unreal Engine 5

### PO (Localization Dashboard → Export Text)

```toml
[source]
path = "Content/Localization/Game/en/Game.po"
context = ["extracted"]           # "#." comments with the key/source
# default id: msgctxt|msgid ("Namespace,Key")

[source.langs]
en = "msgid"

[output]
path = "Content/Localization/Game/uk/Game.po"
field = "msgstr"
```

Then run *Import Text* in the Localization Dashboard. FText `{Arg}` arguments and rich text `<Style>...</>` are protected by default.

### String Table CSV

```toml
[source]
path = "Content/StringTables/*.csv"
id = "Key"
scene = "@file"
[source.langs]
en = "SourceString"
[output]
field = "SourceString"            # UE String Table imports this column
path = "{workdir}/uk/{name}"
```

## Custom engines

### One JSON file per scene (Xenosaga example)

```json
[{"id": 813, "text": "(Is something going on here?)"}, {"id": 827, "text": "/[label(Allen)]"}]
```

```toml
[source]
path = "text_extracted/scene/*.json"   # scene = file name, id = "text_extracted/scene/ST0010.json::813"
skip_pattern = '^/\[label'            # do not translate technical lines

[source.langs]
en = "text"

[tags]
patterns = ['/?\[[a-z_]+\([^)]*\)\]']  # script commands like /[wait(10)]

[glossary]
characters = "translation/characters.json"
```

### JSONL queue with dialogue context (AI: The Somnium Files example)

```toml
[source]
path = "translation/queue.jsonl"
scene = "asset"
speaker = "dialogue_context.speaker.id"
tag_lang = "en"                   # Japanese carries the meaning, English the tag contract
[source.langs]
ja = "jp"
en = "en"
[output]
field = "uk"
[glossary]
characters = "glossary/characters.json"
terms = "glossary/terms.json"
```

### One large JSON with existing translations (Romancing SaGa 2 example)

```toml
[source]
path = "translation/game_strings.json"
records = "strings"
target = "uk"                      # existing translations are imported, not redone
[source.langs]
ja = "ja"
en = "en"
[tags]
patterns = ['\[(?:default|flag=[^\[\]]+|platform=[^\[\]]+)\]']
[glossary]
terms = "translation/glossary/*.json"
```

### Languages in separate files

```toml
[source]
path = "text/en/*.json"
[source.langs.en]
field = "text"
[source.langs.ja]
path = "text/ja/{name}"
field = "text"
```

### Binary or exotic formats

Write a factory that returns an object with `rows` (a list of dicts) and `save(dest)` —
see [custom formats](extending.md#custom-format). Or extract the text with your own tool into JSONL.

## Practical tips

- **Length limits** (UI, subtitles): add a field with the limit and `max_length = "maxlen"` — the model receives `max_len` and the validator checks it.
- **Fonts**: for `uk`, `ыэёъ` and CJK are forbidden by default; if the game font lacks some characters (`ʼ`, `«»`),
  add a regex to `[validate] forbid` and a rule to `[prompt] rules`.
- **Line breaks**: with `tags.newlines = true` the line count is preserved — important for fixed-height dialogue boxes.
- **Do not overwrite the source** until translation is complete: `export` writes to `gameloc_work/output`.
