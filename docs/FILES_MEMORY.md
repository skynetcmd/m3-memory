# Files Memory

> Ingest entire directories into a separate `files.db` store. Hybrid
> search (FTS5 + vector). Hierarchical chunking with file-level
> supersession. Fact extraction. Promotion to core memory when an item
> earns it. Watch-mode staleness review.

m3-memory has three stores, separated by lifecycle:

| Store | Volume | Lifecycle | Purpose |
|---|---|---|---|
| `memory.db` | curated, low | indefinite | Decisions, preferences, facts you want forever |
| `chatlog.db` | append firehose | decay + promote | Conversational turns; selectively promoted |
| **`files.db`** | bulk (regeneratable) | tied to source | Directory ingestion, hierarchical document store |

Files-memory is the third store. Default location: `~/.m3/files_database.db`
(override with `M3_FILES_DB_PATH`).

## When to use it

- You have a corpus of documents (Markdown notes, PDFs, plain text) you
  want the assistant to be able to search and cite verbatim.
- You want the assistant to know what's in your project's `docs/`
  directory without re-reading the whole tree every conversation.
- You want stale-content detection: when files change, the assistant
  should notice without you telling it.
- You want selective promotion: a search hit becomes a permanent
  memory only when you say so.

## Quick start

The CLI lives in `bin/`, so put it on the path (`PYTHONPATH=bin`, or run
`python bin/files_memory/tools.py …` directly):

```bash
# Ingest a directory
PYTHONPATH=bin python -m files_memory.tools ingest ~/Documents/notes --include "*.md"

# Triage: file-level summaries, no leaf content
PYTHONPATH=bin python -m files_memory.tools index --limit 20

# Search: hybrid FTS5 + vector
PYTHONPATH=bin python -m files_memory.tools search "what did we decide about caching"
```

All three are also available as MCP tools (`files_ingest`,
`files_index`, `files_search`) — the assistant can call them directly
once the m3-memory MCP server is loaded in your client.

## What you get

