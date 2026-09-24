# Configuration

**English** | [Українська](../uk/configuration.md)

A project is described by a single `gameloc.toml` file (or `.json` with the same structure).
Relative paths are resolved from the config file's directory. Unknown keys are an error, so typos are caught at once.

## Top level

| Key | Default | Description |
|---|---|---|
| `game` | `""` | game title used in prompts |
| `target_lang` | `"uk"` | target language code; sets the language name in prompts and the language checks |
| `workdir` | `"gameloc_work"` | working directory: checkpoints, logs, proofreading runs, output |
| `provider` | `"openai"` | active profile from `[providers.*]`, or a bare provider type |

## `[source]` — where the strings are

| Key | Description |
|---|---|
| `path` | file or glob (`text/**/*.json`). With several files, ids become `relative/path::id` |
| `format` | `auto` (by extension), `json`, `jsonl`, `csv`, `tsv`, `po` or `package.module:factory` |
| `records` | JSON only: dotted path to the record list/map, e.g. `"strings"` or `"data.lines"` |
| `id` | id field (default `id`; for PO `msgctxt\|msgid`). Without it: the JSON map key or the row number |
| `scene` | scene field; `@file` uses the file name. With several files and no `scene`, the file name is the scene |
| `scene_pattern` | regex that extracts the scene from the `scene` field (or the id): group 1 if present |
| `speaker` | speaker field (character name/key from the glossary) |
| `context` | list of fields with explanations for the model (developer comments, `extracted` for PO) |
| `max_length` | field holding the maximum visible length of the line |
| `target` | field with an existing translation — imported instead of translated again |
| `skip_pattern` | regex over the primary text: matching records are not translated (technical lines) |
| `tag_lang` | language whose tags form the technical contract (default: the first language) |
| `encoding` | file encoding (`utf-8-sig`; a BOM is preserved as it was) |
| `delimiter` | CSV delimiter |
| `options` | arbitrary table passed to plugin formats |

**Field paths** are dotted: `dialogue_context.speaker.id`. Alternatives are separated by `|` —
the first non-empty one wins: `"speaker_id|speaker"`.

### `[source.langs]` — source languages

Order matters: **the first language is the authoritative source of meaning**, the others are references.

```toml
[source.langs]
ja = "jp"          # Japanese from the "jp" field — primary source
en = "en"          # English — reference (and the tag contract if tag_lang = "en")
```

A language can live in a separate file; records are joined by id:

```toml
[source.langs.en]
field = "text"

[source.langs.ja]
path = "text/ja/{relpath}"   # {relpath}, {stem}, {name} of the current source file
field = "text"
# format, records, id — as in [source], if needed
```

For languages other than `tag_lang`, tags are stripped from the prompt so the model sees one set of markers only.

## `[output]` — where to write

| Key | Description |
|---|---|
| `path` | path template: `{workdir}`, `{relpath}`, `{stem}`, `{name}`. Default `{workdir}/output/{relpath}` |
| `field` | field for the translation. Default: `source.target`, otherwise the first language's field (replaces the original) |

> Do not write output over the source when the translation replaces the original field: the next read would see translated text as the source.

## `[glossary]`

```toml
[glossary]
characters = ["glossary/characters.json"]
terms = ["glossary/*.json"]
max_terms = 30
max_characters = 12
```

The file layout is free-form — fields are looked up by common names:

| What | Candidate fields |
|---|---|
| source | `source`, `term`, `original`, `<lang>`, `name_<lang>`, `en`, `name_en`, `name`, `key` |
| translation | `<target_lang>`, `name_<target>`, `preferred_<target>`, `target`, `translation`, `translated` |
| aliases | `aliases`, `alias`, fields of the other source languages; `"A / B"` is split |
| description | `description`, `description_<target>`, `note`, `role`, `voice`, `category` |
| gender | `gender`, `sex` (`male/female/m/f/чоловіча/жіноча/...`) |
| forbidden variants | `forbidden`, `consistency_rules[].variant` (used by `audit`) |

Container: a list, an object with a `terms`/`characters`/`entries`/`glossary`/`items` list, or a map `"Shion": "Шіон"`.
An entry with `gender` counts as a character. Prompts only include terms that occur in the batch texts
plus the speaking characters.

## `[tags]` — technical tags

| Key | Default | Description |
|---|---|---|
| `patterns` | `[]` | extra regexes for engine tags |
| `defaults` | `true` | built-in: `<...>` (HTML, Unity rich text, TMP, UE rich text), `{...}`, printf `%s %1$d`, literal `\n \r \t` |
| `newlines` | `true` | real line breaks are markers too (the line count is preserved) |
| `strict_order` | `false` | require the same tag order (for engines where order matters) |

Examples: `'\[[a-z_]+=[^\]]*\]'` (`[flag=x]`), `'#\w+#'`, `'\$[A-Z_]+\$'`, `'&[a-z]+;'` (HTML entities).

## `[validate]`

| Key | Default | Description |
|---|---|---|
| `forbid` | per language | regexes that must not match the visible text. Default: CJK for non-CJK targets; for `uk` also `ыэёъ` |
| `latin` | `true` for Cyrillic targets | Latin words absent from the source are an error |
| `allowed_latin` | `[]` | allowed Latin words (`HP`, `OK`, brand names) |
| `max_length` | `true` | check the length limit from `source.max_length` |

## `[translate]`

| Key | Default | Description |
|---|---|---|
| `provider` | — | profile used for translation only |
| `max_chars` | 6000 | request character budget (system + batch) |
| `max_records` | 40 | maximum lines per batch |
| `attempts` | 3 | attempts per line (failed ones return with the error description) |
| `workers` | 1 | parallel requests |
| `group_by_scene` | `true` | keep scenes together in file order |
| `merge_scenes` | `true` | small scenes may share a batch (each line is tagged with its scene) |
| `dedupe` | `true` | identical lines of the same speaker are translated once |
| `reuse` | `true` | reuse the translation of an identical line from the checkpoint |
| `log_responses` | `true` | write raw replies to `responses.jsonl` |

## `[proofread]`

`provider`, `max_chars` (12000), `attempts` (2), `workers` (1). See [proofreading](proofreading.md).

## `[prompt]`

| Key | Description |
|---|---|
| `rules` | extra project rules (text) |
| `rules_file` | the same, from a file |
| `translate_file` | full replacement of the translation system prompt |
| `proofread_file` | replacement of the shared proofreading prompt (stage instructions are appended) |
| `note_language` | language of reviewer notes (default: the target language) |

Custom prompts may use the placeholders `{game}`, `{target}`, `{sources}`, `{priority}`, `{rules}`, `{note_language}`.
`{0}` markers and JSON braces are left untouched.

## `[providers.NAME]`

See [providers](providers.md).

## Working directory

```
gameloc_work/
  translations.jsonl   checkpoint: the last line for an id wins
  errors.jsonl         lines that failed all attempts (the next translate run retries them)
  responses.jsonl      raw model replies
  stats.json           requests / tokens per provider:model
  audit.json           audit report
  proofread/<run>/     proofreading runs
  output/              export result
```
