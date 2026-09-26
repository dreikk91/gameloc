# Three-pass proofreading

**English** | [Українська](../uk/proofreading.md)

Proofreading runs as three independent passes, each with its own prompt and answer validation:

| Stage | What the model does | Decision |
|---|---|---|
| `meaning` | compares the translation with the source: omissions, additions, negations, numbers, participants, idioms, jokes | `ok` / `issue` / `needs_review` + note |
| `edit` | polishes the language using the `meaning` note: calques, syntax, gender, forms of address, glossary | `keep` / `change` (+ new text) / `needs_review` |
| `verify` | independently checks the proposed text against the source, the previous version and the notes | `accept` / `reject` / `needs_review` |

Lines marked `needs_review` do not move to the next stage. Only lines that receive `accept` reach the game.

## Quick path

```bash
gameloc proofread prepare               # snapshot of all translated lines
gameloc proofread run                   # meaning → edit → verify, stages advance automatically
gameloc proofread export                # accepted → checkpoint with status "proofread"
gameloc export                          # into the game files
```

`prepare` accepts `--scene` (review chapter by chapter) and `--include-proofread` (review already reviewed lines again).
Without an explicit path it creates `gameloc_work/proofread/<date-time>`; other commands use the latest run.

Proofreading may use a stronger model than translation:

```toml
[proofread]
provider = "claude"
max_chars = 12000
workers = 4
```

## How it works

```
gameloc_work/proofread/20260924-101500/
  snapshot.json               immutable snapshot: texts, drafts, stage prompts
  meaning/packets.jsonl       packets (batch_id = content hash)
  meaning/responses/<id>.json validated answers (with the prompt hash)
  meaning/invalid/            rejected answers with the reason
  edit/ ...  verify/ ...
  review.json                 after export: everything that needs a human
```

- Every answer is validated: same `batch_id` and stage, every id exactly once, an allowed decision,
  a non-empty note; for `change` also tag markers and full text validation. A rejected answer goes back
  to the model with the error description (`attempts`).
- Answers are bound to the prompt hash: if a packet changed, an old answer is never silently reused.
- `export` skips lines whose draft or source changed after the snapshot — they go to `review.json`.
- A run can be interrupted at any time: `run` only answers packets that have no answer yet.

## Manual mode (any chat UI)

When no API is available, or you want to review a critical scene yourself or in a web chat:

```bash
gameloc proofread next meaning            # writes next-<batch>.txt with the full prompt
# paste it into a chat, save the reply as answer.json
gameloc proofread submit meaning <batch> answer.json --reviewer "Olena"
gameloc proofread advance edit            # once meaning is complete
gameloc proofread status
```

Manual and automatic answers can be mixed within one run.

## Resolving the rest

`export` leaves lines that stages could not settle (verify `reject`, `needs_review`, a failed check) in `review.json`.
`resolve` is a fourth stage for them: the model sees the sources, the current text, the rejected proposal, the reason
and every reviewer note, and writes the final text (`final` + translation, validated like `change`) or `needs_review`.

```bash
gameloc proofread export                 # writes review.json
gameloc proofread resolve                # answers resolve packets, then exports again
gameloc proofread next resolve           # or by hand / with an outside agent: next + submit
```

Edit `review.json` before `resolve` to drop lines a human should handle. Lines whose draft changed since
the snapshot are skipped.

## After proofreading

`review.json` lists, for every line that did not pass, the reason, the source, the draft, the proposal and the notes of all stages.
A convenient loop for a human editor:

```bash
gameloc sheet-export review.csv --scene chapter1
# the translator edits the "translation" column
gameloc sheet-import review.csv           # validation + status "manual"
```

`gameloc audit` checks the whole checkpoint (tags, scripts, Latin words, length, forbidden glossary variants);
`audit --terms` also reports glossary terms of the source whose translation (by word start, so inflected forms
count) is missing; `audit --requeue` sends problematic lines back for translation.

To look a line up: `gameloc show <id> --context 3` prints it with its neighbours, sources and translations;
`gameloc grep 'regex'` searches sources and translations.