Measured on the fixed eval corpus: **22/22 top-5 text recall (100%)** and
**100% fact recall** — see [Eval gates](#eval-gates) for the harness.

- **Hierarchical chunking.** Markdown splits at heading tree; PDF splits
  by page; plain text uses a semantic paragraph chunker.
- **Hybrid search.** FTS5 keyword + vector cosine + Reciprocal Rank
  Fusion. Returns hits with provenance (file + division + char range).
- **Wiki-index pattern.** `files_index` gives summaries-only
  (~50 tokens each) so the assistant can triage 200 files in one read
  before pulling leaf content for the 3–5 that matter.
- **Version history.** Re-ingesting a changed file supersedes the
  previous version; the prior content stays queryable with
  `include_history=True`.
- **4× faster incremental re-ingest.** When one section of a
  multi-section file changes, only that section is re-embedded; unchanged
  sections reuse cached embeddings — a measured ~4× speedup in P3 eval
  gates, versus systems that re-embed the whole file on every edit.
- **Fact extraction (optional).** Inline (sync inside ingest) or queue
  (drain later). Facts carry `source_span` back to the leaf text.
- **Entity linking.** Extracted entity names resolve against
  `memory.db`'s entity table; provisional entities for unknowns.
- **Ascension.** Promote selected facts / leaves / file_summaries into
  `memory.db` as curated knowledge. Idempotent; metadata back-points
  to the source.
- **Watch mode.** Polling daemon detects stale / new / missing / failed-
  extraction files and emits notifications via `memory.db`'s inbox.
- **Multi-corpus.** One files.db can hold many corpora; `corpora=[a,b]`
  fans out searches across them.

## Enabling fact extraction

Ingestion is text-only by default (`extract_mode="none"`): you get hierarchical
chunks, embeddings, and extractive head-of-section summaries — enough for
hybrid search. **Fact extraction** is the opt-in layer that runs each chunk
through a local LLM to distil atomic, queryable facts (with entity tags and a
`source_span` back to the leaf text). It is **off until you point it at an LLM
endpoint.**

### 1. Configure the endpoint

Fact extraction (and the LLM summarizer) read these env vars:

| Variable | Purpose |
|---|---|
| `M3_FILES_EXTRACT_URL` | Base URL of an OpenAI-compatible chat endpoint, **without** the `/v1` suffix (the code appends `/v1/chat/completions`). E.g. `http://127.0.0.1:1234`. If unset, extraction is unavailable and ingest falls back to extractive summaries. |
| `M3_FILES_EXTRACT_MODEL` | Model id to request. E.g. `mistralai/ministral-3-14b-reasoning`. Default `qwen3-4b-instruct`. |
| `M3_FILES_SUMMARY_URL` / `M3_FILES_SUMMARY_MODEL` | Same, for the abstractive file/section summarizer. Falls back to `M3_LMSTUDIO_URL` if set. |
| `LM_API_TOKEN` | **Required if your endpoint enforces auth** (LM Studio's "require API key" is on by default). Sent as `Authorization: Bearer <token>`. Stored in the OS keyring / vault — see [ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md#api-keys--authentication). Omit for tokenless endpoints (e.g. Ollama). |

> **Auth gotcha.** An auth-enabled LM Studio silently rejects requests with no
> `Authorization` header — extraction produces zero facts with no error. Set
> `LM_API_TOKEN` (or disable auth in LM Studio's server settings). m3 reads the
> token from env/keyring automatically once it's set.

Keep `temperature` at the server default of 0 for the model card — LM Studio
resets the per-model temp slider to its UI default (often 0.8) on reload, which
makes extraction non-deterministic.

### 2. Choose when extraction runs

| `extract_mode` | Behavior |
|---|---|
| `"none"` (default) | Text + extractive summaries only. No LLM calls. |
| `"inline"` | Extract synchronously inside the ingest transaction. Simplest, but slows ingest by one LLM call per chunk. |
| `"queue"` | Mark chunks `pending` at ingest time; drain them later with `files_extract_pending`. Best for large corpora — ingest stays fast, extraction runs as a separate batch you can checkpoint. |

### 3. Worked example (queue + drain)

The CLI lives in `bin/`, so run it with `bin/` on the path:

```bash
# Point extraction at a local LM Studio (auth on -> LM_API_TOKEN must be set)
export M3_FILES_EXTRACT_URL=http://127.0.0.1:1234
export M3_FILES_EXTRACT_MODEL=mistralai/ministral-3-14b-reasoning

# Ingest with queue mode — chunks land 'pending', ingest stays fast.
# `--mode` selects extraction; `extract` drains the queue afterwards.
PYTHONPATH=bin python -m files_memory.tools ingest ~/Documents/notes \
  --include "*.md" --mode queue

# Drain the queue in batches (safe to re-run; resumes where it left off)
PYTHONPATH=bin python -m files_memory.tools extract --limit 100
```

Or via MCP (the assistant calls these directly): `files_ingest(path=...,
extract_mode="queue")` then `files_extract_pending(limit=100)` repeatedly until
it reports `ok + failed + skipped == 0`.

> **Note on re-ingesting existing files.** `extract_mode="queue"` only marks
> chunks pending for files whose **content changed** — re-ingesting an unchanged
> file is a content-hash no-op and queues nothing. To extract over a corpus you
> already ingested with `extract_mode="none"`, re-ingest after an edit, or use
> `inline` mode on the next change.

Chunks below `EXTRACT_MIN_LEAF_CHARS` (120) are skipped as too short. Extracted
facts can later be promoted into curated `memory.db` with `files_promote`.

## Tool reference

The full reference for all 21 files_memory tools lives at
[`docs/tools/files_memory.md`](tools/files_memory.md). The design
rationale and phasing detail is in
[`docs/decisions/FILE_INGESTION_PLAN.md`](decisions/FILE_INGESTION_PLAN.md).

## Eval gates

| Gate | Coverage |
|---|---|
| `tests/eval_files_ingest.py`   | P1 retrieval — 22 Q-A pairs over a fixed eval corpus |
| `tests/eval_files_phase2.py`   | P2 extraction + ascension + staleness |
| `tests/eval_files_phase3.py`   | P3 provenance + carry-forward + dedup + rename + promotability |
| `tests/eval_files_phase4.py`   | P4 watch daemon + multi-corpus + cross-corpus search |

Phase-1 baseline: 22/22 questions find their expected answer in the
top-5 results (100% text recall). Phase-2: 47 facts extracted from the
5-file corpus, 100% fact recall. P3 + P4 each clear all sub-gates.
