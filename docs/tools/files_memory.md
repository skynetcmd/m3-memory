# files-memory — 21 MCP tools for directory ingestion

> Status: 2026-05-17. Per-tool reference for the `files_memory` package.
> Audience: someone wiring the file-ingestion pipeline into an agent
> workflow or debugging a specific tool. Design context:
> `docs/FILE_INGESTION_PLAN.md`. Source under `bin/files_memory/`.

The files-memory layer ships a separate `files.db` store (default
`~/.m3/files_database.db`) alongside the core `memory.db`. It walks a
directory, chunks files hierarchically, builds a hybrid FTS5 + vector
index, optionally extracts facts and links them to entities in
`memory.db`, supports ascension of selected items to core memory, and
provides watch-mode staleness review for ongoing corpus health.

All 21 tools below are registered in `bin/mcp_tool_catalog.py` and
callable through the m3-memory MCP bridge or the
`python -m files_memory.tools <command>` CLI.

---

## Tool index

| # | Tool | Phase | Purpose |
|---|---|---|---|
| 1 | `files_ingest`              | P1 | Walk a directory, chunk + embed every supported file |
| 2 | `files_search`              | P1 | Hybrid FTS5 + vector search over leaves |
| 3 | `files_index`               | P1 | Wiki-index of file summaries (cheap-first triage) |
| 4 | `files_get`                 | P1 | Fetch one record (file_node or leaf) by UUID |
| 5 | `files_stats`               | P1 | Per-corpus counters (file_nodes, leaves, embed coverage) |
| 6 | `files_health`              | P1 | DB integrity + FTS5 sync check |
| 7 | `files_extract_pending`     | P2 | Drain leaves marked extraction_status='pending' through the LLM |
| 8 | `files_promote`             | P2 | Promote a fact / leaf / file_summary into memory.db |
| 9 | `files_promotion_list`      | P2 | List existing promotions; filter by drifted source |
| 10 | `files_promotable`         | P3 | Top promotion candidates by usage-weighted score |
| 11 | `files_dedup`              | P3 | Scan leaf embeddings for near-duplicates |
| 12 | `files_dedup_list`         | P3 | List candidate pairs from `files_dedup` |
| 13 | `files_dedup_review`       | P3 | Record a 'kept' / 'merged' / 'ignored' decision |
| 14 | `files_staleness_review`   | P2/P3 | Report stale/touched/missing/new/failed/drifted/rename candidates |
| 15 | `files_link_rename`        | P3 | Re-point a file_node at a new path (rename without content change) |
| 16 | `files_corpus_create`      | P4 | Register a new corpus with optional default overrides |
| 17 | `files_corpus_list`        | P4 | Enumerate corpora with row counts |
| 18 | `files_corpus_get`         | P4 | Fetch a single corpus's settings + counts |
| 19 | `files_corpus_set`         | P4 | Update an existing corpus's settings |
| 20 | `files_corpus_delete`      | P4 | Remove a corpus (cascade=True drops file_nodes too) |
| 21 | `files_watch_once`         | P4 | Single staleness + notify pass; suitable for cron |

CLI also exposes `watch` (the long-running poller behind
`files_watch_once`) and `extract` as a synonym for `files_extract_pending`.

---

## Where the implementations live

| Module | Tools |
|---|---|
| `bin/files_memory/ingest.py`        | `files_ingest` |
| `bin/files_memory/search.py`        | `files_search` |
| `bin/files_memory/index.py`         | `files_index`, `files_get`, `files_stats` |
| `bin/files_memory/db.py`            | `files_health` |
| `bin/files_memory/extract.py`       | `files_extract_pending` |
| `bin/files_memory/promote.py`       | `files_promote`, `files_promotion_list` |
| `bin/files_memory/promotability.py` | `files_promotable` |
| `bin/files_memory/dedup.py`         | `files_dedup`, `files_dedup_list`, `files_dedup_review` |
| `bin/files_memory/staleness.py`     | `files_staleness_review`, `files_link_rename` |
| `bin/files_memory/corpora.py`       | `files_corpus_*` (5 tools) |
| `bin/files_memory/watch.py`         | `files_watch_once` (+ `watch_loop` for the CLI) |
| `bin/files_memory/tools.py`         | MCP registration + CLI dispatcher |

