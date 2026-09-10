# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> M3 Memory: Architecture

> Human-facing system design overview. For implementation specifics (schema, sync protocol, search internals), see [TECHNICAL_DETAILS.md](TECHNICAL_DETAILS.md).

---

## 👁️ System Overview

M3 Memory is a local-first persistent memory system for MCP agents. An agent calls MCP tools to write, search, link, and manage memories. The primary store is pluggable — a local SQLite file by default, or PostgreSQL as a first-class primary backend (`M3_DB_BACKEND=postgres`; see Storage Hierarchy). A separate optional PostgreSQL sync layer enables cross-device search.

```
Agent (Claude Code / Gemini CLI / Aider)
    ↕ MCP protocol (stdio)
Memory Bridge — 100+ catalog tools (bin/memory_bridge.py, sourced from bin/mcp_tool_catalog.py)
    ↕
SQLite (local, primary)
    ↕ optional
PostgreSQL (cross-device sync)
```

---

## 🧩 Module Layout

The core memory engine lives in `bin/`. As of 2026-05-17, `bin/memory_core.py` was modularized from **7,725 → 3,716 lines (-52%)** across ~14 commits spanning Phases 0–6, splitting tightly-cohesive concerns into a `bin/memory/` package while preserving the legacy module as a shim plus owner of the write/link/enrich/graph paths.

```
bin/
  memory_core.py         # 3,716-line shim + write / enrichment / graph code
  memory/
    __init__.py          # eager re-exports
    config.py            # env vars, constants, m3_core_rs ref, _EMBED_*_OVERRIDE mutables
    util.py              # sha256_hex, _batch_cosine (write + search shared)
    fts.py               # FTS5 helpers, title overlap
    db.py                # _db, _conn, _lazy_init, schema, history, gates, access-stamp batcher
    embed.py             # cascade, in-process Rust embedder, HTTP client, sliding window + dense recovery
    search.py            # scoring + ranker + reranker + query routing + the four retrieval impls
    entity.py            # vocab loading, 3-tier canonical-name resolution, entity CRUD, extraction queue + runner, entity_search / entity_get / extract_pending impls
```

`entity.py` (Phase 6, commit `6cdd8a3`) followed the same shim/identity pattern as the earlier extractions: `VALID_ENTITY_TYPES`, `VALID_ENTITY_PREDICATES`, `_ENTITY_EXTRACT_SEM`, and `_PENDING_ENTITY_TASKS` are re-exported with object identity preserved through `memory_core.py`.

**Cycle-breaking pattern.** `memory_core` imports its submodules near the top, so submodules cannot top-level-import `memory_core`. Two patterns resolve back-references:

1. **Lazy import inside function bodies** (used in `db.py`, `embed.py`, and `entity.py` for `_track_cost`) — the import happens on first call, after both modules have finished loading.
2. **`_resolve_mc_callbacks()` globals-binding shim** (used in `search.py`) — the 9 callback symbols `memory_core` owns (graph neighbors, session expansion, entity-graph walks, score-extra-rows, etc.) are bound into `search`'s globals at first use, then reused.

**What `memory_core` still owns.** The write path (`memory_write_bulk_impl`, `_check_contradictions`, `_try_enrich_or_enqueue`, `_run_fact_enricher`, `_write_fact_rows`), graph helpers (`_graph_neighbor_ids`, `_session_neighbor_ids`, `_entity_graph_neighbor_ids`, `_score_extra_rows`), conversation impls, agent/task/notification CRUD, and the enrichment queue runners.

See [MEMORY_CORE_MODULARIZATION.md](MEMORY_CORE_MODULARIZATION.md) for the per-commit log and [MEMORY_CORE_MODULARIZATION_LESSONS.md](MEMORY_CORE_MODULARIZATION_LESSONS.md) for the design notes.

---

## 💾 Storage Hierarchy

Two layers, only the first is required.

