# Providers

**English** | [Українська](../uk/providers.md)

A provider is a profile in `[providers.NAME]`. The active one is chosen with `provider = "NAME"`, separately for
translation (`[translate] provider`) and proofreading (`[proofread] provider`), or with `--provider NAME`.
The model can be overridden with `--model`.

## Common options

| Option | Default | Description |
|---|---|---|
| `type` | profile name | `openai`, a preset below, `gemini`, `cli`, `package.module:Class` or an entry point name |
| `model` / `models` | — | a model or a fallback chain: the next one is used when quota runs out, after repeated 429s or when the server is overloaded |
| `timeout` | 180 | seconds per request |
| `retries` | 4 | attempts per request for transient errors |
| `retry_delay` | 3 | exponential backoff base |
| `min_interval` | 0 | minimum pause between requests (shared by all workers) |
| `rpm` | 0 | requests-per-minute limit (sliding window) |

Every provider follows the same error policy:

| Error | Reaction |
|---|---|
| 401/403, invalid key | stop the run |
| 402, daily quota, chat limit | next model from `models`, otherwise stop |
| 429 | wait for `Retry-After`; after two in a row switch to the next model |
| 5xx, overload, network, timeout | retry with backoff (overload switches model) |
| context overflow | the batch is split in half |
| broken JSON, wrong tags | lines go back to the model with the error description (`attempts`) |

After a stop, just run the same command again — the checkpoint is saved.

## OpenAI-compatible APIs

```toml
[providers.gpt]
type = "openai"            # OPENAI_API_KEY
model = "gpt-5-mini"
temperature = 0.3

[providers.claude]
type = "anthropic"         # ANTHROPIC_API_KEY, Anthropic's OpenAI-compatible endpoint
model = "claude-sonnet-5"

[providers.local]
type = "ollama"            # http://localhost:11434/v1
model = "qwen3:14b"
timeout = 600
```

Presets (`type`) and their key variables: `openai` (`OPENAI_API_KEY`), `anthropic` (`ANTHROPIC_API_KEY`),
`openrouter` (`OPENROUTER_API_KEY`), `deepseek` (`DEEPSEEK_API_KEY`), `mistral` (`MISTRAL_API_KEY`),
`groq` (`GROQ_API_KEY`), `gemini-openai` (`GEMINI_API_KEY`), `ollama`, `lmstudio`, `copilot`.
Any other server (vLLM, llama.cpp, a corporate gateway) — `type = "openai"` plus `base_url`.

| Option | Description |
|---|---|
| `base_url` | up to and including `/v1` |
| `api_key` / `api_key_env` | the key or the name of an environment variable (prefer not to store keys in the file) |
| `temperature`, `max_tokens`, `reasoning_effort` | passed through as is |
| `headers` | extra HTTP headers |
| `extra` | arbitrary fields merged into the request body (`response_format`, `top_p`...) |
| `conversation` | reuse the `conversation_id` returned by a proxy: the system prompt is sent once per chat |
| `max_chat_chars` | 45000 — a new chat starts after that |
| `max_chats` | chat limit for the whole run (for proxies with session limits) |

A Copilot proxy:

```toml
[providers.copilot]
type = "copilot"           # base_url http://127.0.0.1:8000/v1, conversation = true
model = "copilot"
min_interval = 3
max_chats = 300
```

## Gemini (native API)

```toml
[providers.gemini]
type = "gemini"            # GEMINI_API_KEY or GOOGLE_API_KEY
models = ["gemini-flash-latest", "gemini-flash-lite-latest"]
rpm = 10
temperature = 0.2
safety_off = true          # BLOCK_NONE — game dialogue often contains violence
# generation = { maxOutputTokens = 8192 }
```

Replies are requested in JSON mode. On the free tier, set `rpm` and a `models` chain.

## Command-line tools

Any program that reads a prompt and prints the answer.

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

| Option | Description |
|---|---|
| `command` | arguments; `{model}`, `{output_file}` (the reply is read from that file), `{prompt}` |
| `stdin` | `true` — the prompt is sent on stdin (recommended: Windows command lines are limited to 32k characters) |
| `env` | extra environment variables |

The CLI provider is stateless: the system prompt is included in every request.

## Checking

```bash
gameloc test --provider gemini
gameloc stats
```

## Custom providers

See [extending](extending.md#custom-provider).
