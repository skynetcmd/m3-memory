# SLM Intent Classifier & Intent-Aware Retrieval

> **Related**: [DUAL_EMBED.md](DUAL_EMBED.md) shows how an SLM profile
> built on top of `extract_text()` can power dual-embedding ingest for
> max-kind retrieval fusion. `extract_text()` is documented in §5 below.

## 1. Overview

`bin/slm_intent.py` is a named-profile classifier that wraps a small local (or
remote) LLM and maps a query to a label from a fixed set. Its output is the
`intent_hint` parameter that `memory_core.memory_search_scored_impl` consults
when deciding whether to apply extra retrieval tricks like a role-biased score
boost or predecessor-turn pull.

The whole stack is **dormant by default** — three independent env gates keep
it off, and with nothing enabled the retrieval path is byte-identical to a
pre-refactor run. No caller inside the repo wires any of this on today; it's
there for bench harnesses, experiments, and future MCP integrations to opt
into.

```
┌──────────────────────────────────────────────┐
│  Caller (bench harness, future MCP wrapper)  │
└───────────────────┬──────────────────────────┘
                    │ classify_intent(query, profile="...")
                    ▼
        ┌──────────────────────────┐
        │ bin/slm_intent.py        │
        │  - load profile YAML     │
        │  - POST /v1/chat/comp    │
        │  - match label list      │
        └───────────────┬──────────┘
                        │ returns label str (or None when gate off)
                        ▼
        ┌──────────────────────────────┐
        │ memory_search_scored_impl    │
        │   (intent_hint=<label>)      │
        │  - shift vector_weight?      │
        │  - role boost on user turns? │
        │  - pull predecessor turns?   │
        └──────────────────────────────┘
```

Profiles live in YAML, one file per named profile. The module ships four
starter profiles under `config/slm/`; bench harnesses add their own by
setting `M3_SLM_PROFILES_DIR` to a co-located directory.

## 2. Gates

Three env-var gates, each independent. All default **off**.

| Gate | Controls |
|---|---|
| `M3_SLM_CLASSIFIER` | The SLM itself. When off, `classify_intent()`, `extract_entities()`, and `extract_text()` return `None` immediately with no HTTP call. |
| `M3_INTENT_ROUTING` | The retrieval-side consumer. When off, `memory_search_scored_impl` silently ignores any `intent_hint` passed in. |
| `M3_QUERY_TYPE_ROUTING` | A narrower heuristic that shifts `vector_weight` toward BM25 for temporal proper-noun queries. Pre-existing; `intent_hint` piggybacks on the same weight-shift logic. |

Set to `1`, `true`, or `yes` (case-insensitive) to enable.

### Three useful combinations

1. **Heuristic routing only** (no SLM, zero extra latency)
   ```bash
   export M3_QUERY_TYPE_ROUTING=1
   ```
   The `when/what date/... <ProperNoun>` heuristic runs; nothing else changes.

2. **Intent routing without SLM**
   ```bash
   export M3_INTENT_ROUTING=1
   ```
   Role-boost and predecessor-pull activate *if* a caller passes an
   `intent_hint` kwarg. No SLM call happens. Useful when your caller already
   knows the intent (e.g. a bench harness that reads the question type from
   ground-truth metadata).

3. **Full intent routing with SLM classification**
   ```bash
   export M3_SLM_CLASSIFIER=1
   export M3_INTENT_ROUTING=1
   ```
   Caller calls `classify_intent(query)` → gets a label → passes it as
   `intent_hint`. All three pieces (weight shift, role boost, predecessor
   pull) activate.

### Additional tunable (not a gate)

| Env | Default | Role |
|---|---|---|
| `M3_INTENT_USER_FACT_BOOST` | `0.1` | Additive score boost for user-authored turns when `intent_hint == "user-fact"`. |
| `M3_SLM_PROFILE` | `default` | Profile name used when caller doesn't pass `profile=`. |
| `M3_SLM_PROFILES_DIR` | — | `os.pathsep`-separated list of dirs searched **before** `config/slm/`. Bench harnesses use this to stack their own profiles. |