```mermaid
%%{init: {'theme':'base','themeVariables':{
  'primaryColor':'#eef2ff','primaryTextColor':'#1e1b4b','primaryBorderColor':'#6366f1',
  'lineColor':'#64748b','fontSize':'14px','fontFamily':'ui-sans-serif,system-ui,sans-serif'}}}%%
flowchart LR
    subgraph L1 ["L1 · Local primary — required"]
        direction TB
        SQ[("<b>SQLite</b><br/><i>default</i>")]
        PGL[("<b>PostgreSQL</b><br/><i>M3_DB_BACKEND=postgres</i>")]
    end

    subgraph L2 ["L2 · Warehouse — optional"]
        PG[("<b>PostgreSQL</b><br/><i>shared across machines</i>")]
    end

    SQ <-->|"row-level delta<br/>(generic bridge)"| PG
    PGL <-->|"set-based upserts<br/>(postgres_fdw fast path)"| PG

    classDef local fill:#eef2ff,stroke:#6366f1,stroke-width:1.5px,color:#1e1b4b
    classDef wh fill:#ecfdf5,stroke:#10b981,stroke-width:1.5px,color:#064e3b
    class SQ,PGL local
    class PG wh
    style L1 fill:#f8fafc,stroke:#cbd5e1,stroke-dasharray:4 3,color:#475569
    style L2 fill:#f8fafc,stroke:#cbd5e1,stroke-dasharray:4 3,color:#475569
```

Either backend can be the local primary, and both sync to a PostgreSQL
warehouse — but by different routes. A **SQLite** primary uses the generic
row-level bridge; a **PostgreSQL** primary uses the `postgres_fdw` fast path,
which is not optional there but the *only* supported path (see
[SYNC_PG_TO_PG.md](SYNC_PG_TO_PG.md)).

**L1 — Primary store** *(pluggable)*. SQLite by default: all reads and writes hit local SQLite first, WAL mode enables concurrent access, no external dependencies — the recommended default for single-user/local. The primary backend can instead be **PostgreSQL** via `M3_DB_BACKEND=postgres` + `M3_PRIMARY_PG_URL` (opt-in, chosen at install or with `mcp-memory install-m3 --db-backend postgres`), for a shared/server-hosted live store. Note: on the PostgreSQL primary, vector search is currently brute-force Rust cosine; pgvector/HNSW ANN is a future accelerator, not yet implemented.

**L2 — PostgreSQL warehouse** *(optional, separate role)* provides cross-device sync. Bi-directional delta sync uses UUID-based UPSERT with watermark tracking. Syncs memories, relationships, embeddings, and encrypted secrets. Configurable via `M3_CDW_PG_URL` (`PG_URL` still works but is deprecated). This is distinct from using PostgreSQL as the L1 primary store above.

---

## 🧩 Extension Seams

M3 has **two orthogonal extension seams**. The shared `*_impl` business logic
(write, search, entity resolution, GDPR, …) is single-sourced between them — you
extend at a seam, you don't fork the core. Full recipes in [EXTENDING.md](EXTENDING.md).

**Storage seam** (`bin/memory/backends/`) — a narrow SQL/DB-API capability
interface, *not* an ORM. Adding a DB backend is one self-contained
`<name>_backend.py`: a co-located `Dialect` subclass (the divergent SQL
fragments), a backend class (`connection`/`ensure_schema`/`keyword_search`/
`vector_search`), and one `@register_backend` line plus an allow-list entry.
`active_backend()`/`dialect()` read the registry — no `if name ==` ladder. SQLite
and PostgreSQL ship.

