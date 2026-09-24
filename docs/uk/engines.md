# Рушії та формати: рецепти

[English](../en/engines.md) | **Українська**

Бібліотека не розпаковує архіви ігор — вона працює з уже витягнутим текстом (JSON/JSONL/CSV/PO)
і записує переклад назад у той самий формат. Розпакування/запакування лишається інструментам рушія
або вашим скриптам; між ними `gameloc` — однаковий для всіх.

## Unity

### I2 Localization / CSV-таблиці

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
field = "Ukrainian"               # колонка додасться, якщо її немає
```

### JSON (Localization package, власні таблиці, TextAsset)

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

Плоский словник `{"menu.start": "Start"}` підтримується без `records`: поля `id` і `text`.

TextMeshPro-теги (`<color>`, `<sprite=3>`, `<link="x">`) покриваються вбудованим шаблоном `<...>`.

## Unreal Engine 5

### PO (Localization Dashboard → Export Text)

```toml
[source]
path = "Content/Localization/Game/en/Game.po"
context = ["extracted"]           # коментарі #. з ключем/джерелом
# id за замовчуванням: msgctxt|msgid ("Namespace,Key")

[source.langs]
en = "msgid"

[output]
path = "Content/Localization/Game/uk/Game.po"
field = "msgstr"
```

Потім *Import Text* у Localization Dashboard. `{Arg}`-аргументи FText і rich-text `<Style>...</>` захищені за замовчуванням.

### String Table CSV

```toml
[source]
path = "Content/StringTables/*.csv"
id = "Key"
scene = "@file"
[source.langs]
en = "SourceString"
[output]
field = "SourceString"            # UE String Table імпортує саме цю колонку
path = "{workdir}/uk/{name}"
```

## Власні рушії

### Кожна сцена — окремий JSON (приклад Xenosaga)

```json
[{"id": 813, "text": "(Is something going on here?)"}, {"id": 827, "text": "/[label(Allen)]"}]
```

```toml
[source]
path = "text_extracted/scene/*.json"   # сцена = ім'я файлу, id = "text_extracted/scene/ST0010.json::813"
skip_pattern = '^/\[label'            # службові рядки не перекладати

[source.langs]
en = "text"

[tags]
patterns = ['/?\[[a-z_]+\([^)]*\)\]']  # команди скрипту /[wait(10)]

[glossary]
characters = "translation/characters.json"
```

### Черга JSONL з контекстом діалогу (приклад AI: The Somnium Files)

```toml
[source]
path = "translation/queue.jsonl"
scene = "asset"
speaker = "dialogue_context.speaker.id"
tag_lang = "en"                   # японський текст — зміст, англійський — контракт тегів
[source.langs]
ja = "jp"
en = "en"
[output]
field = "uk"
[glossary]
characters = "glossary/characters.json"
terms = "glossary/terms.json"
```

### Один великий JSON з наявним перекладом (приклад Romancing SaGa 2)

```toml
[source]
path = "translation/game_strings.json"
records = "strings"
target = "uk"                      # уже перекладене імпортується, не перекладається
[source.langs]
ja = "ja"
en = "en"
[tags]
patterns = ['\[(?:default|flag=[^\[\]]+|platform=[^\[\]]+)\]']
[glossary]
terms = "translation/glossary/*.json"
```

### Мови в різних файлах

```toml
[source]
path = "text/en/*.json"
[source.langs.en]
field = "text"
[source.langs.ja]
path = "text/ja/{name}"
field = "text"
```

### Бінарні або екзотичні формати

Напишіть фабрику, що повертає обʼєкт із `rows` (список словників) і `save(dest)` —
див. [власний формат](extending.md#власний-формат). Або витягніть текст своїм інструментом у JSONL.

## Практичні поради

- **Ліміт довжини** (UI, субтитри): додайте поле з лімітом і `max_length = "maxlen"` — модель отримає `max_len`, валідатор перевірить.
- **Шрифти**: для `uk` за замовчуванням заборонені `ыэёъ` і CJK; якщо шрифт гри не має якихось символів (`ʼ`, `«»`),
  додайте регекс у `[validate] forbid` і правило в `[prompt] rules`.
- **Переноси рядків**: при `tags.newlines = true` кількість рядків збережеться — важливо для діалогових вікон фіксованої висоти.
- **Не перезаписуйте джерело**, поки не завершено переклад: `export` пише в `gameloc_work/output`.