Each module is independently testable; the package has no circular
imports on `bin/memory/` beyond `memory.config` (env reads) and
`memory.embed` (the embed cascade).

---

## Phase 1 tools — walker, hybrid search, index

### `files_ingest`

Walk a directory and ingest every supported file into `files.db`.
Idempotent: same `content_sha256` → no-op; changed content → new
file_node version supersedes the prior one. Symlinks off by default;
binary files skipped via NUL-byte sniff; per-file size cap at 10 MiB
(`--force-size` to override).

Key parameters:

| Param | Type | Default | Notes |
|---|---|---|---|
| `path` | string | required | Directory (or single file) to walk. |
| `include` | list[str] | None | Glob patterns; only matching files are ingested. |
| `exclude` | list[str] | None | Glob patterns; matching files are skipped. |
| `max_depth` | int | None | Max recursion depth from the root (0 = root only). |
| `corpus` | string | resolved | Corpus tag. Resolution: arg > `M3_FILES_CORPUS` env > corpus_settings.default > `"default"`. |
| `extract_mode` | enum | None | `none` \| `inline` \| `queue`. `inline` extracts facts synchronously; `queue` marks leaves pending for a later drain. |
| `original_path` | string | None | Pointer at source artifact when single-file ingest is a conversion. Sidecar `<path>.m3meta.json` overrides this per-file. |
| `dry_run` | bool | False | Walk + count without writing. |
| `record_noops` | bool | False | Write `unchanged_skipped` rows for audit. |

Returns a JSON-safe dict with `run_id`, walk stats, per-bucket file
counts (`files_created`, `files_superseded`, `files_unchanged`,
`files_failed`), and carry-forward telemetry
(`leaves_carried`, `embeds_avoided`, `facts_carried`).

### `files_search`

Hybrid FTS5 + vector cosine + Reciprocal Rank Fusion (k=60). Default
filters to current (non-superseded) leaves + file_nodes. Set
`include_history=True` for time-travel queries.

Cross-corpus: pass `corpora=["alpha","beta"]` for fan-out across many
corpora (returns `corpus_id` on each hit). When both `corpus` and
`corpora` are passed, `corpora` wins.

### `files_index`

The wiki-index primitive: returns file-level summaries (filename, path,
filetype, summary, date_modified) with **no leaf content**. Use BEFORE
`files_search` to triage which files are worth deep-reading — much
smaller token footprint than full search results, and matches the
"Karpathy LLM wiki" pattern from the plan.

### `files_get`, `files_stats`, `files_health`

`files_get(uuid)` resolves to either a file_node or a leaf row. Surfaces
`original_path` and `corpus_id` at the top level. `files_stats(corpus)`
returns counts + embed coverage. `files_health(rebuild=False)` runs
`PRAGMA integrity_check`, checks FTS5 vs. base-table row counts,
counts orphan leaves and orphan ingestion_runs; `rebuild=True`
rebuilds FTS5 when out of sync.

---

## Phase 2 tools — extraction, ascension

### `files_extract_pending`

Drains leaves with `extraction_status='pending'` (left there by a
queue-mode `files_ingest`) through the configured LLM extractor. Each
leaf gets a JSON-mode prompt; the parser strips markdown fences and
recovers trailing commas, then resolves each fact's source phrase back
into a `source_span` (char range) inside the leaf text. Failures land
in `extraction_attempts` for staleness review's `failed_extraction`
bucket. Safe to call repeatedly.

### `files_promote`

Ascension: copies a fact / leaf / file_summary from `files.db` into
`memory.db`. The source row is **not modified** — promotion is a copy
with a metadata back-pointer (`source_memory_id`, `source_path`,
`source_version_label`, `promotion_reason`). Idempotent via the
`promotion_markers` table in `files.db`. Includes orphan recovery: if
a memory.db row exists with back-pointer metadata but the marker is
missing (a half-completed cross-DB write), the recovery path re-attaches
instead of writing a duplicate.

