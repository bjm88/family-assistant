# Family-assistant fine-tuning pipeline

This directory automates **fine-tuning a small Gemma base model with
LoRA** so the "fast tier" (`AI_OLLAMA_FAST_MODEL`) understands our
schema, API, and Avi's voice well enough to handle routine family
Q&A without escalating to the heavyweight model.

Running the full pipeline produces a new Ollama-ready custom model
(e.g. `family-fast:2026-04-23`) and wires it into the live app via
`.env` so every chat turn that doesn't need the heavy agent is
answered by the custom model in 1–3 s instead of 10–20 s.

## Why fine-tune instead of just RAG?

Both techniques encode knowledge for the model, but they do very
different things:

| Approach | What it gives the model | Good for | Bad for |
|---|---|---|---|
| **RAG** (already shipped in `ai/rag.py`) | Live facts injected into every prompt | Today's calendar, today's goals, who's in the house | Token cost, prompt latency, long-lived patterns |
| **Fine-tune** (this pipeline) | Persistent patterns burned into the weights | Schema shape, SQL idioms, API names, Avi's tone | Anything that changes between sessions |

We use BOTH:

- Fine-tune on **structure** (table/column names, SQL templates, API
  names, integration names like Gmail/Twilio/Telegram/Google Calendar,
  persona) so the model "just knows" how the app is shaped.
- RAG on **state** (actual family members, goals, appointments)
  because these change daily.

**No vector DB is needed for training.** LoRA ingests JSONL on disk.
We deliberately keep runtime RAG simple too — no vector DB, just the
existing structured queries — and revisit only if we later need
codebase-retrieval (e.g. "which tool do I call to…").

## Curating gold examples

Before the first training run (and every time you want to teach the
model a new behaviour), edit:

```
scripts/ai_training/templates/labeled_examples.jsonl
```

This is the ONE file you hand-curate. Every row is treated as
authoritative — the dataset builder caps it at 2000 rows
(vs. 300–3000 for synthetic buckets) and accepts your phrasing
verbatim. The schema is documented in the file's header comment;
the short version:

```jsonc
// The fast model OWNS three answer paths and defers everything else:
//   FAMILY         = answer directly from RAG (household state)
//   GEMINI_WEB_API = defer to the web-search/Gemini grounded path
//                    (time, weather, math, stocks, news, world facts)
//   CALENDAR_API   = handle calendar reads + simple writes directly
//                    (free/busy, summarise a day, add/move a hold)
//   HEAVY          = hand off to the full agent (tools, attachments,
//                    monitoring jobs, external-coordination scheduling)
{"user": "what's sara's top goal?",          "decision": "FAMILY",         "response": "Her urgent goal is the half-marathon training plan.",            "category": "recall"}
{"user": "what's my full social security",   "decision": "FAMILY",         "response": "Your SSN on file is 521-43-7896.",                               "category": "sensitive"}
{"user": "what's the stock price of TSLA",   "decision": "GEMINI_WEB_API", "response": "Pulling the current TSLA price from the web.",                   "category": "web_search"}
{"user": "what's on my calendar tomorrow",   "decision": "CALENDAR_API",   "response": "Tomorrow: 9 AM standup, 11 AM dentist, 6:30 PM family dinner.",  "category": "calendar"}
{"user": "block off saturday for soccer",    "decision": "CALENDAR_API",   "response": "Done — Saturday 9 AM to noon is now blocked for soccer.",        "category": "calendar"}
{"user": "email john to remind him lunch",   "decision": "HEAVY",          "response": "I'll have the agent draft and send John a reminder.",            "category": "email"}
{"user": "research summer camps for theo",   "decision": "HEAVY",          "response": "I'll have the agent set up a monitoring task to research camps.","category": "monitor"}
{"user": "schedule juno's vet appointment",  "decision": "HEAVY",          "response": "Agent will book the vet and drop the confirmed time on the cal.","category": "calendar"}
```