## 3. Profile YAML format

Every profile is a single YAML file. Filename stem is the profile name
(`default.yaml` → `profile="default"`). Fields:

```yaml
# REQUIRED ──────────────────────────────────────────────────────────────
url: http://127.0.0.1:11434/v1/chat/completions   # any OpenAI-compatible endpoint
model: qwen2.5:1.5b-instruct                       # whatever ID that endpoint accepts
system: |                                          # system prompt, multi-line OK
  You classify a query into exactly one category.
  Reply with ONLY the category name.
  ...
labels:                                            # at least one — valid outputs
  - user-fact
  - temporal-reasoning
  - multi-session
  - general

# OPTIONAL ──────────────────────────────────────────────────────────────
fallback: general                # returned when model output matches no label;
                                 # must be one of `labels`; defaults to labels[0]
temperature: 0                   # passed through to the chat completion
timeout_s: 5.0                   # httpx timeout; applied to connect+read
api_key_service: LM_API_TOKEN    # keyring service name resolved via auth_utils;
                                 # omit or set null for auth-less endpoints

# OPTIONAL — wire-format backend (default: openai)
backend: openai                  # "openai" = /v1/chat/completions body,
                                 #            reads choices[0].message.content
                                 #            (Ollama, LM Studio default, llama-server,
                                 #             vLLM, OpenAI itself)
                                 # "anthropic" = /v1/messages body with top-level
                                 #            system field, reads content[0].text
                                 #            (Anthropic cloud; LM Studio 0.3+
                                 #             serves this locally too)
cache_system: true               # [anthropic only] wrap system prompt in a
                                 # cache_control ephemeral block so repeated calls
                                 # pay for the system prompt once (90% discount
                                 # on cached reads, billed at ~$0.10/M on Haiku)
anthropic_version: "2023-06-01"  # [anthropic only] API version header

# OPTIONAL — post-processing (applied by extract_text / extract_entities,
# NOT by classify_intent since label-picking handles prose cleanup inline).
# All three fields are independent and any/all may be omitted.
post:
  skip_if_matches:               # regex patterns; if ANY matches the raw
    - '^[-_\.\s]*$'              #   reply (case-insensitive, search), treat
    - 'no (extractable )?facts'  #   the output as empty so callers fall back
  strip_prefixes:                # regex patterns; stripped from the START of
    - '^sure[,.]?\s*'            #   the reply in the order declared, then
    - '^here are (the )?facts?\s*:?\s*'  # repeated until none match (so
                                 #   stacked prefixes like "Sure. Here are..."
                                 #   both peel off)
  format: "[FACTS] {text}"       # optional wrapper around the cleaned text;
                                 # MUST contain the literal "{text}" placeholder
```

### Nothing is hardcoded

The four shipped profiles all default to a local Ollama + qwen2.5:1.5b-instruct
setup because that's the lightest common case. **The code reads whatever you
put in the YAML.** Any server speaking `/v1/chat/completions` works:

- Ollama: `http://127.0.0.1:11434/v1/chat/completions`
- LM Studio: `http://127.0.0.1:1234/v1/chat/completions`
- llama-server: `http://127.0.0.1:8080/v1/chat/completions`
- vLLM: `http://your-host:8000/v1/chat/completions`
- OpenAI: `https://api.openai.com/v1/chat/completions` (with `api_key_service: OPENAI_API_KEY`)
- Groq / Together / any OpenAI-compatible gateway

Change the three fields (`url`, `model`, `api_key_service`), keep everything
else.

### Cloud backends are opt-in

m3-memory ships local-first: every profile in `config/slm/` that doesn't
name a cloud host uses the OpenAI-compatible wire format against a
`127.0.0.1` URL. No SLM call reaches the public internet unless **you**
pick a profile that points there.

