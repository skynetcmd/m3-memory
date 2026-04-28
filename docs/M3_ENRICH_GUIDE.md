# m3-enrich Guide

`m3_enrich` builds **observation memories** from your raw chat history.
It reads conversations from your m3 core memory and chatlog, extracts
atomic user-facts with three-date metadata (when said / when about /
verbatim phrasing), and writes them back as `type='observation'` rows
that are searchable via `mcp__memory__memory_search`.

> **TL;DR**
> ```bash
> python bin/m3_enrich.py --dry-run      # preview
> python bin/m3_enrich.py                 # do it
> ```

> ⚠️ **After enrichment, you MUST do TWO things to actually use the
> results:** (1) set 3 env vars (`M3_PREFER_OBSERVATIONS=1`,
> `M3_TWO_STAGE_OBSERVATIONS=1`, `M3_ENABLE_ENTITY_GRAPH=1`), AND
> (2) **restart your MCP host** (Claude Code: close+reopen the terminal,
> or `/mcp restart memory`). Without the env vars the default
> `memory_search` ignores everything you just built; without the
> restart the env vars never reach the running memory server. Jump to
> [Activate enrichment-aware retrieval](#activate-enrichment-aware-retrieval-required-after-enrichment).
> This trips up nearly every first-time user.

---

## When to use this

You should run `m3_enrich` if:

- Your **chatlog DB has grown large** (5,000+ message rows) and recall
  is getting noisy
- You want **atomic, dated facts** searchable instead of raw chat turns
- You want **supersedes detection** ("user moved from Seattle to Austin")
  to keep older facts demoted but discoverable
- You're running **m3 benchmarks** and want observation-aware retrieval

You should **not** run it if:

- Your chatlog has fewer than ~100 conversations (low signal-to-cost)
- You don't have an SLM available locally **and** don't want cloud spend
- You're trying to fix a bug — observations are a recall layer, not a
  correctness fix

---

## Quick start (LM Studio + qwen3-8b)

This is the recommended path: $0 cost, runs in 10–60 minutes for typical
chatlogs.

### 1. Prereqs

- LM Studio running on `localhost:1234`
- `qwen/qwen3-8b` loaded in LM Studio's Models tab
- `text-embedding-bge-m3` (or another embed model) loaded
- `LM_API_TOKEN=lm-studio` (any string works)

### 2. Preview

```bash
python bin/m3_enrich.py --dry-run
```

You'll see:

```
══════════════════════════════════════════════════════════════
  m3-enrich DRY RUN — no writes will happen
══════════════════════════════════════════════════════════════

  Profile:             enrich_local_qwen
  Model:               qwen/qwen3-8b
  Endpoint:            http://127.0.0.1:1234/v1/messages
  Backend:             anthropic
  Target variant:      m3-observations-20260428
  Type allowlist:      ['message', 'conversation', 'chat_log']

  ── core ─────────────
     path:          .../agent_memory.db
     conversations: 5
     turns total:   30
     est cost:      $0 (local)
     est wall:      ~0.2 min
  ...
```

### 3. Smoke (10 conversations per DB, ~30 sec)

```bash
python bin/m3_enrich.py --limit 10 -y
```

### 4. Full pass

```bash
python bin/m3_enrich.py
```

---

## Profile picker — which YAML to use?

Each profile lives in `config/slm/`. Pass the name (without `.yaml`) via
`--profile`, or a custom YAML via `--profile-path`.

| profile | model | cost | wall | when to use |
|---|---|---|---|---|
| `enrich_local_qwen` | qwen/qwen3-8b @ LM Studio | **$0** | ~3s/conv | Default. Best quality:cost ratio. |
| `enrich_local_gemma` | gemma-4-coder @ LM Studio | **$0** | ~1s/conv | Faster, simpler facts. Less synthesis. |
| `enrich_anthropic_haiku` | claude-haiku-4-5 | ~$3 / 1k conv | ~1.5s | Cloud frontier without LM Studio. |
| `enrich_google_gemini` | gemini-2.5-flash | ~$0.10 / 1k conv | ~2s | Cheapest cloud option. |
| `enrich_openai_gpt` | gpt-4o-mini | ~$0.20 / 1k conv | ~1.5s | OpenAI account holders. |
| `enrich_custom_stub` | (you fill in) | varies | varies | Template for your own endpoint. |

### Examples

```bash
# Cloud frontier
export ANTHROPIC_API_KEY=sk-ant-...
python bin/m3_enrich.py --profile enrich_anthropic_haiku

# Cheapest cloud
export GEMINI_API_KEY=AIza...
python bin/m3_enrich.py --profile enrich_google_gemini

# Custom YAML
python bin/m3_enrich.py --profile-path ~/my-enrich.yaml
```

### Hybrid: different profiles for Observer + Reflector

The Reflector stage (cross-conversation merge/supersede) defaults to the
same profile as Observer, but you can override:

```bash
# Cheap local extraction + frontier reflection
python bin/m3_enrich.py \
    --profile enrich_local_qwen \
    --reflector-profile enrich_anthropic_haiku
```

---

## Scope flags

### Which DB?

| flag | effect |
|---|---|
| (default) | Both core memory + chatlog |
| `--core` | Only core memory (`agent_memory.db`) |
| `--chatlog` | Only chatlog (`agent_chatlog.db`) |
| `--core-db /path/to/db` | Override core DB path |
| `--chatlog-db /path/to/db` | Override chatlog DB path |

### Which row types?

The default allowlist is `message`, `conversation`, `chat_log`. Extend it:

```bash
# Also enrich summaries
python bin/m3_enrich.py --include-summaries

# Also enrich notes
python bin/m3_enrich.py --include-notes

# Custom: add decisions and facts
python bin/m3_enrich.py --include-types decision,fact
```

`type='observation'` is **always skipped** (idempotency — never re-enrich
your own observations).

---

## Pre-flight

`m3_enrich` runs three pre-flight checks before any writes:

1. **Profile smoke** — sends a trivial empty-input prompt to the SLM to
   verify the endpoint is reachable + auth works. Aborts with a clear
   error if not.
2. **DB backup** — copies each target DB to `~/.m3-memory/backups/` with
   a timestamp suffix.
3. **Migration 025** — applies the observation_queue / reflector_queue
   migration if missing (auto-runs the SQL directly; no migrate-script
   dependency).

Skip with `--skip-preflight` for power users.

---

## Reflector

After the Observer pass writes observations, the **Reflector** stage
runs over groups whose observation count meets `--reflector-threshold`
(default 50). It detects merge candidates ("two observations of the
same fact at the same time") and supersedes ("user moved" overrides
"user lives in"), writing supersedes edges into `memory_relationships`.

```bash
# Skip the Reflector pass entirely
python bin/m3_enrich.py --no-reflect

# Lower the threshold — more aggressive reflection
python bin/m3_enrich.py --reflector-threshold 20
```

---

## Retrieving observations

Observations are written under variant `m3-observations-YYYYMMDD` (or
whatever you pass with `--target-variant`).

**Important:** the default `memory_search` does NOT rank observations
above raw turns unless you set the activation env vars. See the
[Activate enrichment-aware retrieval](#activate-enrichment-aware-retrieval-required-after-enrichment)
section below for the required env vars (`M3_PREFER_OBSERVATIONS`,
`M3_TWO_STAGE_OBSERVATIONS`, `M3_ENABLE_ENTITY_GRAPH`) and where to set
them per deployment shape (MCP / shell / cron).

In the bench harness, the variant flag does the activation for you:

```bash
python benchmarks/longmemeval/bench_longmemeval.py \
    --observer-variant m3-observations-20260428
```

---

## Troubleshooting

### "ERROR: profile smoke failed"

Your endpoint isn't reachable. Check:
- `lms server status` (LM Studio CLI) — server up?
- `curl http://127.0.0.1:1234/v1/models` — does it respond?
- Profile YAML's `url` field matches your server

### "ERROR: env var X is empty"

Set the env var the profile expects:
- LM Studio: `export LM_API_TOKEN=lm-studio`
- Anthropic: `export ANTHROPIC_API_KEY=sk-ant-...`
- OpenAI: `export OPENAI_API_KEY=sk-...`
- Gemini: `export GEMINI_API_KEY=AIza...`

### "0 observations written, all empty"

The Observer correctly determined no extractable user-facts in your
sample. Common causes:
- All messages are short acknowledgments, status pings, etc.
- The conversation contains pasted code/articles only
- The `--limit N` picked single-row "groups" — try without `--limit`

### Long conversations time out or return empty

Conversations with 1,000+ turns get **chunked automatically** — split
into pieces that fit the SLM's input budget. If you're still seeing
timeouts:
- Bump `--concurrency` down (default 4) to reduce server load
- Check the YAML's `max_tokens` — reasoning models like qwen3-8b need
  4096+ to leave room for the answer after internal thinking

### "no such table: chroma_sync_queue"

Auto-fixed by `m3_enrich`'s pre-flight — it lazy-creates the table on
chatlog DBs that lack it. If you see this error, you're on an old
version of the script; pull latest.

---

## Writing a custom profile

Copy `config/slm/enrich_custom_stub.yaml` as a template:

```yaml
url: https://your-endpoint/v1/chat/completions
model: your-model-id
api_key_service: YOUR_API_KEY_ENV
backend: openai     # or "anthropic"
temperature: 0
timeout_s: 60.0
max_tokens: 1024
input_max_chars: 6000
labels:
  - observed
fallback: observed
post:
  format: "{text}"
system: |
  (copy the system prompt verbatim from enrich_local_qwen.yaml unless
   you have a specific reason to customize)
```

Use it via `--profile-path /path/to/your.yaml`.

---

## Cost estimation reference

Observer call: ~700 input + 400 output tokens.

| model | input rate | output rate | per call | per 1k conv |
|---|---|---|---|---|
| qwen/qwen3-8b @ LM Studio | $0 | $0 | $0 | $0 |
| gemma-4-coder @ LM Studio | $0 | $0 | $0 | $0 |
| gemini-2.5-flash | $0.075/M | $0.30/M | $0.0001 | $0.10 |
| gpt-4o-mini | $0.15/M | $0.60/M | $0.0002 | $0.20 |
| claude-haiku-4-5 | $1/M | $5/M | $0.003 | $3 |
| claude-sonnet-4-6 | $3/M | $15/M | $0.008 | $8 |

Add ~30% if Reflector fires on >5% of conversations.

---

## Related tools

- `bin/m3_chatlog_backfill_embed.py` — fix unembedded chat_log rows
  (vector search invisibility). Run **before** `m3_enrich` for best
  retrieval results.
- `bin/m3_chatlog_backfill_title.py` — fix useless titles ('user',
  'assistant', NULL) for FTS keyword search.
- `bin/run_observer.py` — lower-level Observer drainer (variant + queue
  modes). `m3_enrich` is the user-friendly wrapper around it.
- `bin/run_reflector.py` — lower-level Reflector drainer.

---

## Core memory enrichment (one-shot bulk)

Same Observer pipeline, pointed at your core memory DB instead of (or in
addition to) the chatlog DB. Useful when you want observations distilled
from `type='message'` / `'conversation'` rows that ended up in your main
memory store — for example, MCP-tool conversations captured via
`conversation_append`, or chats saved manually.

### What gets enriched

The Observer is built to extract user-facts from chat-style turns. By
default, only these row types feed into core enrichment:

  - `message`
  - `conversation`
  - `chat_log`

Curated content (`note`, `decision`, `knowledge`, `fact`, `summary`) is
*excluded* by default — those rows are already facts, not raw dialogue,
and feeding them to the Observer either yields empty output (best case)
or hallucinates "user said X" attributions (worst case). If you have a
specific reason to enrich them anyway, opt in explicitly with
`--include-summaries`, `--include-notes`, or `--include-types`.

### Recommended invocation

```bash
python bin/m3_enrich.py --core \
    --source-variant __none__ \
    --target-variant m3-observations-core-$(date +%Y%m%d)
```

Flag-by-flag:

| Flag | Purpose |
|---|---|
| `--core` | Run only on the core DB (skip chatlog) |
| `--source-variant __none__` | Filter to true core rows (`variant IS NULL`); skips any bench/test variants you may have ingested |
| `--target-variant m3-observations-core-YYYYMMDD` | Tag the produced observations so you can search them by date |

**Why `--source-variant __none__` matters:** if you've ever run a
benchmark or test that wrote `variant='something'` rows into your core
DB, the Observer will happily process them too unless you filter. The
`__none__` sentinel keeps enrichment scoped to the rows that represent
your actual memory.

### Smoke first, full pass second

```bash
# Preview: count groups + estimate wall time, no writes
python bin/m3_enrich.py --core --source-variant __none__ --dry-run

# Smoke: enrich 5 of the biggest groups, see if anything comes out
python bin/m3_enrich.py --core --source-variant __none__ --limit 5 --skip-preflight --yes

# Full pass: drop --limit
python bin/m3_enrich.py --core --source-variant __none__ \
    --target-variant m3-observations-core-$(date +%Y%m%d) \
    --concurrency 4 --skip-preflight --yes
```

Expect most single-row groups to return empty (no extractable user-fact)
— that is correct behavior, not a bug. Curated notes don't have user
dialogue to extract from. The Observer prompt is conservative; it will
return `{"observations": []}` rather than fabricate.

### Concurrency with chatlog enrichment

If you run a long chatlog enrichment (e.g. `--drain-queue` on a backlog)
on the same LM Studio host, load a *second* qwen3-8b instance
(`qwen/qwen3-8b:2`) and use a paired profile so the two passes don't
queue against each other. See `config/slm/enrich_local_qwen_v2.yaml` for
the pattern: identical to `enrich_local_qwen.yaml` except the `model`
field points at the `:2` instance. Then run with `--profile
enrich_local_qwen_v2` for the core pass.

## Entity-graph enrichment (`bin/m3_entities.py`)

Sister tool to `m3_enrich`. Runs the entity extractor (NOT the
Observer) over your memory rows to build the `entities`,
`memory_item_entities`, and `entity_relationships` tables. Lets
retrieval traverse a knowledge graph alongside vector + FTS hits.

### What gets extracted

A typed entity per named thing (host, file_path, function, model,
variant, env_var, ip_address, port, memory_id, ...) and typed
relationships between them (runs_on, defined_in, references,
measured_on, supersedes, ...).

The default vocabulary lives at
`config/lists/entity_graph_m3.yaml` — 33 entity types and 22
predicates derived from the m3-memory corpus. Override per-call with
`--entity-vocab-yaml /path/to/your.yaml` if your domain differs.

### When to use it

- After bulk-ingesting curated knowledge that mentions infrastructure,
  files, or models repeatedly.
- After completing a major decision-tracking session — each "we chose
  X over Y because memory `abc12345`" line becomes a `references` edge.
- Periodically alongside chatlog enrichment (this driver is one-shot,
  not a daemon — re-running picks up new rows via the
  skip-already-extracted heuristic).

Default behavior **skips rows that already have entity links**, so
re-running incrementally enriches only new/changed content.

### Recommended invocation

```bash
python bin/m3_entities.py --core \
    --source-variant __none__ \
    --concurrency 4
```

Smoke first:

```bash
python bin/m3_entities.py --core --source-variant __none__ \
    --limit 10 --concurrency 2 --skip-preflight --yes
```

### Inspecting the graph

```sql
-- Top-cited entities
SELECT canonical_name, entity_type, COUNT(*) AS n_links
FROM entities e JOIN memory_item_entities mie ON mie.entity_id=e.id
GROUP BY e.id ORDER BY n_links DESC LIMIT 30;

-- Relationship inventory
SELECT predicate, COUNT(*) FROM entity_relationships
GROUP BY predicate ORDER BY COUNT(*) DESC;

-- Cross-memory references
SELECT er.from_entity, er.to_entity
FROM entity_relationships er WHERE er.predicate='references';
```

### Profile

`config/slm/entities_local_qwen.yaml` ships pointed at
`qwen/qwen3-8b:2` (the second LM Studio instance, see core enrichment
section above). Same concurrency-with-Observer pattern applies — load
both qwen instances if you want core observations and core entities
running simultaneously.

### Limitations

- One-shot, no daemon mode. Re-run periodically.
- The extractor is conservative on JSON robustness (caps at 25 entities
  + 25 relationships per row). Long entries may yield only the
  highest-signal subset.
- Self-loop relationships are dropped post-hoc.
- `memory_id` canonicals get a `memory_id_` prefix stripped post-hoc to
  match the bare-hex citation format the schema expects.

## Activate enrichment-aware retrieval (REQUIRED after enrichment)

**This is the load-bearing step most users miss.** Running `m3_enrich` and
`m3_entities` writes the enrichment artifacts (observations, entities,
relationships) to your DBs, but the default `memory_search` path will NOT
consult them unless three env vars are set. Without these flags, the
hours of enrichment you just ran sit in tables that retrieval ignores.

> ⚡ **Phase L auto-activation (since 2026-04-28):** the three gates below
> now auto-flip ON when the underlying tables have meaningful population —
> `>=100` observation rows for `M3_PREFER_OBSERVATIONS` /
> `M3_TWO_STAGE_OBSERVATIONS`, and `>0` rows in `entities` for
> `M3_ENABLE_ENTITY_GRAPH`. Counts are checked once per ~5 minutes per
> process (cached). You still SHOULD set the env vars explicitly for
> ingest paths and for clarity, but a working enriched DB will start
> serving observation- and entity-aware retrieval without a config edit
> + restart. **Escape hatch:** set `M3_DISABLE_AUTO_ACTIVATION=1` to
> require the explicit env vars — recommended for benchmark
> reproducibility, ablation studies, and any scenario where you want
> retrieval behavior to depend only on declared config.

### The three env vars

| Variable | What it does | Default |
|---|---|---|
| `M3_PREFER_OBSERVATIONS` | Post-rank observations (`type='observation'`) above raw chat turns. The atomic facts you extracted now lead the result list. | off |
| `M3_TWO_STAGE_OBSERVATIONS` | Expand observation hits with their source turns (~3 per hit). Gives the answerer the surrounding context, not just the distilled fact. | off |
| `M3_ENABLE_ENTITY_GRAPH` | Allow entity-graph traversal during retrieval — the `entities` + `entity_relationships` tables become consultable. Required for `memory_search_routed`'s entity branch to fire. | off |

Set all three to `1` (or `true` / `yes`) to fully activate.

### Where to set them — three options, pick the one that matches how you run m3-memory

#### A. MCP server (Claude Code / agents using m3 via MCP)

Edit your `~/.claude/settings.json` (or whatever MCP host config you use)
and add the env vars to the `memory` server's `env` block:

```json
"mcpServers": {
  "memory": {
    "command": "<path>/.venv/Scripts/python.exe",
    "args": ["<path>/bin/memory_bridge.py"],
    "env": {
      "LM_STUDIO_EMBED_URL": "http://127.0.0.1:1234/v1/embeddings",
      "CHROMA_BASE_URL": "http://<your-warehouse>:8000",
      "M3_PREFER_OBSERVATIONS": "1",
      "M3_TWO_STAGE_OBSERVATIONS": "1",
      "M3_ENABLE_ENTITY_GRAPH": "1"
    }
  }
}
```

> 🔁 **Restart your MCP host so the new env reaches the memory server**
> — MCP servers read their `env` block once at spawn time, so config
> changes do NOT take effect until a restart. Pick the recipe matching
> your host:
>
> | Host | Restart command |
> |---|---|
> | **Claude Code** (terminal) | Close and reopen the terminal session, OR run `/mcp` and pick "restart memory" from the picker |
> | **Claude Desktop** | Quit fully (tray icon → Quit, not just close window) and reopen |
> | **OpenCode / Aider / custom MCP host** | Whatever your host's "reload tool servers" / "restart" command is — most expose it as `/mcp` or `/restart-tools` |
> | **No GUI host (CI / headless)** | Kill and re-launch the process that owns the MCP child |
>
> Verification that the restart took: in a fresh tool call, run any
> `mcp__memory__memory_search` and check that observation rows appear
> in the results. If they don't, the env didn't propagate — see the
> [Verify it's active](#verify-its-active) section below.

#### B. Shell environment (CLI users, bench harnesses, scripts)

Add to your `~/.bashrc` / `~/.zshrc` / Windows env:

```bash
export M3_PREFER_OBSERVATIONS=1
export M3_TWO_STAGE_OBSERVATIONS=1
export M3_ENABLE_ENTITY_GRAPH=1
```

> 🔁 **Open a new shell session** (or `source ~/.bashrc`) for the
> exports to apply to the current terminal. Existing shells / running
> processes do not pick up the change.

#### C. Scheduled tasks / cron (auto-enrich drain jobs)

Wrap the cron command:

```bash
M3_PREFER_OBSERVATIONS=1 M3_TWO_STAGE_OBSERVATIONS=1 M3_ENABLE_ENTITY_GRAPH=1 \
    python bin/m3_enrich.py --drain-queue --drain-batch 50
```

(The drain itself doesn't need these — but keeping the env consistent
across all m3 invocations avoids "works in cron, doesn't work in shell"
surprises.)

> 🔁 **No restart needed for cron / Task Scheduler** — each invocation
> is a fresh process and reads the env from the wrapper line at launch.
> Just edit the cron entry / Task Scheduler action and the next firing
> picks it up.

### Verify it's active

After restarting, run a quick search and look for observation rows in
the result. From a Python REPL with the MCP env loaded:

```python
from memory_core import memory_search_scored_impl
hits = await memory_search_scored_impl("recent decisions", k=5)
for h in hits:
    print(h["type"], h["title"][:60])
# Expect to see at least one type='observation' row near the top.
```

Or via SQL — confirm observations exist for retrieval to find:

```sql
SELECT type, COUNT(*) FROM memory_items
WHERE COALESCE(is_deleted,0)=0
GROUP BY type ORDER BY COUNT(*) DESC LIMIT 5;
-- 'observation' should appear with a meaningful count after enrichment.

SELECT COUNT(*) FROM entities;
SELECT COUNT(*) FROM entity_relationships;
-- Both > 0 after `bin/m3_entities.py`.
```

> 🔁 **If you set the env vars but observations still don't show up
> in results**, the restart didn't take. The MCP `memory` server is
> still running with the OLD env. Symptoms:
>
> - Recent search results contain only `type='message'` / `type='note'`
>   etc., zero `type='observation'` — even though SQL confirms many
>   observations exist.
> - Entity-graph queries return empty results despite a populated
>   `entities` table.
>
> Fix: re-run the restart step from the recipe matching your host
> above. If unsure whether the restart took, in Claude Code run
> `/mcp` and check that the `memory` server status line reflects a
> recent start time.

### Cost / risk

- `M3_PREFER_OBSERVATIONS` and `M3_TWO_STAGE_OBSERVATIONS` cost a few
  ms per search and ~500-2000 extra tokens of context (the observation
  rows + their source turns). Worth it.
- `M3_ENABLE_ENTITY_GRAPH` adds an extra graph-traversal SQL query when
  the query has named entities. Negligible on a populated graph; no-op
  otherwise.
- Default-off is for backward-compat with users who haven't run
  enrichment. Once you HAVE run enrichment, default-off is just leaving
  the lights off in a furnished house.

### Why the gates aren't auto-flipped on data presence

A common question: "shouldn't the search auto-detect observations and
fire the post-rank?" Possibly — that's a Phase J consideration tracked
in the m3-memory roadmap. The conservative current behavior is
explicit-opt-in so the retrieval semantics never change without the
operator knowing.

## Continuous enrichment (auto-enrich on chatlog ingest)

Once your chatlog hooks are wired (Claude Code PreCompact/Stop, Gemini
SessionEnd, OpenCode session_end), every closed conversation runs through
`bin/chatlog_ingest.py`. With `M3_AUTO_ENRICH=1`, that ingest path also
**enqueues the conversation** for the Observer pipeline. A periodic
`m3_enrich --drain-queue` then turns those queue rows into observations.

### Setup

**1. Enable the auto-enqueue hook on chatlog ingest:**

Add to your shell profile (`.bashrc` / `.zshrc`) or set per-session:

```bash
export M3_AUTO_ENRICH=1
# Optional: minimum turns per ingest before enqueue (default 10).
# Lower if you want short conversations enriched too.
export M3_AUTO_ENRICH_MIN_TURNS=10
```

The `INSERT OR IGNORE` semantics mean re-ingesting the same conversation
is harmless — debouncing happens at the queue level. Single-turn pings
and short status-check sessions are skipped via the min-turns gate.

**2. Drain the queue periodically:**

```bash
# Single-shot drain (run anytime; returns when queue is empty)
python bin/m3_enrich.py --drain-queue
```

The drainer:
- Pops up to `--drain-batch` rows (default 100) from `observation_queue`
  on **both** core memory + chatlog DBs
- Calls the Observer SLM per conversation
- Writes observations under `--target-variant` (default `m3-observations-YYYYMMDD`)
- Marks queue rows complete; bumps `attempts` on failure for retry (max 5)

### Scheduled-task recipes

#### macOS / Linux (cron)

Add to your crontab — drain every 30 minutes:

```cron
*/30 * * * * cd /path/to/m3-memory && python bin/m3_enrich.py --drain-queue >> ~/.m3-memory/enrich.log 2>&1
```

#### Linux (systemd timer)

`~/.config/systemd/user/m3-enrich-drain.service`:

```ini
[Unit]
Description=m3-enrich drain pending observations

[Service]
Type=oneshot
WorkingDirectory=/path/to/m3-memory
ExecStart=/usr/bin/python bin/m3_enrich.py --drain-queue
StandardOutput=append:%h/.m3-memory/enrich.log
StandardError=append:%h/.m3-memory/enrich.log
```

`~/.config/systemd/user/m3-enrich-drain.timer`:

```ini
[Unit]
Description=Run m3-enrich drain every 30 minutes

[Timer]
OnUnitActiveSec=30min
OnBootSec=2min
Persistent=true

[Install]
WantedBy=timers.target
```

Enable:

```bash
systemctl --user daemon-reload
systemctl --user enable --now m3-enrich-drain.timer
```

#### Windows (Scheduled Task)

PowerShell:

```powershell
$action = New-ScheduledTaskAction `
    -Execute "python" `
    -Argument "C:\path\to\m3-memory\bin\m3_enrich.py --drain-queue" `
    -WorkingDirectory "C:\path\to\m3-memory"

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName "M3 Enrich Drain" `
    -Action $action -Trigger $trigger
```

### Config knobs

| env var | default | purpose |
|---|---|---|
| `M3_AUTO_ENRICH` | `0` (off) | Enable enqueue at chatlog ingest |
| `M3_AUTO_ENRICH_MIN_TURNS` | `10` | Skip enqueue if ingest wrote fewer turns |
| `M3_REFLECTOR_THRESHOLD` | `50` | Observation count before Reflector fires |
| `M3_PREFER_OBSERVATIONS` | `0` (off) | Prefer observation rows in retrieval |
| `M3_TWO_STAGE_OBSERVATIONS` | `0` (off) | Expand top-k obs into source turns |
| `M3_OBSERVATION_BUDGET_TOKENS` | `4000` | Token budget for obs-only retrieval |

### Operational notes

- **Cost stays bounded.** Each conversation only enriches once (`INSERT OR
  IGNORE`). Re-ingest = no-op on the queue.
- **Drain failures self-recover.** If the SLM is unreachable when a drain
  fires, the queue row's `attempts` increments; next drain retries.
  After 5 attempts the row is skipped (manual review with `SELECT *
  FROM observation_queue WHERE attempts >= 5`).
- **Keep the drain cadence loose.** Every 30 min is more than enough for
  most workflows. Hourly is fine if your SLM is shared with bench runs.
- **No daemon needed.** `--drain-queue` exits when the queue is empty.
  No long-running process to monitor.

### Troubleshooting

**Queue isn't draining**

Check the queue depth:
```bash
sqlite3 memory/agent_chatlog.db \
    "SELECT COUNT(*), MAX(attempts) FROM observation_queue"
```

If `attempts >= 5`, the SLM endpoint was unreachable on every retry. Fix
the endpoint (LM Studio not running, API key missing) and reset:
```bash
sqlite3 memory/agent_chatlog.db \
    "UPDATE observation_queue SET attempts=0, last_error=NULL WHERE attempts >= 5"
```

**Auto-enqueue isn't firing**

Verify the env var is set in the *same shell* that runs `chatlog_ingest`:
```bash
env | grep M3_AUTO_ENRICH
```

If your chatlog ingest is invoked from a host-agent hook (Claude Code,
Gemini), the env var must be exported in that hook's environment. Edit
the hook command in `~/.claude/settings.json` or `~/.gemini/settings.json`
to include `M3_AUTO_ENRICH=1` as a prefix:

```json
"hooks": {
  "PreCompact": [{
    "hooks": [{
      "type": "command",
      "command": "M3_AUTO_ENRICH=1 python /path/to/bin/chatlog_ingest.py ..."
    }]
  }]
}
```

---

## Architecture (one paragraph)

`m3_enrich` is a wrapper around the Phase D Mastra Observer + Reflector
pipeline. It groups conversations by `(user_id, conversation_id,
metadata_json.session_id)` from the source DB, sends each group as a
JSON block to the SLM endpoint defined by the profile, parses the
returned `{observations: [...]}` JSON, and writes each observation as a
`type='observation'` row with three-date metadata. The Reflector stage
then runs over groups exceeding the threshold to detect cross-conv
supersedes/merges, writing supersedes edges into `memory_relationships`.
Both stages are env-gated for retrieval (`M3_PREFER_OBSERVATIONS`,
`M3_TWO_STAGE_OBSERVATIONS`); production behavior is unchanged unless
those flags are set.

For deeper detail, see `docs/MASTRA_DESIGN.md`.

## Adding a new enrichment stage

The current pipeline has two stages:

| Stage | Input | Output | Queue table |
|-------|-------|--------|-------------|
| 1. Observer  | `memory_items` rows where `type IN ('message','conversation','chat_log')` | `type='observation'` rows | `observation_queue` |
| 2. Reflector | `type='observation'` rows | supersedes edges in `memory_relationships` | `reflector_queue` |

Future stages (e.g. `entity_consolidator`, `timeline_validator`) plug
into the same shape:

1. **Pick a stage name** — lowercase snake_case. Add it to
   `bin/m3_enrich_stage.py` `KNOWN_STAGES`.
2. **Reuse a queue table** — migration 026 added a `stage` column to
   both queues, so a new stage can ride on `observation_queue` (raw
   text in) or `reflector_queue` (already-extracted observations in)
   without another migration.
3. **Write a drainer** — pop work via
   `m3_enrich_stage.pop_batch(table, stage, limit, db)`, ack with
   `ack(...)`, fail with `fail(...)`. The Observer/Reflector
   drainers still hold their own inline SQL for now; the helper
   exists so new stages don't add to the duplication.
4. **Wire enqueue** — add an `xxx_enqueue_impl()` next to the
   existing two in `bin/memory_core.py`, mirroring their `INSERT OR
   IGNORE` pattern with `stage='your_stage'`.

The two queue tables stay separate until there are ≥3 real stages;
collapsing them is a future migration, not part of adding stage 3.
