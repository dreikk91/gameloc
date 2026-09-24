# gameloc

**English** | [Українська](https://github.com/dreikk91/gameloc/blob/master/README.uk.md)

**gameloc** (game localization) is an engine-agnostic library and CLI for AI translation and
three-pass proofreading of video game text. One TOML file describes a game: where the strings live,
which source languages they come in, which engine tags they contain and which AI API to use.
From there the pipeline is the same for Unity, Unreal Engine 5 and custom engines.

## Features

- **Any game files**: JSON (list, `id → object`, `id → string`, nested path), JSONL, CSV/TSV, gettext PO
  (Unreal Engine export); globs over hundreds of files; custom formats through a `module:factory` plugin.
- **Multiple source languages**: e.g. Japanese as the authoritative source plus English as a reference;
  languages may live in separate files joined by id.
- **Any API**: OpenAI-compatible (OpenAI, Anthropic, OpenRouter, DeepSeek, Mistral, Groq, Ollama, LM Studio,
  vLLM, Copilot proxies), native Gemini, any command-line tool (`codex exec`, `claude -p`, `gemini`...).
  Third-party providers plug in through entry points.
- **Checkpoints**: every accepted line is written to `translations.jsonl` immediately (fsync). Interrupt it,
  run it again, and it continues where it stopped. When the source text changes, the line is translated again.
- **Tag protection**: `<color>`, `{0}`, `{Name}`, `%d`, `\n` and your own regexes are masked as `{0}`, `{1}`…
  and verified after translation.
- **Validation**: tags, forbidden characters (CJK; Russian-only letters for Ukrainian), untranslated Latin words,
  line length limits. Rejected lines go back to the model together with the error.
- **Context**: scene grouping, speaker with grammatical gender, character profiles and glossary entries —
  only those relevant to the current batch.
- **Three-pass proofreading** `meaning → edit → verify` with an immutable snapshot, validation of every answer
  and a manual mode for any chat UI.
- Deduplication of identical lines, batch splitting on context overflow, parallel workers, rate limits/RPM,
  fallback model chains, token statistics, audit, CSV sheets for human editors.
- **Zero dependencies**: Python ≥ 3.11 standard library only. Fully typed (`mypy --strict`).

## Installation

```bash
uv tool install gameloc        # as a CLI
uv add gameloc                 # as a library in your project
pip install gameloc            # or with pip
```

## Quick start

```bash
gameloc init                   # writes a commented gameloc.toml
# edit [source] to point at your files, pick a provider
export OPENAI_API_KEY=...      # or GEMINI_API_KEY / ANTHROPIC_API_KEY ...
gameloc status                 # how many lines were found and translated
gameloc translate --dry-run    # print the first prompt, send nothing
gameloc translate              # translate everything (Ctrl+C is safe)
gameloc proofread prepare      # three-pass proofreading
gameloc proofread run
gameloc proofread export
gameloc export                 # write translations into copies of the game files
```

Minimal configuration for Unity JSON files:

```toml
game = "My Game"
target_lang = "uk"
provider = "gemini"

[source]
path = "Assets/Localization/en/*.json"
scene = "@file"

[source.langs]
en = "text"

[providers.gemini]
type = "gemini"
models = ["gemini-flash-latest", "gemini-flash-lite-latest"]
rpm = 10
```

## Commands

| Command | What it does |
|---|---|
| `init` | write a `gameloc.toml` template |
| `status` | progress: translated / pending / stale, top pending scenes |
| `translate [--scene S] [--ids ...] [--limit N] [--provider P] [--model M] [--workers N] [--retranslate] [--dry-run]` | translate |
| `test [--provider P]` | send a tiny request to a provider |
| `audit [--requeue]` | re-validate stored translations, find forbidden glossary variants |
| `export` | write translations into the game files (`[output]`) |
| `sheet-export FILE.csv` / `sheet-import FILE.csv` | spreadsheet round trip for human editors |
| `proofread prepare / run / advance / next / submit / status / export` | three-pass proofreading |
| `stats` | requests and tokens per provider/model |

Global options: `-c path/to/config.toml`, `-v` (verbose log).

## Documentation

- [Configuration](https://github.com/dreikk91/gameloc/blob/master/docs/en/configuration.md) — full `gameloc.toml` reference
- [Engines and formats](https://github.com/dreikk91/gameloc/blob/master/docs/en/engines.md) — ready-made recipes for Unity, UE5 and custom engines
- [Providers](https://github.com/dreikk91/gameloc/blob/master/docs/en/providers.md) — APIs, CLI tools, limits, fallback models
- [Proofreading](https://github.com/dreikk91/gameloc/blob/master/docs/en/proofreading.md) — three-pass review, manual mode
- [Extending and library use](https://github.com/dreikk91/gameloc/blob/master/docs/en/extending.md) — Python API, custom formats and providers, publishing to PyPI

## Development

```bash
uv sync
uv run pytest
uv run mypy
uv run ruff check src
```

License: MIT.