To use a cloud backend:

1. **Pick or write a profile** whose `url` points at the provider and
   whose `api_key_service` names the keyring entry holding your key.
   The shipped example is `config/slm/contextual_keys_haiku.yaml`, which
   uses Anthropic Haiku with prompt caching.
2. **Set the key**: `python bin/setup_secret.py <SERVICE>` (for
   Anthropic, `ANTHROPIC_API_KEY`).
3. **Invoke that profile explicitly**: every caller that loads SLM
   profiles takes an explicit name (`--contextual-keys-profile
   contextual_keys_haiku` on the bench, `profile=` kwarg in code). No
   profile loads cloud automatically — the default-named profiles
   (`default`, `memory`, `contextual_keys`, …) all point at local
   servers.

If a profile has `backend: anthropic` but the user hasn't set the API
key, the first request returns an HTTP error and the caller falls back
per its own contract (`extract_text` returns `""`, the bench's enricher
falls back to raw content). No silent cloud calls, no billing surprises.

Switching back to local is a one-line change (`--contextual-keys-profile
contextual_keys` or edit the profile's `url` and `backend` fields).

### Label synchronization

The `labels` list is the classifier's output space. When the label set is
consumed by `memory_core`'s intent-routing logic (role boost, predecessor
pull, BM25 weight shift), the specific strings matter:

| Label | Effect in `memory_search_scored_impl` (with `M3_INTENT_ROUTING=1`) |
|---|---|
| `user-fact` | Role boost on user turns + predecessor pull |
| `temporal-reasoning` | Shift `vector_weight` to 0.3 |
| `multi-session` | Shift `vector_weight` to 0.3 |
| anything else | No effect (the hint is carried for logging but not acted on) |

Using different label names in a profile (e.g. renaming `user-fact` to
`personal-fact`) means memory_core won't recognize the label and the routing
won't fire. That's fine for profiles whose labels feed a different consumer
(e.g. `chatlog.yaml` uses `sensitive / administrative / routine` for a
different purpose). For profiles intended to drive memory retrieval, keep
the canonical four labels.

## 4. Profile discovery order

When you call `classify_intent(query, profile="memory")`, the loader walks
these directories in order and uses the first `memory.yaml` it finds:

1. Each path in `M3_SLM_PROFILES_DIR` (separator: `;` on Windows, `:` elsewhere)
2. `<repo>/config/slm/`

Missing profiles return `None` with a warning; they never raise. Malformed
YAML raises `ValueError` at load time — that's a deploy error.

### Example: bench harness with its own profile

```bash
# benchmarks/longmemeval/slm/bench.yaml exists with bench-specific prompt
export M3_SLM_PROFILES_DIR=benchmarks/longmemeval/slm
export M3_SLM_CLASSIFIER=1
python benchmarks/longmemeval/bench_longmemeval.py ...
# classify_intent(query, profile="bench") loads benchmarks/.../bench.yaml
# classify_intent(query, profile="default") loads config/slm/default.yaml
# (repo default is still reachable as the second search dir)
```

### Example: stacked dirs (local overrides without editing shipped files)

```bash
# ~/.config/m3-memory/slm/memory.yaml has my local prompt tweaks
export M3_SLM_PROFILES_DIR="$HOME/.config/m3-memory/slm:$PWD/benchmarks/longmemeval/slm"
# memory.yaml resolves from $HOME/.config (wins), bench.yaml from benchmarks
```

## 5. Shipped profiles

Five files under `config/slm/`:

