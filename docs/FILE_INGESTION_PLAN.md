# File Ingestion Plan — `files.db` + Ascension

> Status: design (2026-05-17). Pre-implementation. Phasing in §11.
>
> This document consolidates the design discussion of 2026-05-17 (Claude Code
> session). It is the working spec for adding directory-walking, hierarchical
> file ingestion to m3-memory, with a separate `files.db` store, append-only
> version history, file- and leaf-level supersession, fact extraction, entity
> graph integration, and a "promotion to core memory" (ascension) path.
>
> Companion documents (existing): `EMBED_DEPLOYMENT.md`, `EMBED_INPUT_RECIPE.md`,
> `MEMORY_ENTITY_EXTRACTION_PLAN.md`, `ARCHITECTURE.md`, `AGENT_INSTRUCTIONS.md`.

---

## Table of contents

1. [Goal and non-goals](#1-goal-and-non-goals)
2. [Architecture at a glance](#2-architecture-at-a-glance)
3. [Three-store separation, justified](#3-three-store-separation-justified)
4. [The file-node tree](#4-the-file-node-tree)
5. [Identity, versioning, supersession](#5-identity-versioning-supersession)
6. [Chunking dispatcher (per filetype)](#6-chunking-dispatcher-per-filetype)
7. [Extraction pipeline](#7-extraction-pipeline)
8. [Entity graph integration](#8-entity-graph-integration)
9. [Ascension — promotion to core memory](#9-ascension--promotion-to-core-memory)
10. [Tools and CLI surface](#10-tools-and-cli-surface)
11. [Phasing](#11-phasing)
12. [Operational concerns](#12-operational-concerns)
13. [Failure modes and resilience](#13-failure-modes-and-resilience)
14. [Open questions to resolve before phase 1 lands](#14-open-questions-to-resolve-before-phase-1-lands)
15. [Cross-references](#15-cross-references)

---

## 1. Goal and non-goals

### Goal

Allow the user to point m3 at a directory and have it:

1. Walk the tree at any depth, with sensible filters.
2. For every supported file, build a **persistent, queryable, hierarchical
   memory representation** of the file's content — not just an index, the
   actual content addressable at every level of granularity (file → division
   → chunk → fact).
3. Preserve full **provenance** — every retrieved chunk or fact can cite its
   file, version, division (page/slide/section), char range, and the
   ingestion run that produced it.
4. Track **version history** — re-ingesting a changed file does not destroy
   the prior version; old leaves are superseded, not deleted.
5. Expose **promotion ("ascension")** of selected file-derived items to
   curated core memory in `memory.db`, with a back-pointer to the source.
6. Make **staleness, drift, and re-ingestion** a first-class operation, not
   an afterthought — the system tells the user what's worth re-mining.

### Non-goals (phase 1–3)

- Real-time filesystem watching. (Phase 4 candidate.)
- Markdown rendering / a UI. m3 is a store; viewers live elsewhere.
- Cloud sync of `files.db`. It's local-host; if you want sync, copy the file.
- Cross-host ingestion coordination. One ingester per `files.db` at a time.
- General-purpose ETL. The pipeline is opinionated for personal corpora
  (docs, notes, code, papers), not arbitrary structured data.

---

## 2. Architecture at a glance

```
                     ┌──────────────────────────┐
   directory  ──▶    │   files_ingest (walker)  │
                     └─────────────┬────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                    │
              ▼                    ▼                    ▼
       ┌─────────────┐      ┌─────────────┐     ┌─────────────┐
       │ file filter │      │  chunker    │     │ extractor   │
       │  (ignore,   │      │ dispatcher  │     │ (inline OR  │
       │   size, mime)│     │ (per type)  │     │ queue→async)│
       └─────────────┘      └─────────────┘     └─────────────┘
                                   │
                                   ▼
                ┌────────────────────────────────────┐
                │           files.db                 │
                │  ┌──────────────────────────────┐  │
                │  │ file_node (versioned)        │  │
                │  │  ├── metadata (fs truth)     │  │
                │  │  ├── ingestions (append-only)│  │
                │  │  └── content                 │  │
                │  │       ├── leaf (page/slide…) │  │
                │  │       │    ├── text+embed    │  │
                │  │       │    └── facts ────────┼──┼──▶ entity_ref ──┐
                │  │       └── …                  │  │                  │
                │  └──────────────────────────────┘  │                  │
                └────────────────────────────────────┘                  │
                                                                        │
                ┌───────────────────────────────────────────────────────┘
                ▼
         ┌─────────────┐         ┌─────────────────────────┐
         │  memory.db  │ ◀─────  │ files_promote (manual)  │
         │  (core +    │         │ files_promotion_review  │
         │   entities) │         │   (drift detection)     │
         └─────────────┘         └─────────────────────────┘

         ┌─────────────────────────┐
         │ files_staleness_review  │ ──▶ surfaces mtime/sha drift,
         │ (add-on helper)         │     missing files, new files,
         └─────────────────────────┘     promoted-fact impact
```

The dashed boundary between `files.db` and `memory.db` is crossed only at
two points: **entity references** (every fact in `files.db` points to an
entity in `memory.db`) and **promotion** (a memory copied from `files.db`
into `memory.db` with a back-pointer). All other queries respect the boundary.

---

## 3. Three-store separation, justified

m3-memory currently uses two physical stores: `chatlog.db` (high-volume,
append-only conversational turns, lifecycle: decay + selective promotion)
and `memory.db` (curated, low-to-medium volume, lifecycle: indefinite).

This plan adds **`files.db`** as a third physical store with its own
lifecycle: **high-volume, regeneratable, version-tracked, promotable**.

### Why split, not merge

| Concern               | `chatlog.db`        | `memory.db`            | `files.db`             |
|-----------------------|---------------------|------------------------|------------------------|
| Volume                | very high           | low–medium             | high (bulk)            |
| Write pattern         | append-only firehose| curated, intermittent  | bursty per ingest run  |
| Volatility            | append-only         | curated                | re-ingestable          |
| Retention             | decay + promote     | indefinite             | tied to source         |
| Default search scope  | off (opt-in)        | on                     | on (with current filter)|
| Backup priority       | medium              | irreplaceable          | low (regeneratable)    |
| Maintenance cadence   | continuous          | rare                   | per-ingest             |

Merging `files.db` into `memory.db` would:

- **Dilute curated search.** A `memory_search` for "auth middleware decision"
  shouldn't surface 800 leaves from `auth_spec.pdf`.
- **Mix retention models.** Curated memories must never be lost; file leaves
  can be wiped and re-ingested freely. Different vacuum, rebuild, dedup
  schedules.
- **Inflate backup cost.** `memory.db` deserves frequent backup; `files.db`
  does not (it's regeneratable from disk).
- **Confuse scope rules.** Files are often per-project; core memories are
  cross-context. Separation makes scoping obvious.

This matches the production-RAG consensus: separate stores by **lifecycle**,
not by tenant. (See research §15: Tarun Jain, VerticalServe — multi-tenancy
should be intra-store via filtering; inter-store separation should be
justified by lifecycle, which is exactly the case here.)

### What stays in `memory.db`

- All entities (the shared connective tissue across stores). Entities are
  curated knowledge; they have the same lifecycle as core memories. See §8.
- All promoted-from-files items (facts, leaves, summaries) — they become
  copies in `memory.db` with metadata pointing back to `files.db`.

### Logical schema is not physical schema

Entities live in `memory.db` but are accessed only through `entity_*` tools.
That abstraction means the entity store can be split out later (if volume
forces it — see §8 triggers) without touching consumers. Build the API
boundary now; defer the physical split until evidence demands it.

---

## 4. The file-node tree

Each ingested file becomes a small **tree of memories** in `files.db`,
connected by `memory_link` edges. The tree has three branches off the root:
**metadata** (filesystem truth), **ingestions** (append-only run history),
and **content** (the mined payload).

```
file_node  (type: reference, the "this file exists" root)
│
├── metadata/                        ← stable, filesystem truth
│   ├── filename
│   ├── filetype / mime
│   ├── path_absolute
│   ├── path_repo_relative
│   ├── size_bytes
│   ├── content_sha256
│   ├── date_created   (fs ctime / birthtime where supported)
│   ├── date_modified  (fs mtime)
│   ├── source_host    (which machine / mount)
│   ├── identity_key   (see §5 — usually = path, override via doc_id)
│   ├── version_label  (ingester-assigned ordinal OR user override)
│   ├── superseded_by  (UUID of newer file_node, NULL while current)
│   ├── superseded_at  (timestamp)
│   ├── supersession_reason
│   ├── supersedes     (UUID of older file_node, NULL if first version)
│   └── paths_seen[]   (history of paths the same content has lived at)
│
├── ingestions/                      ← one BRANCH per run
│   ├── run_<ts>_<short_id>/
│   │   ├── ingest_date
│   │   ├── ingester_version
│   │   ├── chunker_version
│   │   ├── extractor_version
│   │   ├── extract_mode  (inline | queue | none)
│   │   ├── model_id      (which LLM did extraction, if any)
│   │   ├── chunk_count
│   │   ├── leaf_count
│   │   ├── fact_count
│   │   ├── duration_ms
│   │   ├── file_summary  (LLM-written, 1–3 sentences, embedded)
│   │   └── status        (ok | partial | failed + reason)
│   └── run_<ts>_<short_id>/  …
│
└── content/                         ← the mined payload (NavLayer)
    ├── leaf: page 4         (type: snippet, embedded)
    │   ├── leaf_summary     (1–2 sentence summary of the leaf, embedded)
    │   ├── text             (raw chunk content)
    │   ├── char_range       (start, end in source)
    │   ├── division_type    (page | slide | heading | function | row_range | window)
    │   ├── division_id      (page number, slide index, heading anchor, etc.)
    │   ├── division_label   (human-readable: "Methods", "Slide 12", "fn parse_config")
    │   ├── superseded_by    (UUID of newer leaf, NULL while current)
    │   ├── evolved_from     (UUID of prior-version leaf, if cross-version edge — §5)
    │   ├── material_change  (bool, only meaningful if evolved_from set)
    │   └── linked from:
    │       ├── fact_<uuid> (type: fact, embedded)
    │       │   ├── statement (the claim)
    │       │   ├── source_span (char_start, char_end within leaf)
    │       │   ├── confidence
    │       │   └── entity_refs[] ──▶ memory.db entities
    │       └── fact_<uuid> …
    ├── leaf: page 5 …
    └── leaf: section "Methods" …
```

### Why three branches, not flattened

- **Metadata is filesystem truth.** It changes only when the file changes.
  Stable, never per-run.
- **Ingestions are runs.** Append a new branch each run; never overwrite.
  This is the only way to answer "what did we think this file said in
  April?" or "did our chunker upgrade change what we mine from this file?"
- **Content is the mined payload.** It's regenerated per run; old content
  subtrees stay linked to their ingestion record. Default search filters to
  current (non-superseded) leaves; history is preserved for diff/audit.

### Logical vs physical storage

The "tree" is a graph of memories connected by `memory_link` edges; not a
nested document. m3-memory's `memory_graph` tool already does the traversal.
Physically, each box is a row in the appropriate table:

- `file_node` — `type: reference` (one row per file version)
- `ingestions/run_*` — `type: migration-log` (one row per run)
- `content/leaf` — `type: snippet` (one row per leaf, embedded)
- `content/leaf/fact` — `type: fact` (one row per fact, embedded)
- `leaf_summary`, `file_summary` — `type: summary` (embedded; coarse divisions only)

Metadata fields on file_node live in its `metadata` JSON column — they
never change independently of the file_node identity.

### Coarse-vs-fine division: which leaves get a summary?

Memories for coarse divisions (page, slide, top-level heading); edge labels
only for fine ones (sub-heading, paragraph). Cuts row count without losing
the "show me page 4" query — that query hits leaves; sub-page resolution
hits the fact's `source_span`.

Why this matters: a 300-page PDF would generate 300+ leaf rows but 3000+ if
each paragraph got its own leaf. Bounded row growth is the goal.

---

## 5. Identity, versioning, supersession

The most consequential design decisions. Three intertwined problems:

1. **What makes two file_nodes "the same file across versions"?** (Identity)
2. **How do we re-ingest without destroying history?** (Versioning)
3. **How do we mark old content as no longer current?** (Supersession)

### Identity key

`identity_key` is the stable handle that links file_node versions of the
same logical document. Resolution order:

1. **Explicit `m3_doc_id`** in the file (YAML frontmatter for Markdown,
   header comment for code, custom property for office docs). If present,
   wins unconditionally. Best for files that move.
2. **Path** (default for files without explicit ID). Survives content
   changes, breaks on rename. Acceptable for v1.
3. **(Phase 3) Heuristic match** suggested by staleness review when an
   ingested file disappears and a new file with similar content appears
   nearby. Always user-confirmed; never automatic.

Path-based identity is the practical default. The 10% rename/move case gets
explicit `m3_doc_id`. **Never auto-merge identity by content similarity** —
false positives ("two similar readmes" → mistakenly chained version
history) are unrecoverable without audit.

### Versioning

When `files_ingest` is asked to process a path:

1. Compute `content_sha256`.
2. Resolve `identity_key`.
3. Lookup current (non-superseded) file_node with this `identity_key`.
4. Branch:
   - **No prior version** → create new file_node v1, normal ingest.
   - **Prior version, same sha256** → no-op. Optionally append an
     ingestion record with status `unchanged_skipped` (off by default;
     toggled by `--record-noops`).
   - **Prior version, different sha256** → create new file_node v(N+1),
     supersede the prior (see below), run normal ingest, optionally
     run leaf carry-forward.

### File-level supersession

When v(N+1) supersedes v(N):

1. v(N)'s file_node row updates: `superseded_by = <v(N+1) uuid>`,
   `superseded_at = now()`, `supersession_reason = "content_changed"`.
2. v(N)'s `ingestions/` and `content/` branches stay intact — **no
   deletion**. Default search filters them out via "file_node not
   superseded" but they are still queryable with `--include-history`.
3. v(N+1)'s file_node carries `supersedes = <v(N) uuid>` (back-pointer).
4. v(N+1) carries forward `paths_seen[]` plus its own current path
   (so cross-version rename is visible).
5. Any **promoted-to-core-memory** items derived from v(N) get a metadata
   update: `source_file_superseded_at = now()`,
   `latest_source_version = <v(N+1) uuid>`. They are surfaced by
   `files_promotion_review` (§9) for human reconciliation. They are **not**
   auto-deleted or auto-updated — promotion is a deliberate freeze.

### Leaf-level supersession + carry-forward

Within a version transition, leaves are handled per their content-hash
relationship to the prior version:

- **Identical (`content_sha256` matches old leaf):** carry-forward. The new
  file_node's content branch references the **old leaf UUID** (re-use, do
  not duplicate). Old leaf is **not** superseded — it's the canonical
  storage for that text. Reuse saves embeddings and re-extraction cost.
- **Evolved (content overlaps, hash differs):** new leaf written fresh;
  carries `evolved_from = <old leaf uuid>`. Old leaf is marked
  `superseded_by = <new leaf uuid>`. Material-change flag set by the
  extractor (LLM judges "substance changed" vs "cosmetic edits") — cheap to
  add, useful for filtering diff views.
- **Replaced (no clear predecessor):** new leaf written fresh, no edge.
- **Dropped (old leaf has no successor):** old leaf is superseded by its
  file_node being superseded; no extra action.
- **New (no predecessor):** new leaf, no edge.

The carry-forward optimization is the practical win — a 200-page PDF where
198 pages didn't change between versions only re-embeds and re-extracts the
two changed pages. Detect by hashing **at the leaf level**, before
re-chunking the rest, by comparing the new file's would-be leaves against
the prior version's leaf hashes.

### Fact-level supersession across file versions

Don't auto-supersede. Two facts about "the same thing" across file versions
("ctx defaults to 4096" → "ctx defaults to 8192") stay as **two separate
facts**, each linked to its own file_node version. Default search filters
to current (non-superseded) file_nodes, so only the new fact surfaces.
Users wanting history pass `--include-history`.

Rationale: cross-version fact equivalence is an LLM-judgment problem and
the cost of wrong auto-merges (lost historical fact) exceeds the cost of
having both ("noise" in history-included queries — filterable).

### Version labels

Ingester-assigned ordinal by default: `ingest-1`, `ingest-2`, … Override
via `--version-label v1.2` (CLI flag) or `m3_version: v1.2` in frontmatter.
Never inferred from filename or content — too error-prone.

### Manual supersession

`files_supersede <old_uuid> <new_uuid> [--reason ...]` for cases the
auto-detector misses (e.g. two files the user knows are the same logical
doc but identity_key didn't catch it). User-driven, audited via
`supersession_reason = "manual"`.

---

## 6. Chunking dispatcher (per filetype)

One chunker per filetype, dispatched by MIME / extension / sniff. This is
the production-RAG consensus — there is no single chunker that handles
markdown + PDF + code + CSV well. (See research §15: Databricks, LangCopilot.)

### Dispatcher table

| Filetype          | Detector                | Chunker                          | Division type | Leaf size target |
|-------------------|-------------------------|----------------------------------|---------------|------------------|
| Markdown / RST    | extension + frontmatter | heading-tree splitter            | heading       | 256–1024 tokens  |
| PDF               | extension + magic       | page-aware extractor (PyMuPDF)   | page          | 1 leaf / page    |
| Slides (PPT/PPTX) | extension               | per-slide text+notes extractor   | slide         | 1 leaf / slide   |
| Notebooks (.ipynb)| extension               | per-cell with output             | cell          | 1 leaf / cell    |
| Python / TS / JS  | extension + sniff       | tree-sitter function/class split | function      | 1 leaf / function or module-block |
| Rust / Go / Java  | extension + sniff       | tree-sitter function/class split | function      | 1 leaf / function |
| TOML / YAML / JSON| extension               | structural splitter (top-level keys) | section   | 1 leaf / top-level key |
| CSV / TSV         | extension + sniff       | row-range chunker (header + rows)| row_range     | 50–200 rows / leaf |
| Plain text        | fallback                | semantic chunker (paragraph + similarity merge) | window | 256–512 tokens |
| Office (DOCX)     | extension               | heading-tree splitter (via docx2txt) | heading   | 256–1024 tokens  |
| HTML / EPUB       | extension               | heading-tree splitter after de-chrome | heading | 256–1024 tokens  |
| Images            | mime                    | (phase 3: OCR + caption) | —             | — |
| Audio / video     | mime                    | (phase 3: transcript)    | —             | — |

### Common chunker contract

Every chunker implements:

```python
def chunk(path: Path, content: bytes) -> Iterator[Leaf]:
    """
    Yields Leaf objects. Each Leaf has:
      text, division_type, division_id, division_label,
      char_range, optional sub_division (heading-of-heading, etc.)
    """
```

Plus a per-chunker version constant (`MARKDOWN_CHUNKER_VERSION = 3`) that
lands in the ingestion record's `chunker_version`. Bumping it forces
re-ingest on next staleness review.

### Hybrid content-type handling within a file

The Markdown chunker preserves code fences intact (don't split a code block
across leaves). The PDF chunker preserves tables with their captions. This
is **per-chunker logic**, not a separate concern — each chunker knows its
filetype's pathological cases.

### Boundary detection

The biggest source of accuracy regression in hierarchical chunking is wrong
boundary detection. (See research §15: RAGAboutIt.) Mitigation:

- Each leaf carries `boundary_confidence` (heuristic: high for structural
  splits like headings, lower for semantic-merge windows).
- The staleness/diagnostic tool surfaces leaves with low confidence so they
  can be inspected.
- The chunker dispatcher logs every boundary decision to `ingester_log` —
  debuggable without re-ingest.

### Size guards

- File size cap (default 10 MB, override `--force` per-file).
- Per-leaf token cap (truncate + log warning at 8192 tokens — leaf still
  ingests but with `truncated=true`).
- Per-ingest file count cap (default 10k files, override `--no-cap`).

---

## 7. Extraction pipeline

After chunking, each leaf may be **extracted** — i.e., the LLM mines
discrete facts from the leaf text. This is optional and per-mode:

### Modes

1. **`none`** — no extraction. Leaves stored with embeddings + summaries;
   no fact rows. Cheapest, best for raw-text corpora where the leaf itself
   is the unit of retrieval.
2. **`inline`** — synchronous LLM call per leaf. Slow but immediate. Default
   for small ingests (< 50 files OR `--mode inline`).
3. **`queue`** — leaves are written, facts deferred to the existing
   `extract_pending` → `enrich_pending` pipeline (the same path chat-log
   capture uses). Walk finishes fast; facts trickle in async. Default for
   large ingests.

### Extraction prompt (phase 1, simple)

Reuse the existing `extract_pending` prompt with a per-leaf preamble:
"This text is from file X, division Y, version Z." That single line is
enough to anchor extractions to provenance.

Phase 2 adds a per-filetype prompt variant — code files get
"function-purpose, exported API, gotchas"; PDFs get "claims, definitions,
numerical facts."

### File-level summary

Always written, regardless of extraction mode. Cheap (one LLM call per
file), high value (the wiki-index pattern lives or dies on these).

Prompt: "In 1–3 sentences, summarize this file in a way that helps an LLM
decide whether to read its full contents to answer a question. Include
filetype, topic, and any standout numbers or names."

Embedded; searched first in summary-first queries (§10).

### Leaf-level summary

Written only for coarse divisions (page, slide, top-level heading).
Optional for fine divisions (skip by default to bound row count).

### Backpressure and error handling

- LLM call failures: leaf written with `extraction_status = "failed"`,
  reason logged. `files_staleness_review` can target failed-extraction
  leaves with `--retry-failed`.
- Embedding failures: leaf written with `embedded = false`, retryable
  later. Search must filter on `embedded = true` for the vector channel
  (FTS5 still works).
- Mid-ingest crash: ingestion record marked `status = "partial"`. The
  walker is **idempotent** — rerunning with the same path picks up where
  it left off (already-ingested leaves skipped by content hash).

---

## 8. Entity graph integration

Entities are the cross-store connective tissue. They live in `memory.db`
and are referenced (not duplicated) from `files.db`.

### Why entities stay in `memory.db`

- Entities ARE curated knowledge. "bge-m3 is a BGE embedding model" is a
  durable `knowledge` memory by lifecycle, not a regeneratable artifact.
- Promoting an entity is a no-op — it's already in the curated store.
- Every fact, leaf, chatlog turn links to entities. Making that link
  cross-DB inverts the cost model: pay the join tax on every retrieval.
- The existing `entity_*` tools (search, get, link, merge) are the
  abstraction boundary; physical split can come later if volume forces it.

### Triggers for a future split

If any of these become true, revisit a standalone `entities.db`:

- Entity count > 50k with embeddings.
- Entity write QPS during bulk ingest measurably slows interactive
  `memory_search`.
- A second consumer of the entity graph appears (UI, another agent system)
  that wants direct access without touching core memory.

None of these are true today. Build the access path clean; defer the split.

### Linking from files.db to memory.db entities

Each `fact` row in `files.db` carries `entity_refs[]` — a list of entity
UUIDs (which live in `memory.db`). At query time:

1. `files_search` returns facts with their entity_refs as inline data.
2. To expand ("show me all files mentioning entity X"), call
   `entity_get(X)` for the entity row, then `files_search` filtered by
   `entity_refs contains X`.
3. Cross-store graph traversal is implemented in a single tool —
   `memory_graph_multi_db` — that knows to dispatch by ref. This is the
   only piece of code that needs cross-DB awareness; the rest of the
   system treats each DB as local.

### Entity creation during ingest

The extractor produces facts plus a candidate entity list. For each
candidate:

1. `entity_search(name, aliases)` — exact + fuzzy + alias match.
2. If high-confidence match: link the fact to the existing entity UUID.
3. If no match: create a new entity in `memory.db` (`type: entity`) with
   the candidate name + provisional aliases. Mark `provisional = true`
   so dedup tools can review.
4. If multiple low-confidence matches: link the fact to **all** candidates
   with `confidence < 1.0`; `entity_merge` can later resolve.

Provisional entities are deduplicated in batches via `memory_dedup` —
exactly the same path that already exists for memory dedup.

---

## 9. Ascension — promotion to core memory

Selected items from `files.db` (facts, leaves, summaries) can be promoted
into `memory.db` as curated core memories. This is the **rare exception**,
not the default flow.

### What promotion is

A **copy** from `files.db` into `memory.db` with metadata back-pointing to
the source. The original stays in `files.db` untouched.

Promotion operation (`files_promote <source_memory_id>`):

1. Read the source memory from `files.db`.
2. Map type if needed: `snippet` → `knowledge` or `reference`; `fact` →
   `fact`; file summary → `reference`.
3. Write a new row in `memory.db` with the mapped content, plus metadata:
   ```json
   {
     "promoted_from": "files.db",
     "source_memory_id": "<uuid in files.db>",
     "source_file_node": "<file_node uuid>",
     "source_path": "<absolute path at time of promotion>",
     "source_version_label": "<e.g. ingest-3>",
     "promoted_at": "<timestamp>",
     "promoted_by": "<agent id>",
     "promotion_reason": "<user-supplied string>"
   }
   ```
4. The source in `files.db` is **not** modified, but a sibling
   `promotion_marker` row is added linking it to the promoted core memory
   (for "what have I promoted from this file?" queries).
5. The promoted core memory is now curated, embedded in the core search
   namespace, and subject to all the normal core-memory tools.

### What promotion is NOT

- Not a move (the source stays in `files.db`).
- Not a live link (the promoted memory is frozen — see drift, below).
- Not bidirectional (the source can be re-ingested freely; the promoted
  memory does not auto-update).

### Drift between promoted memory and source

This is the consequential semantics. When the source file is re-ingested
and its file_node is superseded:

1. The promoted memory's metadata is updated:
   `source_file_superseded_at = now()`,
   `latest_source_version = <new file_node uuid>`.
2. The promoted memory is **not** auto-modified.
3. `files_promotion_review` surfaces it as a review candidate.

`files_promotion_review` is an interactive tool that walks promoted
memories whose source has been superseded and offers, per item:

- **Keep as-is** — the promoted memory is still true; the source moving on
  doesn't change the curated belief.
- **Refresh** — replace the promoted memory's content with the equivalent
  fact from the new source version (the user picks which new-version fact
  is "the same fact").
- **Retire** — supersede the promoted memory in `memory.db`. The retired
  memory stays (history) but is filtered from default search.

This is the moment ascension pays off: promoted facts that drift from
their source surface deliberately, not silently.

### Promotion triggers

Three, in increasing automation (and decreasing prevalence):

1. **Manual.** User reads a search result, says "this is core." Tool:
   `files_promote(source_uuid, reason="...")`. **The right default.**
2. **Heuristic suggestion.** Ranker notices: a file fact has been cited in
   N conversations, scored highly K times, lives in a user-flagged
   directory. Tool **suggests** promotion in search results; never
   auto-promotes.
3. **Ingest-tagged.** CLI flag `--ascend file-summaries` (or similar) says
   "for this ingest, promote every file's top-level summary to a
   `reference`." Useful for one-time index of a personal corpus. Off by
   default. Auditable — every auto-promoted item carries
   `promotion_reason = "ingest-tagged:<flag>"`.

The bar for auto-promotion to core memory is much higher than people
realize. A wrong auto-promotion contaminates the curated store; the
curated store's value comes from being curated. Default to manual,
suggest-only for heuristics.

---

## 10. Tools and CLI surface

All tools are MCP-callable and CLI-invokable. Conventions match existing
m3-memory tools (`memory_search`, `memory_write`, etc.).

### Core ingest

- `files_ingest(path, [--mode inline|queue|none], [--scope agent|user|global],
  [--include glob], [--exclude glob], [--max-depth N], [--dry-run], [--resume <run_id>],
  [--ascend <kind>], [--version-label <label>], [--corpus <id>], [--no-cap], [--force])`
  Walks the path, ingests files, returns a run summary.
- `files_ingest_status(run_id)` — progress + counts + failures for an in-flight or completed run.

### Search and read

- `files_search(query, [--corpus], [--filetype], [--division], [--include-history],
  [--limit], [--rerank])`
  Hybrid (FTS5 + vector + MMR) search over `files.db`. Default filters to
  non-superseded file_nodes and non-superseded leaves.
- `files_index([--corpus], [--directory], [--filter])`
  Returns **summaries only** — file_node + file_summary. This is the
  wiki-index primitive: cheap-first triage. See §10.1.
- `files_get(uuid)` — fetch one file_node, leaf, or fact.
- `files_graph(uuid, [--depth N])` — traverse the local graph from a node
  (file → ingestions, file → content → leaves, leaf → facts → entities).
- `memory_search_multi_db(query, stores=[memory, files, chatlog])`
  Cross-store search with reranking. Default for assistant retrieval once
  `files.db` exists.

### Maintenance

- `files_staleness_review([--directory], [--include-touched-only],
  [--auto-reingest-changed], [--auto-skip-touched], [--review-promoted],
  [--report-only], [--json])`
  Add-on helper. See §10.2.
- `files_promotion_review([--auto-keep], [--report-only])`
  Surfaces promoted core memories whose source has been superseded.
- `files_supersede(old_uuid, new_uuid, [--reason])` — manual supersession.
- `files_reingest(file_node_uuid_or_path, [--force])` — re-ingest a single
  file. Useful for targeted refresh.
- `files_delete(uuid, [--cascade])` — hard delete. **Discouraged.** Default
  is supersede, not delete. Cascade required for tree deletion.

### Ascension

- `files_promote(source_uuid, [--reason], [--mapped-type])` — copy from
  `files.db` to `memory.db`.
- `files_promotion_list([--source-superseded], [--by-source-file])` — list
  promoted items, optionally filtered by drift state.

### Diagnostics

- `files_stats([--corpus])` — row counts, embedding coverage, supersession
  rate, recent-run summary.
- `files_health()` — DB integrity, FTS5 freshness, embedding queue depth.
- `files_eval(eval_set_path)` — run an eval set (Q-A pairs) against
  current corpus state. See §12.

### 10.1. The summary-first read path

Karpathy's LLM-wiki insight is that **the LLM doesn't search the full
corpus by default — it reads an index of summaries first, picks the
relevant 3–5, then reads only those.** This is dramatically more
token-efficient than embed-search-everything.

`files_index` returns:
```json
[
  {
    "file_node": "<uuid>",
    "filename": "EMBED_DEPLOYMENT.md",
    "path": "docs/EMBED_DEPLOYMENT.md",
    "filetype": "markdown",
    "version_label": "ingest-3",
    "date_modified": "2026-05-15",
    "summary": "Operator-facing guide for the m3 embedder stack: build, install, configure, troubleshoot. CUDA + CPU dual-path; in-process and HTTP fallback."
  },
  ...
]
```

~50 tokens per entry. For a 200-file corpus that's ~10k tokens total — one
LLM call to triage, then targeted `files_search` on the chosen subset.

The retrieval pattern becomes:

```
1. files_index(filter=...)                ← 10k tokens, one call
2. LLM picks 3-5 file_node UUIDs to read in depth
3. files_search(query, file_node IN (chosen))  ← targeted leaf retrieval
4. LLM synthesizes answer with provenance
```

This is the wiki pattern. Phase-1 must ship `files_index` as a first-class
tool — not just `files_search`.

### 10.2. The staleness helper, in detail

`files_staleness_review` walks the filesystem and `files.db`, computes
diffs, presents a triage list:

```
Stale files (mtime > last_ingestion_date AND content_sha256 changed):
  1. docs/EMBED_DEPLOYMENT.md
       last ingested: 2026-04-12 (ingest-2, sha 9a3f...)
       current mtime: 2026-05-15  (sha b7e1...)
       Δ: 33 days
       facts: 47 (3 promoted to core memory) ⚠
       suggested action: re-ingest, then promotion review

Touched-only (mtime bumped, content unchanged):
  2. docs/OXIDATION_TODO.md
       (mtime-only touch; no content change)
       suggested action: skip

Missing files (in files.db, no longer on disk):
  3. notes/old_plan.md
       last seen: 6 weeks ago
       facts: 12 (0 promoted)
       suggested action: mark file_node as retired (hides from default
       search; history preserved)

New files (on disk, never ingested):
  4. docs/NEW_FEATURE.md
  5. notes/SCRATCH.md
       suggested action: ingest

Failed extractions in last ingest:
  6. docs/BIG_PDF.pdf (ingest-3, status=partial, 4 leaves failed)
       suggested action: retry-failed
```

### Key behaviors

- **mtime is a hint, sha256 is truth.** First filter by mtime delta
  (cheap, eliminates the obvious majority), then recompute sha256 on
  candidates. Touched-only files are surfaced distinctly — don't re-ingest
  them by default.
- **Promotion impact is shown.** Re-ingesting a file with promoted facts
  is higher-stakes; the helper surfaces the count and offers an inline
  `files_promotion_review` flow after re-ingest completes.
- **Three run modes:**
  - Interactive (default) — TTY, per-file choices.
  - Report-only — Markdown / JSON dump; no actions.
  - Batch — `--auto-reingest-changed --auto-skip-touched --review-promoted`
    for scripting.
- **Watch mode (phase 4 candidate)** — runs as a daemon, surfaces a Discord
  notification when N files go stale. Not phase 1.

The helper is **separate from the core ingester** — different concerns,
different schedules, different failure-modes-that-are-OK.

---

## 11. Phasing

Each phase ships independently. Each is **gated by passing eval** (§12).

### Phase 1 — Walker, schema, summary-first index (minimum viable)

**Goal:** an LLM can ingest a directory, ask "what's in this corpus?", and
get sensible answers via the wiki-index pattern. No facts yet.

1. Create `files.db` with schema:
   - `file_nodes` (with `identity_key`, `content_sha256`, `version_label`,
     `superseded_by`, `superseded_at`, `supersession_reason`, `supersedes`,
     `paths_seen[]` from day 1 — cheap to add now, expensive to retrofit)
   - `ingestion_runs` (with `ingester_version`, `chunker_version`,
     `extractor_version`, `model_id` from day 1)
   - `leaves` (with `superseded_by`, `evolved_from`, `material_change`,
     `division_*` from day 1)
   - `leaf_edges` (parent/child/superseded_by/evolved_from)
   - FTS5 + sqlite-vec indexes
2. Walker (`files_ingest`) with:
   - ignore list (gitignore + built-in + `.m3ignore`)
   - size caps, mime filter, binary sniff
   - symlink policy (off by default)
   - `--max-depth`, `--include`, `--exclude`, `--dry-run`
3. Chunker dispatcher with **3 chunkers** to start:
   - Markdown (heading-tree)
   - PDF (page-aware via PyMuPDF)
   - Plain-text fallback (semantic paragraph + sentence-similarity)
4. File summary written for every file (always — wiki index needs it).
5. Leaf summaries for coarse leaves only.
6. **No fact extraction yet.** Extraction mode `none` is the only mode.
7. Tools: `files_ingest`, `files_index`, `files_search`, `files_get`,
   `files_stats`, `files_health`.
8. Default search filters non-superseded. `--include-history` opts in.
9. Identity = path. Re-ingest of changed content → file-level supersession
   active from day 1.
10. Eval harness (§12) with ≥ 20 hand-written Q-A pairs against a fixed
    test corpus. CI runs eval after every chunker change.

**Acceptance gate:** assistant can triage a 100-file corpus via
`files_index` and answer 80% of eval Q-A pairs correctly using
`files_search` targeted by index triage.

### Phase 2 — Fact extraction, ascension, staleness helper

**Goal:** an LLM can mine facts from ingested files, promote selected
facts to core memory, and the user can see what's worth re-ingesting.

1. Fact extraction:
   - `inline` mode (sync per-leaf).
   - `queue` mode (reuses `extract_pending` + `enrich_pending`).
   - Facts carry `source_span`, `confidence`, `entity_refs[]`.
2. Entity graph integration:
   - Extractor produces candidate entities.
   - `entity_search` + `entity_link` + provisional entity creation in
     `memory.db`.
   - Provisional entities surface in `memory_dedup` for review.
3. Add per-filetype chunkers:
   - PPT/PPTX (per-slide), Jupyter (per-cell), Python (tree-sitter
     function/class), TOML/YAML/JSON (structural).
4. **Ascension:**
   - `files_promote` (manual).
   - `promotion_marker` rows in `files.db`.
   - `memory_search_multi_db` extended to include `files`.
5. **Staleness helper:**
   - `files_staleness_review` (interactive + report-only modes).
   - `files_promotion_review` (interactive).
6. **Carry-forward optimization:**
   - On supersession, unchanged leaves are reused (not re-embedded).
   - Changed leaves get `evolved_from` edges.
7. Eval expansion: ≥ 100 Q-A pairs; per-filetype subsets.

**Acceptance gate:** end-to-end ingest → search → promote → re-ingest →
promotion review works on a 500-file corpus. Carry-forward measurably
reduces re-ingest time (≥ 60% leaves reused for cosmetic edits).

### Phase 3 — Diff, drift, heuristic promotion suggestions

**Goal:** the system actively maintains corpus coherence over time.

1. Cross-version leaf diff:
   - `material_change` flag set by LLM judgment.
   - `files_diff(old_file_node, new_file_node)` tool.
2. Heuristic promotion suggestions:
   - Ranker tracks fact usage (cite count, search rank, conversation
     mentions).
   - Search results surface "this looks promotable" inline.
3. Near-duplicate detection at corpus level:
   - `files_dedup` finds near-duplicate leaves across files via
     embedding similarity.
   - Two-phase: exact-hash drop at ingest (phase 1), semantic near-dup
     surface for review (phase 3).
4. Identity heuristic for renames:
   - Staleness review suggests "file X looks like a rename of file Y,
     link?" when an ingested file disappears and a new file with similar
     content appears nearby. Always user-confirmed.
5. Image OCR / caption (mime-detected images get an OCR pass + LLM
   caption, stored as a leaf).
6. Audio/video transcript ingest (whisper or equivalent).

**Acceptance gate:** corpus drift is detectable and actionable; user can
run `files_staleness_review` after a month of edits and reconcile in < 10
minutes for a 1000-file corpus.

### Phase 4 — Daemon, watch mode, multi-corpus

**Goal:** unobtrusive, always-on.

1. Watch-mode staleness daemon (Discord notifications).
2. Multi-corpus isolation:
   - One `files.db` global, `corpus_id` column on file_nodes.
   - Default search is current-corpus; cross-corpus is explicit.
3. Cross-host sync (deferred — needs serious thought; not in scope here).

---

## 12. Operational concerns

### Eval harness (must exist from phase 1)

A held-out set of Q-A pairs against a fixed test corpus. Runs after every
chunker change, every extractor change, every supersession-logic change.
Reports per-filetype + overall accuracy.

Without this, "we improved chunking" is a vibe, not a measurement.
(Research §15: Towards Data Science — production RAG demands evaluation
after every corpus change.)

Tool: `files_eval(eval_set_path)`. CI gate before merging any chunker
change.

### Versioning of pipeline components

Every leaf and ingestion record carries:
- `ingester_version` (the walker / orchestrator)
- `chunker_version` (the dispatcher; bumps when ANY chunker bumps)
- `extractor_version` (the extraction prompt + model)
- `model_id` (the LLM)

Staleness review can target files ingested under stale versions
(`--ingester-older-than v2`).

### Backup posture

| Store        | Backup priority | Frequency       | Rationale                  |
|--------------|-----------------|-----------------|----------------------------|
| `memory.db`  | irreplaceable   | nightly + sync  | core curated knowledge     |
| `chatlog.db` | medium          | weekly          | promotable but voluminous  |
| `files.db`   | low             | optional        | regeneratable from disk    |

If `files.db` is lost, `files_ingest <root>` rebuilds it (modulo entity
links — see below).

### Recovery from `files.db` loss

`files.db` is regeneratable from the filesystem **except** for two things:

1. **Entity links.** Facts in `files.db` link to entities in `memory.db`.
   On re-ingest, the extractor will re-link, but the link UUIDs differ.
   Acceptable — entities are the durable identifiers; specific
   fact→entity edge UUIDs are not.
2. **Promotion markers.** The links from `files.db` items to their
   promoted-to-core-memory copies. On re-ingest, the promoted core
   memories still exist (they're in `memory.db`), but the back-pointer
   from `files.db` is lost. **Mitigation:** every promoted core memory
   carries `source_path` + `source_version_label`; `files_promotion_repair`
   (phase 2) walks `memory.db` and re-creates `promotion_marker` rows in
   `files.db` by matching paths.

### Resource budgets

- **Embedding throughput.** In-process CUDA path (per
  `EMBED_DEPLOYMENT.md`) handles ~hundreds of embeddings/sec on RTX 5080.
  A 1k-file corpus with avg 20 leaves/file = 20k embeddings ≈ 1–2 minutes.
  Acceptable.
- **LLM extraction.** At ~1 leaf/sec for inline mode, a 1k-file corpus =
  several hours. Queue mode handles this async; the walk itself is
  minutes.
- **Disk.** Full text duplicated from filesystem into `files.db`. For
  personal corpora (≤ 10 GB) this is acceptable. For huge corpora, a
  future `--text-by-reference` mode could store `(path, char_range)` and
  lazy-load — out of scope here.

### Idempotency

Every operation is idempotent:
- `files_ingest` of an unchanged file: no-op.
- `files_ingest` of a changed file: new version, prior preserved.
- `files_promote` of an already-promoted item: returns existing core
  memory UUID, no duplicate.
- Mid-ingest crash: rerun picks up where it left off (leaf-level resume
  via content hash dedup).

### Observability

- Per-ingest summary: counts, durations, failures, per-filetype breakdown.
- `files_stats` for corpus-wide health.
- Structured logs for each chunker decision (boundary detection in
  particular — see §6).
- Ingest events surfacable via existing notification path.

---

## 13. Failure modes and resilience

A taxonomy of how this pipeline can fail, with explicit handling for each.

### File-level failures

| Failure                          | Detection           | Handling                                       |
|----------------------------------|---------------------|------------------------------------------------|
| File unreadable (permissions)    | os.open raises      | Skip + log; ingestion record `partial`.        |
| File too large                   | size check          | Skip unless `--force`; log warning.            |
| File is binary, sniff missed it  | post-read entropy   | Skip + log; suggest mime filter.               |
| File is empty                    | size = 0            | Skip silently.                                 |
| File mid-write (truncated)       | hash mismatch on retry | Retry once; if still inconsistent, skip + log. |
| Encoding errors                  | utf-8 decode raises | Fall back to chardet; if still fails, skip.    |

### Chunker failures

| Failure                          | Detection                | Handling                                  |
|----------------------------------|--------------------------|-------------------------------------------|
| Chunker raises mid-file          | wrapped try/except       | Skip the file; ingestion record `failed`. |
| Chunker produces 0 leaves        | empty iterator           | Skip + log; surfaces in staleness review. |
| Chunker produces oversize leaf   | token count check        | Truncate + flag `truncated=true`.         |
| PyMuPDF / dependency missing     | import error             | Disable chunker; log; suggest install.    |

### Extraction failures

| Failure                          | Detection           | Handling                                       |
|----------------------------------|---------------------|------------------------------------------------|
| LLM call timeout                 | timeout              | Retry once with backoff; mark `failed` after. |
| LLM returns malformed JSON       | parse error          | Retry once; mark `failed` after.              |
| Embedding call timeout           | timeout              | Leaf written `embedded=false`; retryable.     |
| Entity service unavailable       | tool error           | Fact written with empty `entity_refs[]`; backfilled later. |
| OOM during batch embed           | resource error       | Reduce batch size; retry.                     |

### Storage failures

| Failure                          | Detection           | Handling                                       |
|----------------------------------|---------------------|------------------------------------------------|
| `files.db` locked                | sqlite busy          | Retry with jittered backoff (existing pattern).|
| `files.db` corrupt               | integrity check      | Halt ingest; surface in `files_health`; offer rebuild. |
| Disk full                        | write fails          | Halt ingest; mark run `failed`; preserve partial state. |
| FTS5 index out of sync           | `files_health` scan  | Rebuild FTS5 indexes (existing m3 tooling).   |

### Logical failures

| Failure                          | Detection           | Handling                                       |
|----------------------------------|---------------------|------------------------------------------------|
| identity_key collision (two files claim same `m3_doc_id`) | on ingest | Refuse; surface to user with both paths.|
| Cross-version supersede loop     | on supersede        | Refuse cycle; log.                            |
| Promoted memory points at deleted source | on promotion_review | Surface as orphan; offer retire/keep.    |
| Entity merge across stores fails | on `entity_merge`   | Existing dedup tooling handles; flag if cross-store. |

### Concurrency

- **Single-writer per `files.db`.** Multi-process ingest is not supported
  in phase 1 — would require a coordinator. (Phase 4 if needed.)
- **Many-reader.** Search / index reads work concurrently with ingest;
  sqlite WAL mode handles this.
- **Mid-ingest crash recovery:** ingestion run marked `partial` on
  ungraceful exit (lockfile + atexit handler); next ingest of same path
  picks up via leaf-level content-hash dedup.

### Data integrity invariants

The ingester upholds these invariants; `files_health` checks them:

1. Every leaf belongs to exactly one file_node.
2. Every file_node has at least one ingestion_run (else orphaned).
3. `superseded_by` is acyclic.
4. `evolved_from` is acyclic.
5. Every fact's `source_span` is within its leaf's `char_range`.
6. Every fact's `entity_refs[]` resolves to a row in `memory.db`
   (dangling refs surface as warnings, not errors — entity may be merged).
7. Every promoted core memory in `memory.db` has a matching
   `promotion_marker` in `files.db` (else surface in
   `files_promotion_repair`).

---

## 14. Open questions to resolve before phase 1 lands

A short list. Each blocks something concrete; each has a recommended
default.

### Q1. Identity key default

**Question:** Path-based identity (simple, breaks on rename) or always-
require-`m3_doc_id` (robust, friction)?

**Recommendation:** Path-based default; `m3_doc_id` opt-in via frontmatter
or header comment. Phase-3 rename heuristic surfaces missed cases.

### Q2. Default extraction mode

**Question:** `none` / `inline` / `queue` as the day-one default?

**Recommendation:** `none` for phase 1 (extraction lands in phase 2).
Once phase 2 is in: `queue` if file count > 50, `inline` otherwise.

### Q3. Default scope

**Question:** `agent` (private) or `user` (cross-agent)?

**Recommendation:** `user` — file content is generally personal-knowledge
shaped, not agent-private. Override per-ingest with `--scope`.

### Q4. Default search behavior

**Question:** Does the assistant's normal `memory_search` include
`files.db` by default once it exists, or does it stay opt-in via
`memory_search_multi_db`?

**Recommendation:** Opt-in for phase 1 (don't change existing behavior).
**Default-on for phase 2** once eval shows it doesn't degrade
core-memory-only searches. Concretely: `memory_search` becomes
`memory_search_multi_db(stores=[memory, files], rerank=true)` under the
hood, with `memory` ranked higher.

### Q5. Per-project vs global `files.db`

**Question:** One `files.db` for everything, or one per project?

**Recommendation:** **Global** at
`~/.claude/projects/.../files.db`, with a `corpus_id` field that allows
per-project filtering at query time. This matches the production-vector-DB
consensus (separate collections in one store, not one DB per tenant). A
per-project override is possible via env var
(`M3_FILES_DB_PATH=<repo>/.m3/files.db`) for users who want isolation.

### Q6. Version label default

**Question:** Infer from mtime / git tag / sha, or use ingester ordinal?

**Recommendation:** Ingester-assigned ordinal (`ingest-1`, `ingest-2`).
Override via `--version-label` or frontmatter. Never inferred — too easy
to get wrong.

### Q7. Promotion type mapping

**Question:** When promoting a `snippet` from `files.db`, does it become
a `knowledge`, a `reference`, or stay a `snippet` in `memory.db`?

**Recommendation:** Default to **`knowledge`** for snippets being promoted
as substantive content; **`reference`** only for file-summary promotions
("this file exists and is about X"). User overrides via `--mapped-type`.

### Q8. Leaf-summary embedding

**Question:** Embed leaf summaries AS WELL AS leaf text (double the
embedding cost on coarse leaves), or text only?

**Recommendation:** **Embed both.** Summary embeddings retrieve "this leaf
is about X"; text embeddings retrieve "this leaf contains the exact
phrase X." Different recall profiles; both matter. Cost is bounded
because only coarse leaves get summaries.

### Q9. Ingestion failure: partial keep or full rollback?

**Question:** If 90% of a file ingests but the last leaf crashes, do we
keep the partial result or roll back?

**Recommendation:** **Keep partial**, mark ingestion `status=partial`,
record failure reason. User can rerun ingest (idempotent), targeting the
failed leaf via staleness review's "retry-failed" mode. Rollback would
discard useful work and complicate recovery.

### Q10. Search-result format includes raw text or just spans?

**Question:** `files_search` returns full leaf text in results, or just
`(file_node, leaf_uuid, char_range)` for the caller to fetch?

**Recommendation:** Returns **leaf text + spans** by default (matches how
`memory_search` works), `--lazy` flag for spans-only for huge result
sets. This is the assistant's default consumer pattern.

---

## 15. Cross-references

### Within this repo

- `docs/ARCHITECTURE.md` — overall m3-memory architecture.
- `docs/EMBED_DEPLOYMENT.md` — embedder stack used for leaf + fact + entity
  embedding.
- `docs/EMBED_INPUT_RECIPE.md` — what text gets embedded (anchor
  augmentation, model-tag namespacing).
- `docs/MEMORY_ENTITY_EXTRACTION_PLAN.md` — existing entity-extraction
  pipeline this plan integrates with.
- `docs/M3_ENRICH_GUIDE.md` — `extract_pending` / `enrich_pending` paths
  the queue extraction mode reuses.
- `docs/CHATLOG.md` — chatlog-promote precedent for the ascension pattern.
- `docs/MCP_TOOLS.md` — tool conventions for new `files_*` tools.

### External research (consulted 2026-05-17)

Production RAG and chunking:
- Six Lessons Learned Building RAG Systems in Production — Towards Data Science
- Production RAG: Chunking, Retrieval, and Evaluation Strategies That Actually Work — Towards AI
- Inside a Production RAG System — DEV
- Chunking Strategies to Improve LLM RAG Pipeline Performance — Weaviate
- Document Chunking for RAG: 9 Strategies Tested — LangCopilot
- Hard-Won Lessons from 1200+ Hours of RAG Development — ByteVagabond
- The Ultimate Guide to Chunking Strategies for RAG Applications — Databricks
- Data Ingestion for RAG: An IBM Cookbook Guide — IBM

Hierarchical / parent-child chunking:
- The Hidden Architecture: Parent-Child Chunking — Sandgarden
- HiChunk: A Hierarchical Chunking Method — AI Innovations
- The Chunking Blind Spot — RAGAboutIt
- Mastering Document Chunking Strategies for RAG — Medium

GraphRAG / entity stores:
- Efficient Knowledge Graph Construction and Retrieval for Large-Scale RAG — arXiv 2507.03226
- Graph RAG Guide 2025: Architecture, Implementation & ROI — Salfati Group

Personal knowledge bases (Obsidian / NotebookLM / wiki):
- How to Build a Local LLM Knowledge Base With Obsidian — Modemguides
- Building an Obsidian RAG with DuckDB and MotherDuck — MotherDuck
- Karpathy's LLM Wiki gist
- Karpathy Named It. I Built One on My Notes — Decoding AI
- What Is Karpathy's LLM Wiki? — MindStudio

Vector-store partitioning:
- Should Each User Get Their Own Vector Database? — Tarun Jain
- GenAI Best Practices for Using Vector DB Collections — VerticalServe

Deduplication and re-ingestion:
- Deduplication of Graphitic RAG Evidence Segments in lucidRAG
- Designing RAG Architectures That Scale: Chunking, Deduplication, and Accuracy — Sabarish Kumar

---

## Appendix A — Concrete schema sketch (SQL-ish, not authoritative)

```sql
-- files.db schema (sketch; phase 1)

CREATE TABLE file_nodes (
  uuid                TEXT PRIMARY KEY,
  identity_key        TEXT NOT NULL,        -- usually = path
  filename            TEXT NOT NULL,
  filetype            TEXT NOT NULL,
  mime                TEXT,
  path_absolute       TEXT NOT NULL,
  path_repo_relative  TEXT,
  size_bytes          INTEGER NOT NULL,
  content_sha256      TEXT NOT NULL,
  date_created        TEXT,                 -- ISO 8601
  date_modified       TEXT NOT NULL,
  source_host         TEXT NOT NULL,
  version_label       TEXT NOT NULL,        -- e.g. "ingest-3"
  superseded_by       TEXT,                 -- UUID, NULL while current
  superseded_at       TEXT,
  supersession_reason TEXT,
  supersedes          TEXT,                 -- UUID, NULL if first version
  paths_seen          TEXT,                 -- JSON array
  corpus_id           TEXT NOT NULL DEFAULT 'default',
  created_at          TEXT NOT NULL,
  file_summary        TEXT NOT NULL,        -- the wiki-index summary
  file_summary_embed  BLOB,                 -- sqlite-vec
  metadata            TEXT NOT NULL         -- JSON, extensible
);

CREATE INDEX idx_file_nodes_identity ON file_nodes(identity_key, superseded_by);
CREATE INDEX idx_file_nodes_corpus ON file_nodes(corpus_id, superseded_by);
CREATE INDEX idx_file_nodes_sha ON file_nodes(content_sha256);

CREATE TABLE ingestion_runs (
  uuid               TEXT PRIMARY KEY,
  file_node          TEXT NOT NULL REFERENCES file_nodes(uuid),
  ingest_date        TEXT NOT NULL,
  ingester_version   TEXT NOT NULL,
  chunker_version    TEXT NOT NULL,
  extractor_version  TEXT,
  extract_mode       TEXT NOT NULL,        -- 'none' | 'inline' | 'queue'
  model_id           TEXT,
  chunk_count        INTEGER NOT NULL,
  leaf_count         INTEGER NOT NULL,
  fact_count         INTEGER NOT NULL DEFAULT 0,
  duration_ms        INTEGER NOT NULL,
  status             TEXT NOT NULL,         -- 'ok' | 'partial' | 'failed'
  status_reason      TEXT
);

CREATE INDEX idx_runs_file_node ON ingestion_runs(file_node, ingest_date);

CREATE TABLE leaves (
  uuid                TEXT PRIMARY KEY,
  file_node           TEXT NOT NULL REFERENCES file_nodes(uuid),
  ingestion_run       TEXT NOT NULL REFERENCES ingestion_runs(uuid),
  division_type       TEXT NOT NULL,        -- 'page' | 'slide' | 'heading' | ...
  division_id         TEXT NOT NULL,        -- "4", "slide-12", "intro/methods"
  division_label      TEXT,
  text                TEXT NOT NULL,
  text_sha256         TEXT NOT NULL,
  char_range_start    INTEGER NOT NULL,
  char_range_end      INTEGER NOT NULL,
  leaf_summary        TEXT,                  -- nullable; set for coarse leaves only
  text_embed          BLOB,                  -- sqlite-vec
  summary_embed       BLOB,                  -- sqlite-vec
  embedded            BOOLEAN NOT NULL DEFAULT 0,
  superseded_by       TEXT,                  -- UUID of newer leaf
  evolved_from        TEXT,                  -- cross-version edge
  material_change     BOOLEAN,
  boundary_confidence REAL,                  -- 0.0 - 1.0
  truncated           BOOLEAN NOT NULL DEFAULT 0,
  extraction_status   TEXT NOT NULL DEFAULT 'pending'  -- 'pending'|'ok'|'failed'|'skipped'
);

CREATE INDEX idx_leaves_file ON leaves(file_node, superseded_by);
CREATE INDEX idx_leaves_sha ON leaves(text_sha256);
CREATE VIRTUAL TABLE leaves_fts USING fts5(text, content='leaves', content_rowid='rowid');

CREATE TABLE facts (
  uuid              TEXT PRIMARY KEY,
  leaf              TEXT NOT NULL REFERENCES leaves(uuid),
  file_node         TEXT NOT NULL REFERENCES file_nodes(uuid),  -- denorm for filter
  statement         TEXT NOT NULL,
  source_span_start INTEGER NOT NULL,
  source_span_end   INTEGER NOT NULL,
  confidence        REAL NOT NULL,
  statement_embed   BLOB,                                       -- sqlite-vec
  superseded_by     TEXT,                                       -- intra-file only (rare)
  extraction_run    TEXT NOT NULL REFERENCES ingestion_runs(uuid)
);

CREATE INDEX idx_facts_leaf ON facts(leaf);
CREATE INDEX idx_facts_file ON facts(file_node, superseded_by);

CREATE TABLE fact_entity_refs (
  fact         TEXT NOT NULL REFERENCES facts(uuid),
  entity_uuid  TEXT NOT NULL,                                    -- lives in memory.db
  confidence   REAL NOT NULL,
  PRIMARY KEY (fact, entity_uuid)
);

CREATE TABLE promotion_markers (
  uuid                TEXT PRIMARY KEY,
  source_memory       TEXT NOT NULL,                             -- UUID in files.db
  source_memory_type  TEXT NOT NULL,                             -- 'fact'|'leaf'|'file_summary'
  promoted_to         TEXT NOT NULL,                             -- UUID in memory.db
  promoted_at         TEXT NOT NULL,
  promoted_by         TEXT NOT NULL,
  reason              TEXT
);

CREATE INDEX idx_promotion_source ON promotion_markers(source_memory);

CREATE TABLE memory_links (
  src_uuid    TEXT NOT NULL,
  dst_uuid    TEXT NOT NULL,
  edge_type   TEXT NOT NULL,                                    -- 'parent'|'evolved_from'|'supersedes'|...
  metadata    TEXT,                                              -- JSON
  PRIMARY KEY (src_uuid, dst_uuid, edge_type)
);
```

Not authoritative — schema will be refined when the migration lands. The
shape above is the working target.

---

## Appendix B — Quick-reference: what changed from earlier sketches

For readers following the design conversation, the consequential
late-stage decisions:

- **Three-store separation** (files / chatlog / memory) — confirmed; not
  per-corpus, not per-tenant. Lifecycle is the split axis.
- **Entities stay in `memory.db`.** Triggers for future split documented.
- **File-level supersession** added (was leaf-only earlier).
- **Carry-forward optimization** added (reuse unchanged leaves).
- **`files_index` is a first-class tool** — wiki-index pattern, not
  a derived endpoint of `files_search`.
- **`ingester_version` / `chunker_version` / `extractor_version` from
  day 1** — versioning the pipeline, not just the data.
- **Eval harness is phase-1, not deferred.**
- **Manual promotion only** in phase 1–2; heuristic suggestions deferred
  to phase 3; ingest-tagged promotion is an opt-in CLI flag, never
  default.
- **Per-filetype chunker dispatcher** — three chunkers in phase 1 (md /
  pdf / fallback semantic); more in phase 2.
- **Semantic chunking for fallback**, not fixed-size sliding window.

---

## Appendix C — Reconciliation with core m3 refactor (Phase 7+8, 2026-05-17)

A parallel refactor (commit `bd07525`, "Phase 7 & 8 modularization") landed
~1,800 lines of extractions on `bin/memory_core.py` into the modular
`bin/memory/` package. It is **non-breaking** — 100% symbol-identity parity
is preserved via re-exports from `memory_core` — but it changes the
**canonical** import paths the `files_memory` package should use going
forward.

### Module map (post-refactor)

| Symbol / concern             | Old (still works)            | New canonical home               |
|------------------------------|------------------------------|----------------------------------|
| `memory_write_impl`          | `memory_core`                | `memory.write`                   |
| `memory_write_bulk_impl`     | `memory_core`                | `memory.write`                   |
| `_check_contradictions`      | `memory_core`                | `memory.write`                   |
| `_auto_classify`             | `memory_core`                | `memory.enrich`                  |
| `_maybe_auto_entities`       | `memory_core`                | `memory.enrich`                  |
| Graph traversal helpers      | `memory_core`                | `memory.graph`                   |
| Emitter helpers              | `memory_core`                | `memory.emitters`                |
| `_ENTITY_MENTION_RE`         | `memory_core` / `.search`    | `memory.fts`                     |
| `_TEMPORAL_QUERY_RE`         | `memory_core`                | `memory.fts`                     |
| Content-safety guards        | `memory_core`                | `memory.util`                    |
| ~40 misc constants & gates   | scattered                    | `memory.config`                  |

### What changes in `files_memory/`

1. **`promote.py`** should import `memory_write_impl` from `memory.write`
   instead of `memory_core`. The current `memory_core` path keeps working
   but the new path is shorter and survives future deprecations of the
   shim re-exports.
2. **`entities.py`** is already correct — it goes directly to the
   `entities` table in `memory.db` via sqlite3, not through any
   `memory.*` API.
3. **`files_memory/config.py`** is already correct — it imports from
   `memory.config`, which is the canonical store for `FILES_DB_PATH`.
4. No schema-level changes required. Files-memory's own schema is
   independent.

### Operational implications

- **The single-DB-per-store invariant still holds.** Phase 7+8 didn't
  change memory.db's schema; the entities table, memory_items table, etc.
  are untouched.
- **Cross-DB writes (ascension)** continue to use `memory_write_impl`.
  The refactor reorganized where the function lives but not its
  signature or behavior.
- **`memory_core_parity` test gates the shim.** That test (added with
  the refactor) ensures every name files_memory might import from
  `memory_core` stays available. If a future refactor breaks parity, the
  test catches it before we do.

### Action items (folded into P3+)

- [ ] P3.x housekeeping: update `promote._write_to_memory_db` to import
      from `memory.write` (cosmetic; no behavior change).
- [ ] P3 eval gate: add a smoke check that `files_promote` works under
      both the old (`memory_core`) and new (`memory.write`) import paths
      so we catch any future re-export drift.
- [ ] When OXIDATION_TODO's "Entity Resolution" Rust target lands,
      revisit `entities.py` — it may need to switch from raw SQL to
      whatever the new entity-resolution API exposes.

### What didn't change

The three-store architecture, the file-node tree, supersession,
ascension semantics, the chunker dispatcher, and the staleness helper
are all unaffected. Phase 7+8 cleaned up core m3's internals; it did
NOT alter the contracts files-memory relies on.
