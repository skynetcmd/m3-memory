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

```bash
# Ingest a directory
python -m files_memory.tools ingest ~/Documents/notes --include "*.md"

# Triage: file-level summaries, no leaf content
python -m files_memory.tools index --limit 20

# Search: hybrid FTS5 + vector
python -m files_memory.tools search "what did we decide about caching"
```

All three are also available as MCP tools (`files_ingest`,
`files_index`, `files_search`) — the assistant can call them directly
once the m3-memory MCP server is loaded in your client.

## What you get

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
- **Carry-forward.** When one section of a multi-section file changes,
  the unchanged sections reuse their embeddings — measured ~4× faster
  re-ingest in P3 testing.
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

## Tool reference

The full reference for all 21 files_memory tools lives at
[`docs/tools/files_memory.md`](tools/files_memory.md). The design
rationale and phasing detail is in
[`docs/FILE_INGESTION_PLAN.md`](FILE_INGESTION_PLAN.md).

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
