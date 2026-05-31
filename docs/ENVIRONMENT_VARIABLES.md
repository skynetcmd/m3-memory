# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/icon.svg" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> Environment Variables & Secure Credentials


This document specifies the environment variables required by M3 Memory.
 It is essential for security and portability that **no hardcoded values (IPs, API keys, etc.)** are present in any repository files.

## 🏛️ The "Zero-Leak" Architecture Principle

```mermaid
graph TD
    subgraph "1. Highest Priority"
        ENV[Environment Variables]
    end
    subgraph "2. OS Native"
        KEY[OS Keyring / Keychain]
    end
    subgraph "3. Synchronized Vault"
        VLT[Encrypted synchronized_secrets]
    end

    ENV -->|Fallback| KEY
    KEY -->|Fallback| VLT
```

All user-specific variables MUST be loaded into your shell's environment from a secure, local-only source.
 The recommended method is to use your operating system's native secret management service:

*   **macOS**: Keychain
*   **Linux**: Secret Service API (e.g., GNOME Keyring, KeePassXC)
*   **Windows**: Credential Manager

We provide example `zshenv.example` and `zshrc.example` files in the `config/` directory. These scripts automatically detect your OS and load secrets from the appropriate backend, making them available as environment variables.

---

## 🚀 Quick Setup

1.  **Copy the examples**:
    ```bash
    cp config/zshenv.example ~/.zshenv
    cp config/zshrc.example ~/.zshrc
    ```
2.  **Edit the new files (`~/.zshenv`, `~/.zshrc`)**:
    *   Set the `M3_MEMORY_ROOT` variable to the absolute path of your `m3-memory` directory.
    *   Follow the commented-out instructions to store your secrets (API keys, IPs, etc.) in your OS's keychain for the first time.
3.  **Restart your shell** (`zsh`). The scripts will now automatically and securely load your configuration on every new terminal session.

---

## 📋 Core Environment Variables

Your `.zshenv` should define and export the following variables by calling the `get_secret` function.

### Infrastructure & Connectivity

| Variable | Purpose | Example Keychain Command (macOS) |
|---|---|---|
| `M3_MEMORY_ROOT` | **Required.** Absolute path to your workspace directory. | `export M3_MEMORY_ROOT="/path/to/your/m3-memory"` (Set directly) |
| `SYNC_TARGET_IP` | IP address of the central PostgreSQL/ChromaDB server. | `_keychain_set agentos_sync_target_ip "YOUR_SERVER_IP"` |
| `CHROMA_BASE_URL`| Full URL to the ChromaDB API. | `_keychain_set agentos_chroma_url "http://YOUR_SERVER_IP:8000"` |
| `PG_URL`| **Required.** Full PostgreSQL connection string with credentials. | `_keychain_set agentos_pg_url "postgresql://USERNAME:REPLACE_WITH_YOUR_PASSWORD@host/db"` |

### API Keys & Authentication

| Variable | Purpose | Example Keychain Command (macOS) |
|---|---|---|
| `AGENT_OS_MASTER_KEY`| **Required.** Master key for the encrypted vault. | `_keychain_set AGENT_OS_MASTER_KEY "your-secure-key"` |
| `LM_API_TOKEN` | **Required.** Token for your local LLM server (e.g., LM Studio, Ollama, vLLM). | `_keychain_set LM_API_TOKEN "your-token"` |
| `PERPLEXITY_API_KEY`| API key for Perplexity AI (web search). | `_keychain_set PERPLEXITY_API_KEY "your-ppl-key"` |
| `XAI_API_KEY`| API key for xAI/Grok (web search fallback). | `_keychain_set XAI_API_KEY "your-grok-key"` |
| `ANTHROPIC_API_KEY`| API key for Anthropic/Claude models. | `_keychain_set ANTHROPIC_API_KEY "your-claude-key"` |
| `GEMINI_API_KEY`| API key for Google/Gemini models. | `_keychain_set GEMINI_API_KEY "your-gemini-key"` |

### MCP Proxy (`bin/mcp_proxy.py`)

The MCP proxy bridges OpenAI-compatible chat clients (Aider, OpenClaw) to the MCP tool catalog. It runs on `localhost:9000` by default.

| Variable | Purpose | Default |
|---|---|---|
| `LM_STUDIO_BASE` | Base URL of the local LLM endpoint that the proxy forwards completion requests to. | `http://localhost:1234/v1` |
| `LM_READ_TIMEOUT` | Read timeout (seconds) for upstream LLM calls. | `300` |
| `MCP_PROXY_ALLOW_DESTRUCTIVE` | When set to `1`, `true`, or `yes`, exposes the 9 destructive catalog tools (`memory_delete`, `chroma_sync`, `memory_maintenance`, `memory_set_retention`, `memory_export`, `memory_import`, `gdpr_export`, `gdpr_forget`, `agent_offline`). Default hides them. | unset |

