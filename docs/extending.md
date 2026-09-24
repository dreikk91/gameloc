# Розширення та використання як бібліотеки

## Python API

```python
from pathlib import Path
from gameloc import Project, Proofreader, Store, Translator, load_config

cfg = load_config("gameloc.toml")
cfg.translate.workers = 4                      # будь-яку опцію можна змінити в коді

report = Translator(cfg, provider="gemini").run(scenes=["chapter1"], limit=500)
print(report.translated, report.failed)

proof = Proofreader(cfg, cfg.work_dir / "proofread" / "chapter1")
proof.prepare(scenes=["chapter1"])
proof.run()                                    # усі три стадії
proof.export()

Project(cfg).export(Store(cfg.work_dir))       # у файли гри
```

Конфіг можна зібрати без файлу: `config_from_dict({...}, root=Path("."))`.

Основні класи:

| Клас | Призначення |
|---|---|
| `Config` | налаштування; `load_config`, `config_from_dict` |
| `Project` | завантажує записи (`Record`) і пише переклади назад (`export`) |
| `Record` | `id`, `texts` (мова → текст), `scene`, `speaker`, `context`, `max_length`, `existing` |
| `Store` | чекпоінт: `translation(record)`, `put(...)`, `requeue(...)` |
| `Translator` | `pending()`, `batches()`, `build_prompt()`, `translate_records()`, `run()` |
| `Proofreader` | `prepare`, `run`, `advance`, `next_prompt`, `submit`, `status`, `export` |
| `TagMasker`, `Validator` | маскування тегів і перевірка перекладу |
| `Glossary`, `Term` | глосарій і персонажі |
| `create_provider`, `Provider` | провайдери |

## Власний провайдер

Достатньо реалізувати `_send`; повтори, backoff, rate limit і резервні моделі дає базовий клас.
Кидайте типізовані помилки, щоб спрацювала правильна політика.

```python
from gameloc import Completion, Provider
from gameloc.providers.base import AuthError, RateLimited, TransientError, post_json

class MyProvider(Provider):
    type = "my"
    default_model = "my-model"

    def _send(self, model: str, system: str, user: str) -> Completion:
        data = post_json("https://api.example.com/generate",
                         {"model": model, "system": system, "prompt": user},
                         {"Authorization": f"Bearer {self.options['api_key']}"}, self.timeout)
        return Completion(data["output"], model, {"total_tokens": data.get("tokens", 0)})
```

Підключення:

```toml
[providers.mine]
type = "my_package.providers:MyProvider"
```

або через entry point у `pyproject.toml` вашого пакета — тоді працює `type = "my"`:

```toml
[project.entry-points."gameloc.providers"]
my = "my_package.providers:MyProvider"
```

У коді можна передати будь-яку фабрику: `Translator(cfg, provider_factory=lambda: MyProvider({...}))`
(один екземпляр на воркер). Стан розмови скидайте в `reset()`.

## Власний формат

Фабрика отримує шлях і `[source] options`, повертає обʼєкт із:

- `rows: list[dict]` — змінювані записи (у них пишеться переклад);
- `keys: list[str] | None` — id із ключів, якщо в рядках їх немає;
- `save(dest: Path)` — серіалізація.

```python
from pathlib import Path
from typing import Any

class MsgTable:
    """Рядки формату `id=text`."""

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

Модуль має бути імпортовним (встановлений пакет або тека в `PYTHONPATH`).

## Власні правила перевірки

`Validator.problems(record, text) -> list[str]` можна розширити наслідуванням і передати свій екземпляр:

```python
from gameloc import Translator, Validator

class MyValidator(Validator):
    def problems(self, record, text):
        issues = super().problems(record, text)
        if text.count("\n") > 2:
            issues.append("діалогове вікно вміщує максимум 3 рядки")
        return issues

translator = Translator(cfg)
translator.validator = MyValidator(cfg, translator.masker)
```

## Публікація на PyPI

```bash
uv build                         # dist/gameloc-0.1.0.tar.gz + .whl
uv publish --token pypi-...      # або налаштуйте trusted publishing у GitHub Actions
```

Імʼя `gameloc` на момент створення було вільне на pypi.org, але PyPI може відхилити надто схожі назви,
тож спершу спробуйте TestPyPI: `uv publish --publish-url https://test.pypi.org/legacy/`.
Перед публікацією оновіть `authors`, `project.urls` і версію.