| Profile | Purpose | Label set | Caller today |
|---|---|---|---|
| `default.yaml` | Fallback when no `profile=` and no `M3_SLM_PROFILE` | `user-fact / temporal-reasoning / multi-session / general` | `classify_intent()` with no args |
| `memory.yaml` | Pin-to-this for production memory retrieval | Same as default | Future MCP `intent_hint` wiring |
| `chatlog.yaml` | Chatlog turn sensitivity triage | `sensitive / administrative / routine` | Reserved for future chatlog_core hook |
| `entity_extract.yaml` | Free-text entity extractor (not label-based) | placeholder `extracted` | `bin/augment_memory.py enrich-titles` via `extract_entities()` |
| `contextual_keys.yaml` | Atomic-fact extraction for ingest-time embed-key enrichment, local LLM | placeholder `extracted` | `benchmarks/longmemeval/bench_longmemeval.py --contextual-keys` via `extract_text()` |
| `contextual_keys_haiku.yaml` | Same as above, via Anthropic Haiku cloud (prompt caching enabled) | placeholder `extracted` | `bench_longmemeval.py --contextual-keys --contextual-keys-profile contextual_keys_haiku` |

Free-text profiles (`entity_extract.yaml`, `contextual_keys.yaml`) use the
`system` prompt + the endpoint but ignore the `labels` field for output.
They carry a single placeholder label (`extracted`) only because the
profile schema requires `labels` to be a non-empty list.

Every file starts with a `!! EDIT BEFORE ENABLING THE GATE !!` comment block
listing what to change for your environment.

### Choosing the right extractor function

| Function | Output | Splitting? | Post-processing? | Typical profile |
|---|---|---|---|---|
| `classify_intent(query, profile=...)` | One label from `profile.labels` | n/a (label picker handles it) | No — label picking is its own cleanup | `default`, `memory`, `chatlog` |
| `extract_entities(text, profile=...)` | `list[str]` (comma/newline-split, ≤60 char filter) | Yes | Yes, before splitting | `entity_extract` |
| `extract_text(text, profile=...)` | `str` (raw model reply, verbatim after post-processing) | No | Yes | `contextual_keys` |

The `post:` block in the profile YAML is consumed only by `extract_entities`
and `extract_text`. It's applied to the raw reply BEFORE any splitting so
that preamble-strip rules see the whole response.

## 6. End-to-end walkthroughs

### Scenario A: Local Ollama, default profile

```bash
# Assumes: ollama running, qwen2.5:1.5b-instruct pulled
#   $ ollama pull qwen2.5:1.5b-instruct
#   $ ollama serve

# Nothing to edit — config/slm/default.yaml already points here.
export M3_SLM_CLASSIFIER=1
export M3_INTENT_ROUTING=1

python bin/slm_intent.py    # prints resolved profiles + search dirs

# Now a caller that imports slm_intent and memory_core passes intent_hint:
python -c "
import asyncio, sys; sys.path.insert(0, 'bin')
from slm_intent import classify_intent
from memory_core import memory_search_impl

async def run():
    intent = await classify_intent('when did I adopt Sparky?')
    print('intent:', intent)
    # With M3_INTENT_ROUTING=1 the hint activates role boost + predecessor pull
    results = await memory_search_impl(
        'when did I adopt Sparky?', k=5, intent_hint=intent
    )
    print(results)

asyncio.run(run())
"
```

### Scenario B: LM Studio + a different model

Edit `config/slm/memory.yaml`:

```yaml
url: http://127.0.0.1:1234/v1/chat/completions    # LM Studio default
model: llama-3.2-3b-instruct                       # whatever's loaded
api_key_service: LM_STUDIO_API_KEY                 # keyring entry name
```

Then:

```bash
export M3_SLM_CLASSIFIER=1
export M3_INTENT_ROUTING=1
export M3_SLM_PROFILE=memory    # use the edited file, not default.yaml
```

### Scenario C: OpenAI

Store the key first:

```bash
python bin/setup_secret.py      # interactive; stores OPENAI_API_KEY in keyring
```

Add a profile `config/slm/openai.yaml`:

```yaml
url: https://api.openai.com/v1/chat/completions
model: gpt-4o-mini
api_key_service: OPENAI_API_KEY
timeout_s: 15.0
temperature: 0
system: |
  You classify a memory-retrieval query...
labels: [user-fact, temporal-reasoning, multi-session, general]
fallback: general
```