**Per-request header**: clients should send `X-Agent-Id: <agent-name>` on `/v1/chat/completions`. The proxy propagates this to the catalog dispatcher and enforces `inject_agent_id` for tools that record agent identity (`memory_write`, `agent_heartbeat`, etc.) — clients cannot spoof identity in the request body.

### Retrieval & Ranking Tuning

These knobs change how results are ranked. Defaults are safe — override only if you need to. See `bin/memory_core.py` for implementation.

| Variable | Default | Purpose |
|---|---|---|
| `M3_SPEAKER_IN_TITLE` | `1` | Prepend `[Role]` to the title at write time when `metadata.role` is a proper name (not `user`/`assistant`/`system`/`tool`). Makes speaker visible to FTS5 so queries like "what did Caroline say about X" find speaker-scoped turns. Set to `0` to disable. |
| `M3_SHORT_TURN_THRESHOLD` | `20` | Character-length threshold below which the ranker applies a length penalty (floor 0.3×). Suppresses filler turns like "ok cool" from dominating rank. |
| `M3_TITLE_MATCH_BOOST` | `0.05` | Multiplier for the title-overlap boost: if a fraction `f` of query tokens appear in the title, add `M3_TITLE_MATCH_BOOST * f` to the final score. Set to `0` to disable. |
| `M3_IMPORTANCE_WEIGHT` | `0.05` | Weight of the caller-supplied `importance` field (0.0–1.0) in final ranking. Set to `0` to ignore importance entirely. |

### Ingestion Enrichment (opt-in)

All off by default. These change how much work each `memory_write` does and can multiply the row count on chatty conversations — turn them on deliberately. They only fire for `type="message"` rows written with a `conversation_id`; other writes are unaffected.

| Variable | Default | Purpose |
|---|---|---|
| `M3_INGEST_WINDOW_CHUNKS` | `0` | On writes, emit a `type="summary"` row every N turns that concatenates the previous N message bodies. Captures Q&A pairs that single-turn embeds miss. |
| `M3_INGEST_WINDOW_SIZE` | `3` | Number of consecutive turns combined into each window chunk when `M3_INGEST_WINDOW_CHUNKS=1`. |
| `M3_INGEST_GIST_ROWS` | `0` | On writes, emit a heuristic `type="summary"` gist row for the conversation once it passes the minimum-turn threshold and every stride thereafter. Deterministic; no LLM. |
| `M3_INGEST_GIST_MIN_TURNS` | `8` | Minimum turns in a conversation before the first gist row is emitted. |
| `M3_INGEST_GIST_STRIDE` | `8` | After the first gist, emit a new one every N additional turns. |
| `M3_INGEST_EVENT_ROWS` | `0` | Regex-extract event sentences (`<ProperNoun> <verb> ... <date hint>`) from each message and emit one `type="event_extraction"` row per match, linked back via `references`. Deterministic; no LLM. |
| `M3_QUERY_TYPE_ROUTING` | `0` | Retrieval-side: when a query matches "When/what date/which day" plus a proper noun, shift `vector_weight` to `0.3` (BM25-heavy) so the named-entity signal isn't diluted by embedding similarity. |

**Always-on:** resolved temporal anchors from `metadata.temporal_anchors` are now automatically prefixed to the embed text as `[YYYY-MM-DD] …` so vector and FTS searches can hit absolute dates even when the source text says "yesterday". No flag; free when anchors are absent.

### Files Memory — Fact Extraction (opt-in)