Type mapping: `fact → fact`, `leaf → knowledge` (default; overridable
via `mapped_type`), `file_summary → reference`.

### `files_promotion_list`

Lists promotions. `source_superseded=True` surfaces only promotions
whose source file_node has since been superseded — candidates for
review since the promoted memory may now be out of step with the
source's current state. (Phase 5's `files_promotion_review` interactive
flow is open work.)

---

## Phase 3 tools — promotability, dedup, rename

### `files_promotable`

Surfaces top promotion candidates by usage-weighted heuristic:
`score = log(1 + hit_count) * confidence * exp(-age_days / half_life)`.
Half-life defaults to 30 days. `hit_count` is incremented every time a
fact's leaf surfaces in `files_search` results (the search path itself
does the increment). Suggestion-only — never auto-promotes.

Defaults: top 20 candidates above `min_score=0.30`. Already-promoted
items hidden unless `include_already_promoted=True`.

### `files_dedup`, `files_dedup_list`, `files_dedup_review`

Pairwise semantic dedup over `leaf_embeddings`. `files_dedup` scans
within a single corpus (or all current leaves when `corpus` is None),
finds pairs above `threshold` (default 0.92 cosine), skips intra-file
pairs and carry-forward edges (`evolved_from`), and records candidates
in `semantic_dedup_candidates` with their cosine score.

Two-phase pipeline:

| Stage | Tool | Purpose |
|---|---|---|
| Detect | `files_dedup` | Find pairs; record candidates |
| Review | `files_dedup_list` | Inspect text snippets + paths |
| Decide | `files_dedup_review` | Record `kept` \| `merged` \| `ignored` |

`merged` is currently intent-only; actual leaf merging is a future
operation (would need careful rewiring of `evolved_from` chains).

### `files_staleness_review`, `files_link_rename`

`files_staleness_review` classifies the current filesystem state into
six buckets:

- **stale**: mtime + sha changed → re-ingest
- **touched_only**: mtime bumped but content unchanged → skip
- **missing**: in db, not on disk → mark retired?
- **new**: on disk, never ingested → ingest
- **failed_extraction**: leaves with `extraction_status='failed'`
- **drifted_promotion**: promoted memory whose source file_node has been superseded
- **rename_candidate**: a `missing` and a `new` file whose `content_sha256` matches → looks like a rename

`files_link_rename(uuid, new_path)` re-points a file_node at a new path
without supersession (content stays identical). Refuses when the
content has changed too — caller should re-ingest in that case.

---

## Phase 4 tools — multi-corpus, watch mode

### Multi-corpus management

Five tools form a complete CRUD surface over the `corpus_settings`
table. Per-corpus settings stored as JSON: `extract_mode`, `scope`,
`description`, `default`, `retention_days`, `created_at`.

| Tool | Purpose |
|---|---|
| `files_corpus_create(id, ...)` | Register a new corpus. `default=True` flips this corpus to be the installation default (clears the flag on prior default in the same transaction). |
| `files_corpus_list()` | Enumerate corpora (incl. corpora that have file_nodes but no settings row) with row counts. |
| `files_corpus_get(id)` | One corpus's settings + counts. |
| `files_corpus_set(id, ...)` | Update fields; None args are no-ops. Creates the settings row if absent. |
| `files_corpus_delete(id, cascade=False)` | Remove the settings row; cascade=True also deletes every file_node in the corpus (DESTRUCTIVE — refuses without the flag when files exist). |

Default-corpus resolution order: `--corpus` CLI flag > `M3_FILES_CORPUS`
env > `corpus_settings.default` row > `"default"`.

`files_search` and `files_index` gain a `corpora=[...]` list parameter
that overrides single-`corpus_id` for fan-out queries.

### `files_watch_once`

Single staleness-review + notification dispatch pass. Suitable for cron,
scheduled tasks, or one-off invocations. Each cycle invokes
`files_staleness_review` and emits a notification per
(file_node, event_kind) pair via `memory.db`'s `notifications` inbox
(`mcp__memory__notify` shape). Per-pair cooldown (default 1 hour)
suppresses duplicate notifications across cycles — state lives in a
`watch_state` k/v table inside `files.db` so cooldowns survive restarts.

