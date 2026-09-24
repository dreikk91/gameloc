# Extending and using as a library

**English** | [Українська](../uk/extending.md)

## Python API

```python
from pathlib import Path
from gameloc import Project, Proofreader, Store, Translator, load_config

cfg = load_config("gameloc.toml")
cfg.translate.workers = 4                      # any option can be changed in code

report = Translator(cfg, provider="gemini").run(scenes=["chapter1"], limit=500)
print(report.translated, report.failed)

proof = Proofreader(cfg, cfg.work_dir / "proofread" / "chapter1")
proof.prepare(scenes=["chapter1"])
proof.run()                                    # all three stages
proof.export()

Project(cfg).export(Store(cfg.work_dir))       # into the game files
```

A configuration can be built without a file: `config_from_dict({...}, root=Path("."))`.

Main classes:

| Class | Purpose |
|---|---|
| `Config` | settings; `load_config`, `config_from_dict` |
| `Project` | loads records (`Record`) and writes translations back (`export`) |
| `Record` | `id`, `texts` (language → text), `scene`, `speaker`, `context`, `max_length`, `existing` |
| `Store` | checkpoint: `translation(record)`, `put(...)`, `requeue(...)` |
| `Translator` | `pending()`, `batches()`, `build_prompt()`, `translate_records()`, `run()` |
| `Proofreader` | `prepare`, `run`, `advance`, `next_prompt`, `submit`, `status`, `export` |
| `TagMasker`, `Validator` | tag masking and translation checks |
| `Glossary`, `Term` | glossary and characters |
| `create_provider`, `Provider` | providers |

## Custom provider

Implementing `_send` is enough; retries, backoff, rate limiting and fallback models come from the base class.
Raise the typed errors so the right policy applies.

```python
from gameloc import Completion, Provider
from gameloc.providers.base import post_json

class MyProvider(Provider):
    type = "my"
    default_model = "my-model"

    def _send(self, model: str, system: str, user: str) -> Completion:
        data = post_json("https://api.example.com/generate",
                         {"model": model, "system": system, "prompt": user},
                         {"Authorization": f"Bearer {self.options['api_key']}"}, self.timeout)
        return Completion(data["output"], model, {"total_tokens": data.get("tokens", 0)})
```

`post_json` already maps HTTP statuses to `AuthError`, `QuotaExhausted`, `RateLimited`, `ContextOverflow`
and `TransientError`.

Register it:

```toml
[providers.mine]
type = "my_package.providers:MyProvider"
```

or through an entry point in your package's `pyproject.toml` — then `type = "my"` works:

```toml
[project.entry-points."gameloc.providers"]
my = "my_package.providers:MyProvider"
```

In code you can pass any factory: `Translator(cfg, provider_factory=lambda: MyProvider({...}))`
(one instance per worker). Reset conversation state in `reset()`.

## Custom format

A factory receives the path and `[source] options` and returns an object with:

- `rows: list[dict]` — mutable records (translations are written into them);
- `keys: list[str] | None` — ids taken from keys when rows have none;
- `save(dest: Path)` — serialization.

```python
from pathlib import Path
from typing import Any

class MsgTable:
    """Lines of the form `id=text`."""

    def __init__(self, path: Path, options: dict[str, Any]) -> None:
        self.keys = None
        self.rows = []
        for line in path.read_text(encoding=options.get("encoding", "utf-8")).splitlines():
            key, _, text = line.partition("=")
            self.rows.append({"id": key, "text": text})

    def save(self, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("".join(f"{r['id']}={r['text']}\n" for r in self.rows), encoding="utf-8")

def load(path: Path, options: dict[str, Any]) -> MsgTable:
    return MsgTable(path, options)
```

```toml
[source]
path = "data/*.msg"
format = "my_game.formats:load"
options = { encoding = "cp1251" }
[source.langs]
en = "text"
```

The module must be importable (an installed package or a directory on `PYTHONPATH`).

## Custom validation rules

`Validator.problems(record, text) -> list[str]` can be extended by subclassing:

```python
from gameloc import Translator, Validator

class MyValidator(Validator):
    def problems(self, record, text):
        issues = super().problems(record, text)
        if text.count("\n") > 2:
            issues.append("the dialogue box fits at most 3 lines")
        return issues

translator = Translator(cfg)
translator.validator = MyValidator(cfg, translator.masker)
```

## Publishing to PyPI

```bash
uv build                         # dist/gameloc-0.1.0.tar.gz + .whl
uv publish --token pypi-...      # or set up trusted publishing in GitHub Actions
```

The name `gameloc` was free on pypi.org when the project was created, but PyPI may reject names that are too
similar to existing ones, so try TestPyPI first: `uv publish --publish-url https://test.pypi.org/legacy/`.
Update `authors`, `project.urls` and the version before publishing.