**Required fields:** `user`, `decision`. **Response required when**
`decision` is `FAMILY` or `CALENDAR_API` — those teach the model to
produce a real answer/confirmation, so gold output is mandatory.
For `HEAVY` and `GEMINI_WEB_API` rows, `response` is optional — if
omitted, the builder substitutes a class-specific canonical handoff
("Handing this to the full agent." variants, "Let me check the web
for that." variants). Comment lines (`//`) and blank lines are
skipped.

For `CALENDAR_API` rows, write the gold response with **placeholder
times** (e.g. "9 to 10:30 AM", "Done — held next Friday 2 PM ET").
The model is learning the *shape* of the answer; at inference time
the calendar tool supplies the live data. Anything that needs
external coordination on top of the calendar write (booking with a
vet, dentist, contractor) stays `HEAVY`.

### Sensitive identifiers ARE in scope for the fast model

SSNs, passport numbers, passwords, safe combinations, and the like
should be labeled `FAMILY` — that's the whole point of fine-tuning
this tier. Speakers reach `family_qa_router` only after face
recognition (`recognized_person_id` is set), and
`chat_prompts.build_rag_block` already injects the speaker's
sensitive identifiers into the prompt. The agent-level safety
layer (`api/ai/prompts.with_safety()`) handles unrecognised
speakers at a different layer; the labels in this file represent
the happy path.

Use placeholder values in the gold `response` — the model is
learning the *shape* of a sensitive recall, not memorising real
identifiers. At inference time RAG supplies the live values.

To preview your changes without committing to a 30-minute fine-tune:

```bash
cd scripts/ai_training/
uv run python 2_build_sft_dataset.py --inspect
```

The `--inspect` output prints (a) per-bucket counts including the
`labeled` bucket, (b) the FAMILY/HEAVY split inside `labeled`,
(c) the category histogram, and (d) 20 random rows so you can
sanity-check phrasing.

The builder will **refuse to run** if any line in
`labeled_examples.jsonl` is malformed — it'll print the exact
file:line so you can fix it. This is intentional: the whole point
of the file is human review, so silent drops would defeat it.

After fine-tuning, you can use the same file as a regression set —
pipe each row's `user` through `ollama run <new-tag>` and diff
against the gold `response`.

## Pipeline overview

```
              ┌─────────────────────────────────────────────┐
              │  config.yaml  (base model, hyperparams)     │
              └─────────────────────────────────────────────┘
                                  │
        ┌─────────────────────────┼─────────────────────────┐
        ▼                         ▼                         ▼
  1_dump_corpus.py         templates/*             2_build_sft_dataset.py
  (live schema, APIs)      (human-authored Q→A)    (expand → train.jsonl)
        │                         │                         │
        └────────────┬────────────┘                         │
                     ▼                                      │
              artifacts/corpus/                             │
                     │                                      │
                     └──────────────────────────────────────┘
                                      │
                                      ▼
                     artifacts/dataset/{train,valid}.jsonl
                                      │
                                      ▼
                              3_fine_tune.sh
                (mlx_lm.lora → fuse → GGUF Q4_K_M → ollama create)
                                      │
                                      ▼
                     ollama registry: family-fast:<date>
                                      │
                                      ▼
                .env: AI_FAMILY_QA_MODEL=family-fast:latest
                                      │
                                      ▼
                         live chat uses the custom model
```

## One-time setup

Everything is Python 3.12 on `uv` (matching the rest of the repo)
plus one external CLI tool (`mlx_lm`) and one git clone
(`llama.cpp`).

```bash
# 1) MLX-LM — Apple's native fine-tuner
uv pip install --with mlx mlx-lm

# 2) llama.cpp — for the HF → GGUF conversion step. The fine-tune
#    script will clone it for you on first run; no action needed.

# 3) HuggingFace token (optional but recommended — avoids rate limits
#    when the pipeline pulls the base model). Paste into ~/.huggingface/token:
huggingface-cli login

# 4) Ollama is already running on localhost:11434 per the main README.
```

## Running the pipeline

### First-time training

```bash
cd scripts/ai_training/

# 1) Dump live schema + API surface into inspectable markdown/YAML.
uv run python 1_dump_corpus.py

# 2) Expand the corpus + templates into SFT JSONL.
uv run python 2_build_sft_dataset.py

# 3) Fine-tune, fuse, quantise, register with Ollama. 15-45 min on a Mac Studio.
./3_fine_tune.sh

# 4) Point live chat at the new model. The script prints the exact line:
echo 'AI_FAMILY_QA_MODEL=family-fast:latest' >> ../../.env
../restart.sh backend
```

### Re-training when a new Gemma base drops

```bash
# Checks HF for a newer revision of config.base_model. If found, re-runs
# steps 1-3 and tags a fresh :<date> build. --force to retrain anyway
# (useful after you add tables / extend templates).
./4_update_base_model.sh            # no-op when base is up to date
./4_update_base_model.sh --force    # retrain regardless
```

### Optional cron helper

```bash
# Weekly check at 3am Sunday. --cron writes a launchd plist on macOS
# (LaunchAgents) or a systemd timer on Linux. Idempotent; re-run to
# update the schedule.
./4_update_base_model.sh --install-cron
./4_update_base_model.sh --uninstall-cron
```

## Regenerating the training corpus as the app grows

The corpus dump is deterministic — it reads the live schema catalog
and the routers' / integrations' docstrings. Anytime you:

- add a table / column / relationship
- add a router or a tool
- add an integration adapter under `python/api/integrations/`
  (just write a real module docstring — the dump auto-discovers it)
- tweak Avi's persona

…just re-run `1_dump_corpus.py` + `2_build_sft_dataset.py` +
`3_fine_tune.sh`. The pipeline is idempotent; old artifacts are
overwritten and the old Ollama tag is preserved so you can roll
back with one line in `.env`.

When you add a new integration, the dump will pick up the docstring
automatically and the builder will emit a generic
"What is the X integration?" pair. To unlock the richer "What does
the agent use X for?" / "How does the agent do Y?" pairs, add an
entry to `INTEGRATION_PROFILES` near the top of
`2_build_sft_dataset.py` (six lines per integration — see the
existing entries as a template).

## File map

| File | What it does |
|---|---|
| `config.yaml` | Single source of truth for base model, tag names, paths, hyperparams. |
| `1_dump_corpus.py` | Reads `llm_schema_catalog` + walks `python/api/{routers,ai,integrations}/` → writes `artifacts/corpus/{schema.yaml, apis.yaml, tools.yaml, integrations.yaml, prompts.yaml}`. |
| `templates/schema_questions.yaml` | Q→A templates that expand per-column (e.g. "What does X column hold?"). |
| `templates/sql_patterns.yaml` | Q→SQL templates that expand per-table (e.g. "list all X where …"). |
| `templates/labeled_examples.jsonl` | **THE gold curation file.** Hand-authored `{user, decision, response}` rows that supervise BOTH routing and answer quality. See [Curating gold examples](#curating-gold-examples) below. |
| `2_build_sft_dataset.py` | Combines corpus + templates → `artifacts/dataset/{train,valid}.jsonl` in Gemma chat format. |
| `3_fine_tune.sh` | Runs `mlx_lm.lora` → `mlx_lm.fuse` → `llama.cpp/convert_hf_to_gguf.py` → quantise → `ollama create`. |
| `4_update_base_model.sh` | Polls HF for a new revision of `config.base_model`; if newer, re-runs 1–3 and bumps Ollama tag. Also owns `--install-cron`. |
| `artifacts/` | All machine-generated output. Git-ignored. Delete any time. |

## Troubleshooting

### `mlx_lm: no module named mlx_lm`

You're on Linux/Windows, or MLX isn't installed. See one-time setup
above. MLX is Apple-Silicon-only; there's no x86 path.

### `ollama create` says the model is too large

Lower `lora.rank` in `config.yaml` (e.g. to 8) or pick a smaller
base model (`google/gemma-4-E2B`).

### Gemma 4 stop-token / chat-template warnings

Gemma 4 introduced a native `system` role and dropped Gemma 3's
`<end_of_turn>` sentinel. The Modelfile that `3_fine_tune.sh`
emits omits an explicit `PARAMETER stop` so Ollama's bundled
gemma4 chat template handles framing. If you see runaway generations
or the model emitting raw `<|...|>` channel tokens, your Ollama
build predates gemma4 support — `brew upgrade ollama` (or your
distro equivalent) and re-run `./3_fine_tune.sh`.

### Fine-tune loss is flat

Almost always the dataset. `uv run python 2_build_sft_dataset.py
--inspect` prints bucket counts, token-length histograms, and a
random sample of 20 training rows — sanity-check that the examples
look right before you spend 30 minutes on a dud run.

### Custom model answers fine in `ollama run` but the app won't use it

Two env vars gate the routing. Confirm both:

```bash
grep -E 'AI_FAMILY_QA_(SHORTCUT_ENABLED|MODEL)' ../../.env
# AI_FAMILY_QA_SHORTCUT_ENABLED=true
# AI_FAMILY_QA_MODEL=family-fast:latest
```

Then restart the backend. The orchestrator log line
`[orch] path=family_qa_shortcut` appears on every routed turn; if
you see `[orch] path=heavy_agent_with_ack_race` instead, the
classifier is rejecting the turn (either the model is cold or the
question genuinely needs heavy — check the classifier log for the
reason).