**Framework seam** (`bin/catalog/dispatch.py` `_dispatch_one` + a `mapping.py`) —
a thin adapter that translates a framework's calls into m3 tool dispatch and maps
structured rows back into the framework's shapes, reusing the framework-agnostic
`M3Client` dispatch core. Because it only speaks tool-dispatch, an adapter works
over *every* storage backend with no per-backend code. Shipped adapters:
LangChain/LangGraph, CrewAI (implements CrewAI's `StorageBackend` protocol), and
PydanticAI (deps-injected tools + a recall history-processor, plus a formal
`M3MemoryToolset`).

---

## 🔍 Search Pipeline

Hybrid retrieval: lexical and semantic scored in parallel, fused, then re-ranked.
Every stage is scored and explainable (`explain=True` returns the per-stage
contribution).

### The common path

```mermaid
%%{init: {'theme':'base','themeVariables':{
  'primaryColor':'#eef2ff','primaryTextColor':'#1e1b4b','primaryBorderColor':'#6366f1',
  'lineColor':'#64748b','fontSize':'14px','fontFamily':'ui-sans-serif,system-ui,sans-serif'}}}%%
flowchart LR
    Q(["🔎 Query"])

    subgraph RETRIEVE ["① Retrieve — both run, always"]
        direction TB
        FTS["<b>FTS5 + BM25</b><br/><i>lexical</i>"]
        VEC["<b>Cosine similarity</b><br/><i>semantic</i>"]
    end

    FUSE["<b>② Fuse</b><br/>w·vector + (1−w)·BM25"]
    RANK["<b>③ Re-rank</b><br/>MMR diversity"]
    R(["📄 Results"])

    Q --> FTS & VEC
    FTS & VEC --> FUSE --> RANK --> R

    classDef q fill:#1e1b4b,stroke:#1e1b4b,color:#fff,rx:14,ry:14
    classDef stage fill:#eef2ff,stroke:#6366f1,stroke-width:1.5px,color:#1e1b4b,rx:6,ry:6
    classDef fuse fill:#fef3c7,stroke:#d97706,stroke-width:1.5px,color:#78350f,rx:6,ry:6
    class Q,R q
    class FTS,VEC,RANK stage
    class FUSE fuse
    style RETRIEVE fill:#f8fafc,stroke:#cbd5e1,stroke-dasharray:4 3,color:#475569
```

1. **FTS5 keyword matching** — BM25-ranked full-text search with query
   sanitization. Falls back to pure semantic search when no keyword matches.
2. **Vector similarity** — cosine against locally-generated embeddings.
3. **MMR diversity re-ranking** — suppresses near-duplicates, balancing
   relevance against diversity.

### Every stage, including the conditional ones

The diagram above is what usually happens. Two stages it leaves out are easy to
miss and change results materially — **`w` is not a constant**, and there is an
optional cross-encoder after MMR.

```mermaid
%%{init: {'theme':'base','themeVariables':{
  'primaryColor':'#eef2ff','primaryTextColor':'#1e1b4b','primaryBorderColor':'#6366f1',
  'lineColor':'#64748b','fontSize':'13px','fontFamily':'ui-sans-serif,system-ui,sans-serif'}}}%%
flowchart TB
    Q(["🔎 Query"])
    ROUTE{{"<b>Query router</b><br/>temporal / proper-noun shape?"}}
    W07["w = 0.7<br/><i>semantic-leaning</i>"]
    W03["w = 0.3<br/><i>lexical-leaning</i>"]

    FTS["<b>FTS5</b> · BM25 score"]
    VEC["<b>Vector</b> · cosine"]

    FUSE["<b>Fuse</b><br/>w·vector + (1−w)·BM25"]

    subgraph POST ["Rank &amp; trim"]
        direction LR
        REC["Recency<br/>bonus"] --> TMP["Temporal boost<br/><i>dates in query</i>"] --> MMR["MMR<br/>diversity"] --> ELB["Elbow trim<br/><i>drop-off cut</i>"]
    end

    RERANK["<b>Cross-encoder rerank</b><br/><i>opt-in: rerank=True</i>"]
    R(["📄 Results"])

    Q --> ROUTE
    Q --> FTS & VEC
    ROUTE -->|default| W07
    ROUTE -->|"when routing fires"| W03
    W07 & W03 -.->|"sets w"| FUSE
    FTS & VEC --> FUSE --> POST
    POST --> RERANK -.->|"lazy-loads the model"| R
    POST --> R

    classDef q fill:#1e1b4b,stroke:#1e1b4b,color:#fff,rx:14,ry:14
    classDef stage fill:#eef2ff,stroke:#6366f1,stroke-width:1.5px,color:#1e1b4b,rx:6,ry:6
    classDef fuse fill:#fef3c7,stroke:#d97706,stroke-width:1.5px,color:#78350f,rx:6,ry:6
    classDef opt fill:#f5f3ff,stroke:#8b5cf6,stroke-width:1.5px,stroke-dasharray:5 3,color:#4c1d95,rx:6,ry:6
    classDef decide fill:#ecfdf5,stroke:#10b981,stroke-width:1.5px,color:#064e3b
    class Q,R q
    class FTS,VEC,REC,TMP,MMR,ELB,W07,W03 stage
    class FUSE fuse
    class RERANK opt
    class ROUTE decide
    style POST fill:#f8fafc,stroke:#cbd5e1,stroke-dasharray:4 3,color:#475569
```

**The query router** (`memory/search_routing.py:_maybe_route_query`) inspects the
query's *shape* before scoring. A "when did X happen"-style query, or one whose
intent is `temporal-reasoning` / `multi-session`, flips `w` from **0.7 to 0.3** —
weighting BM25 over embeddings, so proper-noun signal is not diluted by semantic
similarity. Gated by `M3_QUERY_TYPE_ROUTING`, **on by default**.

**The cross-encoder reranker** is opt-in (`rerank=True`). It scores each
query/result *pair* with a distilled ms-marco model rather than comparing
pre-computed vectors, which is more accurate and far more expensive
(~50 ms/pair on GPU, ~200 ms on CPU). The model is lazy-loaded — importing the
search module does **not** import `sentence_transformers`, so callers that never
rerank pay nothing at cold start.

> Dashed borders mark stages that do not always run. Everything else is on
> every query.

### Routed Retrieval (optional)

The `memory_search_routed` tool (default-disallowed, for benchmarks and research) provides temporal-aware routing: queries containing temporal keywords (when, before, after, days ago, etc.) are routed to a wider verbatim-only retrieval (k + temporal_k_bump, vector_kind_strategy='default'), while non-temporal queries retrieve at k with optional two-tier fact-variant fusion (max-kind deduplication). The temporal pattern matching is regex-based with no LLM overhead, achieving 100% recall on temporal-reasoning tasks with low false-positive rate. Environment variable `M3_ROUTER_TEMPORAL_K_BUMP` overrides the default bump (5).

Two optional post-retrieval expansions can be layered on top of the routed result, both default-off:

- **`graph_depth: int = 0`** — when > 0, take each top-K hit's id and traverse `memory_relationships` up to N hops (clamped to 3). New rows are scored against the query embedding and max-fused with the primary result before re-trimming to k. Useful when ingest populated typed edges (`references`, `supersedes`, `precedes`, `follows`, etc.); a no-op on corpora that skipped relationship writes during bulk ingest.
- **`expand_sessions: bool = False, session_cap: int = 12`** — when true, pull all turns sharing each top-K hit's `conversation_id` (capped at `session_cap` per session), score them against the query, and max-fuse. Mirrors the bench-time "reflection-style retrieval" pattern that helps supersession (knowledge-update) and side-clause recall (single-session-preference) questions.

Both expansions reuse the standard embedding path for scoring, so they integrate cleanly with the existing retrieval stack — no new schema, no new infrastructure.

### Entity-Relation Graph (on by default, gate `M3_ENABLE_ENTITY_GRAPH`)

A post-write stage extracts typed entities and relationships from stored memory items using a configured small language model (SLM). It is **on by default** (`M3_ENABLE_ENTITY_GRAPH=1`) but only does work when an extraction SLM endpoint is reachable; with none configured the queue no-ops. Set `M3_ENABLE_ENTITY_GRAPH=0` to disable. Each entity becomes a row in a separate `entities` table, with mention links in `memory_item_entities` and typed relationships in `entity_relationships`. The extraction is **semaphore-gated** (default concurrency: 2), **non-blocking** (queue-on-miss), and **resolution-on-write** (3-tier cascade: exact → token-Jaccard fuzzy → embedding cosine; no LLM tiebreaker).

Entity types and predicates are defined by a **swappable vocabulary profile**, so the graph schema is user-configurable without code changes: set `M3_ENTITY_VOCAB_YAML` (or `--entity-vocab-yaml`) to point at your own YAML profile — the stock default lives at `config/lists/entity_graph_default.yaml`. The default vocabulary constrains entity types to `{person, place, organization, event, concept, object, date}` and predicates to a 34-predicate set spanning general (`mentions`, `same_as`, `supersedes`, …), human-life (`works_at`, `located_in`, `family_of`, `owns`, …), and technical (`runs_on`, `defined_in`, `measured_on`, …) domains. A custom profile can define a domain-specific type/predicate set by editing the YAML; the chosen vocabulary is validated at extraction time.

A new `entity_graph: bool` kwarg on `memory_search_routed` walks `entity_relationships` from query-mentioned entities, fuses linked memory items into the result. Variant rows are skipped by default; bench paths opt in via `entity_extractor_variant_allowlist`.

See [ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md#entity-relation-graph) for all gates.

---

## ✏️ Write Pipeline

Every `memory_write` call runs through this sequence:

```mermaid
%%{init: {'theme':'base','themeVariables':{
  'primaryColor':'#eef2ff','primaryTextColor':'#1e1b4b','primaryBorderColor':'#6366f1',
  'lineColor':'#64748b','fontSize':'13px','fontFamily':'ui-sans-serif,system-ui,sans-serif',
  'actorBkg':'#1e1b4b','actorTextColor':'#ffffff','actorBorder':'#1e1b4b',
  'noteBkgColor':'#fef3c7','noteBorderColor':'#d97706','noteTextColor':'#78350f',
  'labelBoxBkgColor':'#6366f1','labelBoxBorderColor':'#6366f1','labelTextColor':'#ffffff',
  'altBackground':'#f8fafc'}}}%%
sequenceDiagram
    autonumber
    participant A as 🤖 Agent
    participant M as Memory Bridge
    participant E as Embedder
    participant S as Store

    A->>M: memory_write(content)
    M->>M: Input safety check

    alt fast embedder available - the normal case
        Note over M,E: NORMAL PATH - a fast embedder is reachable
        M->>E: Embed
        E-->>M: Vector
        M->>M: Contradiction detection
        M->>M: Auto-link related memories
    else no fast embedder reachable
        Note over M,E: ZERO-LAG PATH - no fast embedder<br/>Persist verbatim now and defer the vector to the<br/>cognitive loop. The row is FTS-searchable immediately,<br/>and vector search picks it up once the embed pass has run.
    end

    M->>M: SHA-256 content hash
    M->>S: Store
    S-->>M: OK
    M-->>A: Created: uuid
    Note over M,S: Enrichment, entity extraction and any<br/>deferred embedding happen AFTER this return.
```

- **Safety check** — rejects XSS, SQL injection, code injection, prompt injection
- **Contradiction detection** — if a same-type memory exists with conflicting content (cosine ≥ `M3_CONTRADICTION_THRESHOLD`, default **0.92**), the old memory is superseded and the full history preserved. The title gate defaults to `loose` (`M3_CONTRADICTION_TITLE_GATE`), so an identical title is *not* required; set it to `strict` for the legacy title-substring behaviour
- **Auto-linking** — connects the new memory to the most related existing memory (cosine > 0.7) via a `related` relationship
- **Content hash** — SHA-256 for tamper detection via `memory_verify`

### Fact Enrichment (on by default, gate `M3_ENABLE_FACT_ENRICHED`)

A post-write stage extracts atomic facts from stored memories using a configured small language model (SLM). It is **on by default** (`M3_ENABLE_FACT_ENRICHED=1`) but only does work when an enrichment SLM endpoint is reachable; with none configured the queue no-ops. Set `M3_ENABLE_FACT_ENRICHED=0` to disable. Each fact becomes a separate `fact_enriched` row linked back to the source via a `references` edge. The enrichment is **semaphore-gated** (default concurrency: 2) and **non-blocking**: if the enricher semaphore is full, the source write returns immediately and the enrichment is enqueued in `fact_enrichment_queue` for later processing. The `enrich-pending` CLI command (or `enrich_pending` MCP tool) drains the queue with a dry-run / confirm flow and retries up to `M3_FACT_ENRICH_MAX_ATTEMPTS` times on failure (default 3). Verbatim rows are always persisted before enrichment is attempted, so enricher failures never corrupt the primary write.

Items with a non-NULL `variant` (typically benchmark rows) are **skipped by default**; pass `fact_enricher_variant_allowlist={"variant-name", ...}` to opt specific variants in. See [ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md#fact-enrichment) for all gates and the [fact_enriched profile](../config/slm/fact_enriched.yaml) for SLM configuration.

---

## 🔄 The Cognitive Loop

Everything above describes the **write path** — what happens before `memory_write`
returns. The write path is deliberately thin: it validates, stores, hashes, and
returns. The expensive work happens **afterwards**, in a separate long-running
process (`bin/m3_cognitive_loop.py`) that wakes on an interval and drains queues.

That split is the reason a write is fast. Nothing in the loop is on the caller's
latency path, so embedding, entity extraction and enrichment can be as expensive
as they need to be without a tool call ever waiting on them.

```mermaid
%%{init: {'theme':'base','themeVariables':{
  'primaryColor':'#eef2ff','primaryTextColor':'#1e1b4b','primaryBorderColor':'#6366f1',
  'lineColor':'#64748b','fontSize':'13px','fontFamily':'ui-sans-serif,system-ui,sans-serif'}}}%%
flowchart TB
    W(["memory_write"]) --> V["validate"] --> ST["store"] --> RET(["return ✓"])
    Q[("queues<br/><i>rows awaiting work</i>")]
    ST -.->|enqueue| Q

    subgraph LOOP ["Cognitive loop — separate process, --interval 300s"]
        direction TB
        subgraph QD ["queue-driven — run when their queue is non-empty"]
            direction TB
            P1["entities"] ~~~ P2["enrich<br/><i>+ Reflector</i>"] ~~~ P3["classify"] ~~~ P4["embed"]
            P5["files_extract"] ~~~ P6["consolidate"] ~~~ P7["distill"] ~~~ P8["prune"]
        end
        subgraph TD ["time-driven — run when due"]
            direction LR
            T1["sync · ≥1h"] ~~~ T2["maintenance · ≥1h"] ~~~ T3["audit · ≥7d"]
        end
    end

    Q --> QD

    linkStyle default stroke:#64748b

    classDef fast fill:#ecfdf5,stroke:#10b981,stroke-width:1.5px,color:#064e3b,rx:6,ry:6
    classDef pass fill:#eef2ff,stroke:#6366f1,stroke-width:1.5px,color:#1e1b4b,rx:6,ry:6
    classDef time fill:#f5f3ff,stroke:#8b5cf6,stroke-width:1.5px,color:#4c1d95,rx:6,ry:6
    classDef queue fill:#fef3c7,stroke:#d97706,stroke-width:1.5px,color:#78350f
    class W,V,ST,RET fast
    class P1,P2,P3,P4,P5,P6,P7,P8 pass
    class T1,T2,T3 time
    class Q queue
    style LOOP fill:#f8fafc,stroke:#cbd5e1,stroke-dasharray:4 3,color:#475569
    style QD fill:#ffffff,stroke:#e2e8f0,color:#475569
    style TD fill:#ffffff,stroke:#e2e8f0,color:#475569
```

### The passes

Eleven passes, each independently skippable (`--skip-<name>`):

| Pass | What it does |
|---|---|
| `entities` | Extract entities and relationships; build the entity graph |
| `enrich` | Distill atomic facts from stored memories via a local SLM — **and run the Reflector** (below) |
| `classify` | Resolve `type="auto"` writes into a concrete memory type |
| `embed` | Generate embeddings for rows written without one |
| `files_extract` | Drain queued fact-extraction for ingested files |
| `consolidate` | Merge groups of old same-type memories into summaries |
| `distill` | Compress long threads into key points |
| `prune` | Decay and prune abandoned chat-log conversations |
| `sync` | Push/pull against the PostgreSQL warehouse (≥ 1h apart) |
| `maintenance` | Housekeeping — indexes, integrity, retention (≥ 1h apart) |
| `audit` | Integrity audit over the hash chain (≥ 7d apart) |

### The Reflector — contradiction detection's second path

The write path catches contradictions **deterministically**: cosine similarity
against a threshold, no model involved (see [Write Pipeline](#-write-pipeline)).
That only catches near-restatements of a single memory.

The loop's **Reflector pass** (inside `enrich`; `bin/m3_enrich.py:_run_reflector_pass`)
is the second path. It reviews enriched facts for conflicts the write-time check
could not see — because the contradicting memories were written far apart, or
because the conflict is semantic rather than lexical — and writes `supersedes`
edges when it finds one. It runs **by default** (`--no-reflect` opts out).

So contradiction handling is not one mechanism but three, at different points and
costs: deterministic at write time, model-assisted in the loop, and explicit via a
[curation plan](#-deterministic-curation).

### Fairness and back-pressure

The loop shares a machine with the user's editor and the local model, so pass
selection is not a simple round-robin (`_select_pass_order`, a pure function so
the policy is testable on its own):

- **Queue-aware** — a pass whose queue is empty is dropped from the cycle. Under
  throttle only *one* pass runs, and without this filter that single slot could
  go to an empty queue while a backlog of thousands waits another full cycle.
- **Round-robin** — the eligible list rotates by cycle number, so an
  always-backlogged upstream pass can't permanently hold the GPU ahead of
  downstream ones.
- **Idle-aware intensity** — when the governor reports load (a proxy for "the
  user is busy"), only the rotated leader runs. When idle, every eligible pass
  runs to drain the backlog.

Time-driven passes have no queryable queue, so they are never filtered out — their
own `--*-min-interval-s` gate decides. Absence from the work map means
"eligibility unknown, let the pass decide", never "no work".

### What this means operationally

- **A fresh write is searchable immediately by FTS, and by vector search once
  `embed` has run.** If the loop is not running, writes still succeed and stay
  FTS-searchable — they are simply not enriched or embedded. `m3 doctor` reports
  a stalled loop.
- **The loop is a separate process, not a thread.** It has its own lifecycle
  (`AgentOS_LoopWatchdog` restarts it on a schedule), so a watchdog-terminated
  loop never runs `atexit` — stale PID entries under the engine root are routine,
  not evidence of a crash.
- **Draining a backlog** is a matter of raising `--limit-per-pass` (default 4) or
  running the pass directly; see [EMBED_DEPLOYMENT.md](EMBED_DEPLOYMENT.md#draining-an-embed-backlog).

---

## 🧹 Deterministic Curation

Curation — dedupe, supersede, prune, promote — is the third contradiction path,
and the one under an operator's direct control. The important architectural point
is **where the LLM sits**: it plans, it does not act.

```mermaid
%%{init: {'theme':'base','themeVariables':{
  'primaryColor':'#eef2ff','primaryTextColor':'#1e1b4b','primaryBorderColor':'#6366f1',
  'lineColor':'#64748b','fontSize':'13px','fontFamily':'ui-sans-serif,system-ui,sans-serif'}}}%%
flowchart LR
    AG(["🤖 curate-memory /<br/>curate-chatlog subagent"])
    PLAN["<b>PLAN</b><br/><i>structured JSON</i>"]
    APPLY["<b>curator_apply.py</b><br/><i>deterministic — no LLM</i>"]
    REP["<b>report</b><br/><i>per-section outcome</i>"]

    AG -->|"decides WHAT"| PLAN
    PLAN --> APPLY
    APPLY -->|"does it, once"| REP
    REP -.->|"agent reads"| AG

    classDef llm fill:#f5f3ff,stroke:#8b5cf6,stroke-width:1.5px,color:#4c1d95,rx:14,ry:14
    classDef det fill:#ecfdf5,stroke:#10b981,stroke-width:2px,color:#064e3b,rx:6,ry:6
    classDef data fill:#fef3c7,stroke:#d97706,stroke-width:1.5px,color:#78350f,rx:6,ry:6
    class AG llm
    class APPLY,REP det
    class PLAN data
```

> The boundary is the point: **purple is the only place a model runs.** Green is
> a plain function, so there is no procedure left for an agent to improvise on.

`curator_apply.py` is **one entry point with no LLM in the loop**. The agent's job
is "emit a plan, call apply, read the report" — a single MCP round-trip instead of
N tool calls.

That shape is a **structural fix for two real failures** (2026-05-17), both from an
era when a second LLM agent interpreted the plan and called tools one at a time:

1. It looped single-id `memory_delete` across ~486 IDs and burned a ~16-minute budget.
2. After the prompt was rewritten to mandate `memory_delete_bulk`, the replacement
   agent invented a "write the IDs to a file with Bash" strategy and ran past its
   budget reasoning about Windows path mapping.

Neither is a prompt bug — both are the predictable result of leaving a procedure
to an agent's discretion. Rather than write a stricter prompt, m3 made the wrong
path *impossible*: the apply step is a deterministic function, so there is no
procedure left to get creative about.

Both stores share one plan schema (an absent key is a no-op):

| Store | Sections |
|---|---|
| memory | `delete` (soft), `delete_hard` (cascade), `link`, `update` |
| chatlog | `decay`, `dedup` (keep/drop), `promote`, `prune` |

Errors never cross the boundary as exceptions — each section reports its own
outcome, so a partial failure is visible rather than aborting the batch. Reached
via the `curate_memory_apply` / `curate_chatlog_apply` MCP tools.

---

## 🧠 Intelligence Features

M3 uses a local LLM for features that benefit from language understanding. Any server that exposes OpenAI-compatible `/v1/chat/completions` and `/v1/embeddings` endpoints works.

- **Auto-classification** — pass `type="auto"` and the LLM categorizes the memory into one of 30+ types
- **Conversation summarization** — compress long threads into key points
- **Memory consolidation** — merge groups of old memories into summaries, reducing noise while preserving knowledge

All LLM features run locally. No external API calls.

---

## 🔒 Security Model

### Credential Resolution

Three-tier priority: environment variables → OS keyring → encrypted vault (AES-256, PBKDF2, 600K iterations).

### Content Integrity

SHA-256 hash on every write. `memory_verify` re-computes and compares.

### Input Safety

Content safety check at the write boundary rejects XSS, SQL injection, Python code injection, and prompt injection patterns.

### Runtime Hardening

Strict HTTP timeouts, circuit breaker (3-failure threshold), token values never logged, FTS5 query sanitization, semaphore-bounded embedding concurrency.

---

## 👥 Scoping & Multi-Tenancy

| Scope | Behavior |
|-------|----------|
| `agent` (default) | Per-agent memory |
| `user` | Persists across sessions and agents |
| `session` | Auto-expires after 24 hours |
| `org` | Shared across all users and agents |

Every search accepts `user_id` and `scope` filters.

---

## 🇪🇺 GDPR Compliance

- **Article 17 (Right to Be Forgotten):** `gdpr_forget` hard-deletes all data for a user — memories, embeddings, relationships, history, sync queue
- **Article 20 (Data Portability):** `gdpr_export` returns all memories as portable JSON
- Audit trail in `gdpr_requests` table with timestamps and item counts
