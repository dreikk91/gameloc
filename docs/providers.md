# Провайдери

Провайдер — це профіль у `[providers.NAME]`. Активний обирається `provider = "NAME"`, окремо для перекладу
(`[translate] provider`) та редактури (`[proofread] provider`) або ключем `--provider NAME`.
Модель можна підмінити `--model`.

## Спільні опції

| Опція | За замовч. | Опис |
|---|---|---|
| `type` | імʼя профілю | `openai`, пресет нижче, `gemini`, `cli`, `пакет.модуль:Клас` або імʼя entry point |
| `model` / `models` | — | модель або ланцюжок резервних: при вичерпанні квоти, повторних 429 чи перевантаженні сервера береться наступна |
| `timeout` | 180 | секунд на запит |
| `retries` | 4 | спроб на запит для тимчасових помилок |
| `retry_delay` | 3 | база експоненційної затримки |
| `min_interval` | 0 | мінімальна пауза між запитами (спільна для всіх воркерів) |
| `rpm` | 0 | ліміт запитів на хвилину (ковзне вікно) |

Політика помилок однакова для всіх провайдерів:

| Помилка | Реакція |
|---|---|
| 401/403, невірний ключ | зупинка запуску |
| 402, денна квота, ліміт чатів | наступна модель з `models`, інакше зупинка |
| 429 | чекати `Retry-After`, після двох поспіль — наступна модель |
| 5xx, перевантаження, мережа, таймаут | повтор з backoff (перевантаження — наступна модель) |
| переповнення контексту | пакет ділиться навпіл |
| зламаний JSON, неправильні теги | рядки повертаються моделі з описом помилки (`attempts`) |

Після зупинки просто запустіть команду ще раз — чекпоінт збережено.

## OpenAI-сумісні API

```toml
[providers.gpt]
type = "openai"            # OPENAI_API_KEY
model = "gpt-5-mini"
temperature = 0.3

[providers.claude]
type = "anthropic"         # ANTHROPIC_API_KEY, OpenAI-сумісний ендпоінт Anthropic
model = "claude-sonnet-5"

[providers.local]
type = "ollama"            # http://localhost:11434/v1
model = "qwen3:14b"
timeout = 600
```

Пресети (`type`) і їхні змінні ключа: `openai` (`OPENAI_API_KEY`), `anthropic` (`ANTHROPIC_API_KEY`),
`openrouter` (`OPENROUTER_API_KEY`), `deepseek` (`DEEPSEEK_API_KEY`), `mistral` (`MISTRAL_API_KEY`),
`groq` (`GROQ_API_KEY`), `gemini-openai` (`GEMINI_API_KEY`), `ollama`, `lmstudio`, `copilot`.
Будь-який інший сервер (vLLM, llama.cpp, корпоративний шлюз) — `type = "openai"` + `base_url`.

| Опція | Опис |
|---|---|
| `base_url` | до `/v1` включно |
| `api_key` / `api_key_env` | ключ або імʼя змінної оточення (краще — не зберігати ключ у файлі) |
| `temperature`, `max_tokens`, `reasoning_effort` | передаються як є |
| `headers` | додаткові HTTP-заголовки |
| `extra` | довільні поля, що домішуються до тіла запиту (`response_format`, `top_p`...) |
| `conversation` | повторно використовувати `conversation_id`, який повертає проксі: системний промпт надсилається раз на чат |
| `max_chat_chars` | 45000 — після цього починається новий чат |
| `max_chats` | ліміт чатів на весь запуск (для проксі з обмеженням сесії) |

Copilot-проксі (як у попередніх скриптах):

```toml
[providers.copilot]
type = "copilot"           # base_url http://127.0.0.1:8000/v1, conversation = true
model = "copilot"
min_interval = 3
max_chats = 300
```

## Gemini (нативний API)

```toml
[providers.gemini]
type = "gemini"            # GEMINI_API_KEY або GOOGLE_API_KEY
models = ["gemini-flash-latest", "gemini-flash-lite-latest"]
rpm = 10
temperature = 0.2
safety_off = true          # BLOCK_NONE — ігрові діалоги часто містять насильство
# generation = { maxOutputTokens = 8192 }
```

Відповідь запитується в JSON-режимі. Безкоштовний тариф: задайте `rpm` і ланцюжок `models`.

## CLI-утиліти

Будь-яка програма, що читає промпт і друкує відповідь.

```toml
[providers.codex]
type = "cli"
model = "gpt-5.6-luna"
timeout = 300
command = ["codex", "exec", "--model", "{model}", "--sandbox", "read-only", "--skip-git-repo-check",
           "--color", "never", "-c", "approval_policy=\"never\"",
           "--output-last-message", "{output_file}", "-"]

[providers.claude-cli]
type = "cli"
model = "claude-sonnet-5"
command = ["claude", "-p", "--model", "{model}"]

[providers.agy]
type = "cli"
models = ["Gemini 3.8 Flash (Low)", "Gemini 3.8 Flash (Medium)"]
stdin = false
command = ["agy.exe", "-p", "{prompt}", "--model", "{model}"]
```

| Опція | Опис |
|---|---|
| `command` | аргументи; `{model}`, `{output_file}` (відповідь читається з файлу), `{prompt}` |
| `stdin` | `true` — промпт надсилається в stdin (рекомендовано: у Windows командний рядок ≤ 32 тис. символів) |
| `env` | додаткові змінні оточення |

CLI-провайдер без стану: системний промпт додається до кожного запиту.

## Перевірка

```bash
tr-pipeline test --provider gemini
tr-pipeline stats
```

## Власний провайдер

Див. [розширення](extending.md#власний-провайдер).
