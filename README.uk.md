# gameloc

[English](README.md) | **Українська**

**gameloc** (game localization) — рушійно-незалежна бібліотека та CLI для AI-перекладу й трирівневої редактури ігрового тексту.
Один TOML-файл описує гру: де лежать рядки, якими мовами, які теги в тексті та через який API
перекладати. Далі конвеєр однаковий для Unity, Unreal Engine 5 і власних рушіїв.

## Можливості

- **Будь-які файли гри**: JSON (список, `id → об'єкт`, `id → рядок`, вкладений шлях), JSONL, CSV/TSV, gettext PO (експорт UE5);
  glob на сотні файлів; власний формат через плагін `module:factory`.
- **Кілька мов-джерел**: напр. японська як першоджерело + англійська як довідка; мови можуть лежати в окремих файлах, які зʼєднуються за id.
- **Будь-який API**: OpenAI-сумісні (OpenAI, Anthropic, OpenRouter, DeepSeek, Mistral, Groq, Ollama, LM Studio, vLLM, Copilot-проксі),
  нативний Gemini, будь-яка CLI-утиліта (`codex exec`, `claude -p`, `gemini`...). Власні провайдери через entry points.
- **Чекпоінти**: кожен прийнятий рядок одразу пишеться в `translations.jsonl` (fsync). Перервали — запустили знову, продовжить з того ж місця.
  Змінився текст оригіналу — рядок перекладеться наново.
- **Захист тегів**: `<color>`, `{0}`, `{Name}`, `%d`, `\n`, власні регекси маскуються в `{0}`, `{1}`… і перевіряються після перекладу.
- **Валідація**: теги, заборонені символи (CJK, російські літери для `uk`), символи, яких немає у шрифті гри,
  розмір вікна (ширина в пікселях, перенесення, кількість рядків), ліміти в байтах, неперекладені латинські слова, ліміт довжини рядка.
  Невдалі рядки повертаються моделі з описом помилки.
- **Контекст**: групування за сценами, мовець зі статтю, профілі персонажів і глосарій — лише релевантні для конкретного пакета.
- **Трирівнева редактура** `meaning → edit → verify` з незмінним snapshot, перевіркою кожної відповіді та ручним режимом через будь-який чат.
- Дедуплікація однакових рядків, розбиття пакета при переповненні контексту, паралельні воркери, rate limit/RPM, ланцюжок резервних моделей,
  статистика токенів, аудит, CSV-таблиця для ручної правки.
- **Нуль залежностей** — лише стандартна бібліотека Python ≥ 3.11. Повна анотація типів (`mypy --strict`).

## Встановлення

```bash
uv tool install gameloc        # як CLI
uv add gameloc                 # як бібліотека в проєкт
pip install gameloc            # або так
```

## Швидкий старт

```bash
gameloc init                   # створює закоментований gameloc.toml
# відредагуйте [source] під свої файли, оберіть провайдера
export OPENAI_API_KEY=...      # або GEMINI_API_KEY / ANTHROPIC_API_KEY ...
gameloc status                 # скільки рядків знайдено й перекладено
gameloc translate --dry-run    # показати перший промпт, нічого не надсилати
gameloc translate              # перекласти все (Ctrl+C безпечний)
gameloc proofread prepare      # трирівнева редактура
gameloc proofread run
gameloc proofread export
gameloc export                 # записати переклад у копії файлів гри
```

Мінімальний конфіг для Unity-JSON:

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

## Команди

| Команда | Що робить |
|---|---|
| `init` | шаблон `gameloc.toml` |
| `status` | прогрес: перекладено / в черзі / застаріло, топ сцен у черзі |
| `translate [--scene S] [--ids ...] [--limit N] [--provider P] [--model M] [--workers N] [--retranslate] [--dry-run]` | переклад |
| `test [--provider P]` | тестовий запит до провайдера |
| `audit [--requeue] [--terms]` | перевалідувати збережені переклади, знайти заборонені варіанти (і пропущені терміни) з глосарію |
| `show ID [--context N]` / `grep REGEX` | рядок із сусідами / пошук в оригіналах і перекладах |
| `export` | записати переклади у файли гри (`[output]`) |
| `sheet-export FILE.csv` / `sheet-import FILE.csv` | таблиця для ручної правки перекладачем |
| `proofread prepare / run / advance / next / submit / status / export / resolve` | трирівнева редактура; `resolve` розвʼязує `review.json` |
| `stats` | витрати запитів і токенів по моделях |

Глобальні опції: `-c шлях/до/конфігу.toml`, `-v` (детальний лог).

## Документація

- [Конфігурація](docs/uk/configuration.md) — повний довідник `gameloc.toml`
- [Рушії та формати](docs/uk/engines.md) — готові рецепти для Unity, UE5, власних рушіїв
- [Провайдери](docs/uk/providers.md) — API, CLI-утиліти, ліміти, резервні моделі
- [Редактура](docs/uk/proofreading.md) — трирівнева вичитка, ручний режим
- [Розширення та бібліотека](docs/uk/extending.md) — Python API, власні формати й провайдери, публікація на PyPI

## Розробка

```bash
uv sync
uv run pytest
uv run mypy
uv run ruff check src
```

Ліцензія: MIT.