Controls the optional LLM fact-extraction / summarization layer of the
file-ingestion subsystem (`files.db`). Off until `M3_FILES_EXTRACT_URL` is set;
without it, ingest produces text + extractive summaries only. Full guide:
[FILES_MEMORY.md → Enabling fact extraction](FILES_MEMORY.md#enabling-fact-extraction).

| Variable | Default | Purpose |
|---|---|---|
| `M3_FILES_EXTRACT_URL` | _(unset)_ | OpenAI-compatible chat endpoint base URL, **without** `/v1` (e.g. `http://127.0.0.1:1234`). Resolution order for extraction: this → `M3_FILES_SUMMARY_URL` → `M3_LMSTUDIO_URL`. All unset = extraction unavailable. |
| `M3_FILES_EXTRACT_MODEL` | `qwen3-4b-instruct` | Model id requested for fact extraction (falls back to `M3_FILES_SUMMARY_MODEL`). |
| `M3_FILES_SUMMARY_URL` | _(unset)_ | Endpoint for the abstractive summarizer; falls back to `M3_LMSTUDIO_URL`. Also serves as a fallback for extraction (see above). |
| `M3_FILES_SUMMARY_MODEL` | `qwen3-4b-instruct` | Model id for the summarizer. |
| `M3_LMSTUDIO_URL` | _(unset)_ | Shared last-resort fallback endpoint for both extract and summary when their specific URLs are unset. |

Auth: if the endpoint enforces a key (LM Studio default), set
[`LM_API_TOKEN`](#api-keys--authentication) — it is sent as
`Authorization: Bearer <token>`. Omit for tokenless endpoints (Ollama).

### Local LLM selection

M3 does not pin a specific chat model. `bin/llm_failover.py` discovers whatever is loaded on your OpenAI-compatible endpoint(s) and picks the largest model by parameter count, filtering out embedding-only models. To minimize latency for enrichment features (auto-classify, summarization), keep a **small** instruct model (0.5B–1B) loaded alongside your embedder:

- **Ollama**: `ollama pull qwen2.5:0.5b` or `ollama pull llama3.2:1b`
- **LM Studio**: load any 0.5B–1B instruct GGUF (Q6/Q8)
- **llama.cpp**: `llama-server -m qwen2.5-0.5b-instruct-q8_0.gguf`
- **vLLM / LocalAI**: any HF-compatible small instruct model

If only the small model is loaded, `get_best_llm` picks it automatically — no env var needed. If you also load a larger generation model on the same endpoint, it will currently be preferred for every feature (per-feature routing to prefer small-for-enrichment is on the roadmap). See [QUICKSTART → Optional: load a small chat model](QUICKSTART.md#optional-load-a-small-chat-model-for-enrichment).

---

## Fact Enrichment

Optional SLM-distillation pipeline to extract atomic facts from stored memories. **Off by default.** See ARCHITECTURE.md for design overview.

| Variable | Default | Purpose |
|---|---|---|
| `M3_ENABLE_FACT_ENRICHED` | `false` | Master gate. Set to `true`/`1`/`yes` to enable fact extraction on writes. |
| `M3_FACT_ENRICH_CONCURRENCY` | `2` | Maximum concurrent SLM enrichment tasks. Higher values parallelize fact extraction; lower values reduce latency jitter on write paths. |
| `M3_FACT_ENRICH_MAX_ATTEMPTS` | `5` | Maximum retries for failed enrichment queue items before they are marked as poison (poisoned items remain visible in queue with `last_error` for manual inspection). |
| `M3_FACT_ENRICHED_URL` | (empty) | Override SLM endpoint URL. If unset, reads from the `fact_enriched.yaml` profile `url` field. |
| `M3_FACT_ENRICHED_MODEL` | (empty) | Override SLM model name. If unset, reads from the `fact_enriched.yaml` profile `model` field. Both URL and model must be non-empty when enrichment runs, or the extraction fails with a clear error. |

---

## Entity-Relation Graph

Optional SLM-extraction pipeline to build a typed knowledge graph of entities and relationships from stored memories. **Off by default.** See ARCHITECTURE.md for design overview.

| Variable | Default | Purpose |
|---|---|---|
| `M3_ENABLE_ENTITY_GRAPH` | `false` | Master gate. Off by default; nothing extracted unless set to `true`/`1`/`yes`. |
| `M3_ENTITY_EXTRACT_CONCURRENCY` | `2` | Maximum concurrent SLM extraction tasks. Mirrors fact_enriched concurrency tuning. |
| `M3_ENTITY_EXTRACT_MAX_ATTEMPTS` | `5` | Queue retry cap before poisoned-item exclusion. Failed items remain in extraction queue with `last_error` for manual inspection. |
| `M3_ENTITY_RESOLVE_FUZZY_MIN` | `0.8` | Minimum token-Jaccard similarity score for fuzzy-match resolution tier. Entities with canonical names matching above this threshold within the same type are merged. |
| `M3_ENTITY_RESOLVE_COSINE_MIN` | `0.85` | Minimum embedding cosine similarity for cosine-match resolution tier (final fallback before creating a new entity). |
| `M3_ENTITY_GRAPH_URL` | (empty) | Override SLM endpoint URL. If unset, reads from the `entity_graph.yaml` profile `url` field. |
| `M3_ENTITY_GRAPH_MODEL` | (empty) | Override SLM model name. If unset, reads from the `entity_graph.yaml` profile `model` field. Both URL and model must be non-empty when extraction runs, or the process fails with a clear error. |

---

## Project Oxidation — Rust Core (`m3_core_rs`)

Optional Rust compute core ([`m3-core-rs`](https://github.com/skynetcmd/m3-core-rs)). Until wheels are published to PyPI, install it manually with `pip install "m3-core-rs @ git+https://github.com/skynetcmd/m3-core-rs.git@v0.9.0#subdirectory=crates/m3-core-py"` (needs a Rust toolchain ≥1.94 + maturin). When the `m3_core_rs` wheel is importable, hot-path operations — SHA-256 hashing, cosine / batch-cosine, MMR reranking, the expansion-displacement guard, chat-log redaction, and pre-retrieval query routing — route through Rust. 

**By default, when `m3_core_rs` is importable, all Rust integrations are active out-of-the-box.** Every pathway falls back gracefully and silently to the pure-Python implementation when the wheel is absent. Users can explicitly opt out of any Rust-accelerated hot paths by setting the escape-hatch environment variables described below.

| Variable | Default | Purpose |
|---|---|---|
| `M3_CORE_RS_DISABLE` | `0` | Kill-switch. Set to `1`/`true`/`yes` to disable the Rust core completely and force the pure-Python path for **every** oxidation-wired operation even when the `m3_core_rs` wheel is installed. |
| `M3_ROUTE_SHADOW_MODE` | `enforce` (if wheel loaded, otherwise `off`) | Configuration gate for the accelerated Rust route decider in `bin/auto_route.py`. `enforce` (default when Rust core importable) routes queries instantly via pre-retrieval Rust classification and maps branch names via a conceptual shim translation (delivers **30-40x routing speedup**). `log` runs shadow-mode drift comparison logging. `off` disables the Rust path and runs Python post-retrieval routing. |
| `M3_EMBED_GGUF` | (empty) | Path to a bge-m3 GGUF file. When set (and `m3_core_rs` is built with the `embedded` feature), `_embed` / `_embed_many` produce embeddings **in-process via llama.cpp** instead of POSTing to a llama-server. Unset (default) keeps the HTTP embed path. Guarded: a GGUF whose embedding dimension ≠ `EMBED_DIM` is rejected and HTTP is used. |
| `M3_EMBED_GGUF_MODEL_TAG` | `bge-m3-GGUF-Q4_K_M.gguf` | The `embed_model` tag applied to vectors produced by the in-process path (above). Defaults to the llama.cpp-served bge-m3 tag the embedded backend is parity-verified against (cosine ≈ 0.996 vs stored rows with that tag). This is a distinct content-hash cache namespace from LM Studio's `text-embedding-bge-m3` rows. |
| `M3_EMBED_FALLBACK_URL` | `http://127.0.0.1:8082` | URL of the CPU HTTP fallback embed server (m3-embed-server). When `M3_EMBED_GGUF` is set but the in-process `EmbeddedEmbedder` fails to construct (GGUF missing, CUDA OOM, wheel built without `--features embedded`) or raises mid-call, `_embed` / `_embed_many` POST to `{this URL}/embedding` (singular path) before falling through to `M3_EMBED_URL`. The fallback must serve bge-m3 (or a model with matching `EMBED_DIM`) to remain vector-compatible with rows tagged `M3_EMBED_GGUF_MODEL_TAG`. Vectors produced via this fallback are tagged with `M3_EMBED_GGUF_MODEL_TAG`, sharing the in-process cache namespace. |

### Observable backend selection

`bin/memory_core.py` exposes process-global counters so callers can see which
embed path actually served each call:

- `get_embed_backend_stats() -> dict[str, int]` — snapshot of served-call
  counts keyed by label: `'cuda-inprocess'`, `'vulkan-inprocess'`,
  `'metal-inprocess'`, `'cpu-inprocess'`, `'cpu-http-fallback'`,
  `'http-primary'`. The dict is a copy; mutate freely.
- `reset_embed_backend_stats()` — clear the counters between phases (handy
  in benchmarks that want to attribute embeds to a particular query workload).

Both helpers are thread-safe. `_embed()` increments by 1; `_embed_many()`
increments by the number of inputs served along that path.
| `M3_TEST_GGUF` | (empty) | Test-only. Points the `m3-embed-llamacpp` crate's opt-in real-inference test at a GGUF model. Unset → that test is skipped. Not read by m3-memory at runtime. |

> **Note — the `M3_MMR_SHADOW` var has been retired.** An earlier build added a shadow-mode flag for the MMR reranker; the Rust MMR (`mmr_rerank_scored`) is now authoritative when `m3_core_rs` is loaded (it replicates the Python loop's selection sequence exactly, verified by `tests/test_oxidation_parity.py`). No env var gates it — `M3_CORE_RS_DISABLE` is the only override.