CLI also exposes `watch` (the blocking polling loop) — invocable as
`python -m files_memory.tools watch --interval-seconds 300 --directory ~/Documents`.
The poller is SIGINT-friendly and accepts `--max-cycles` for tests.

---

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `M3_FILES_DB_PATH` | `~/.m3/files_database.db` | Path to files.db |
| `M3_FILES_DB_PROMPT_ON_FIRST_USE` | `1` | First-use prompt UX in CLI |
| `M3_FILES_CORPUS` | `default` (after resolution) | Default corpus tag |
| `M3_FILES_DEFAULT_EXTRACT_MODE` | `none` | Extract mode if `--mode` not passed |
| `M3_FILES_EXTRACT_CONCURRENCY` | `2` | Per-leaf extraction concurrency |
| `M3_FILES_EXTRACT_MIN_LEAF_CHARS` | `120` | Skip extraction below this size |
| `M3_FILES_EXTRACT_URL` | (none) | LLM endpoint for extraction |
| `M3_FILES_EXTRACT_MODEL` | `qwen3-4b-instruct` | LLM model for extraction |
| `M3_FILES_SUMMARY_URL` | (none) | LLM endpoint for summaries |
| `M3_FILES_PROMO_HALF_LIFE_DAYS` | `30` | Promotability score decay |
| `M3_FILES_PROMO_SUGGEST_THRESHOLD` | `0.30` | Promotability minimum |
| `M3_FILES_DEDUP_THRESHOLD` | `0.92` | Semantic dedup cosine floor |
| `M3_FILES_DEDUP_LEAF_LIMIT` | `10000` | Max leaves per dedup scan |
| `M3_FILES_MAX_FILE_BYTES` | `10485760` | Per-file size cap (10 MiB) |
| `M3_FILES_MAX_FILES_PER_INGEST` | `10000` | Per-ingest file count cap |
| `M3_FILES_FOLLOW_SYMLINKS` | `0` | Symlink policy during walk |

See `bin/files_memory/config.py` for the complete list with comments.

---

## Quick-start CLI recipes

```bash
# Ingest a corpus (markdown-only, no extraction)
python -m files_memory.tools ingest ~/Documents/notes \
    --include "*.md" --corpus notes

# Same, with inline fact extraction
M3_FILES_EXTRACT_URL=http://localhost:11434 \
M3_FILES_EXTRACT_MODEL=qwen2.5:1.5b-instruct \
python -m files_memory.tools ingest ~/Documents/notes \
    --mode inline --corpus notes

# Triage: find files relevant to "embedding cache"
python -m files_memory.tools index --corpus notes \
    | jq '.[] | select(.summary | contains("embedding"))'

# Then drill into hits
python -m files_memory.tools search "embedding cache invalidation" \
    --corpus notes --limit 5

# Cross-corpus search
python -m files_memory.tools search "GDPR retention" \
    --corpora notes,policies

# Promote a fact to core memory
python -m files_memory.tools promote <fact-uuid> --reason "core decision"

# Run a watch-cycle (one-shot, suitable for cron)
python -m files_memory.tools watch-once --directory ~/Documents/notes

# Run a watch loop (blocking; Ctrl-C to stop)
python -m files_memory.tools watch --directory ~/Documents/notes \
    --interval-seconds 300
```

---

## Cross-references

- `docs/FILE_INGESTION_PLAN.md` — design rationale + phasing detail
- `tests/eval_files_ingest.py` — P1 acceptance gate (22 Q-A pairs)
- `tests/eval_files_phase2.py` — P2 extraction + ascension + staleness
- `tests/eval_files_phase3.py` — P3 provenance + carry-forward + dedup + rename + promotability
- `tests/eval_files_phase4.py` — P4 watch + multi-corpus + cross-corpus
- `bin/mcp_tool_catalog.py` — canonical MCP registration (all 96 tools)