```bash
export M3_SLM_CLASSIFIER=1
export M3_INTENT_ROUTING=1
export M3_SLM_PROFILE=openai
```

### Scenario D: Bench harness with a co-located profile

The bench harness ships its own profile next to itself so the harness
repo-subtree is portable. See the `MAIN_PORT_NOTES.md` breadcrumb on
`bench-wip` for the reference implementation. In short:

```bash
# benchmarks/longmemeval/slm/bench.yaml — bench-specific prompt + labels
export M3_SLM_PROFILES_DIR=benchmarks/longmemeval/slm
export M3_SLM_CLASSIFIER=1
python benchmarks/longmemeval/bench_longmemeval.py ...
```

Inside the harness:

```python
from slm_intent import classify_intent
label = await classify_intent(question, profile="bench")
```

## 7. Observability

### Self-test

```bash
python bin/slm_intent.py
```

Prints the current gate state, resolved default profile name, all search
directories with existence markers, and every `.yaml` discoverable by name.
Cheap sanity check — does **not** make any HTTP calls.

### Verifying the gate is actually on

```python
import os; os.environ['M3_SLM_CLASSIFIER'] = '1'
import asyncio, sys; sys.path.insert(0, 'bin')
from slm_intent import classify_intent
print(asyncio.run(classify_intent('when did this happen?')))
# Returns a label string on success, None on any failure.
```

If you get `None` unexpectedly, check:
1. Is the gate env var set in *this* process? (Spawned subprocesses don't inherit unless you export.)
2. Is the profile YAML discoverable? `list_profiles()` tells you.
3. Is the endpoint reachable? Curl it manually first.
4. Watch stderr: the module logs `WARNING: SLM classify via profile=... failed: ...` on HTTP errors.

### Test coverage

`tests/test_slm_intent.py` — 11 cases covering gate-off → None, profile
loader, search-dir stacking, malformed YAML handling, label matching (exact
and substring), and fallback. Run with:

```bash
python -m pytest tests/test_slm_intent.py -v
```

## 8. Operational notes

### Latency budget

An SLM call per search adds a round-trip to whatever endpoint you configured.
Local qwen2.5:1.5b takes ~50-200 ms on commodity hardware; remote OpenAI
takes 300-800 ms. Keep `timeout_s` tight in the profile (5-10 s is
appropriate for a classifier — if it's slower than that, the search should
proceed without the hint rather than block).

The module passes the timeout to every `httpx.AsyncClient.post` call and
raises `TimeoutError` cleanly → `classify_intent` returns `None` → caller
falls through to unhinted retrieval. Worst case: you lose the hint, not the
search.

### Callers sharing an httpx client

For bench runs classifying hundreds of queries, inject a shared client to
avoid per-call connection setup:

```python
import httpx
from slm_intent import classify_intent, load_profile

prof = load_profile("bench")
async with httpx.AsyncClient(timeout=prof.timeout_s) as client:
    for q in questions:
        intent = await classify_intent(q, profile="bench", client=client)
        ...
```

### Profile cache invalidation

Profiles are cached by name after the first `load_profile()` call. Edits to
the YAML are picked up only after:

```python
from slm_intent import invalidate_cache
invalidate_cache()
```

Or just restart the process.

## 9. Related docs

- [`docs/CHATLOG.md`](CHATLOG.md) — how the chatlog subsystem works today; a future integration could call `classify_intent(profile="chatlog")` on each inbound turn.
- [`docs/CLI_REFERENCE.md`](CLI_REFERENCE.md) — `bin/augment_memory.py` uses `extract_entities` via the `entity_extract` profile.
- [`docs/CHANGELOG_2026.md`](CHANGELOG_2026.md) — the April 21 entry documents the landing of this subsystem.
- `bin/slm_intent.py` — the module docstring repeats the profile-format reference and adds a `_selftest()` for operators.
