"""Configuration for the m3-memory core.

Two kinds of names live here:

1. **Pure constants** read once at import time from environment variables or
   literals. Read-only after import. Other modules `from .config import X`
   to get them.
2. **Mutable state** (singletons, caches) that live here to avoid circular
   imports. These are prefixed with `_` and other modules should import the
   `config` module and access them as `config._X`.

Phase 1 extracted basic paths/names.
Phase 3 moved the HTTP-client singleton and backend stats to `embed.py` but
kept the thresholds here.
Phase 5 introduced circuit-breaker thresholds.
"""
from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Any

# Oxidation / Rust availability.
# _OXIDATION_DISABLED must be bound UNCONDITIONALLY — `memory_core.py`
# re-exports it via `from memory.config import _OXIDATION_DISABLED`, and
# any deferred reference (chatlog_core, auto_route, chatlog_redaction)
# crashes at import time on a host without `m3_core_rs` installed. The
# Phase 7+8 refactor (commit bd07525) inlined this read inside the try/
# except, which made the symbol conditionally bound — fixed here.
_OXIDATION_DISABLED: bool = os.environ.get("M3_CORE_RS_DISABLE", "0").lower() in (
    "1", "true", "yes"
)
# Typed `Any` so callers can access the Rust extension's attributes without
# mypy flagging every use as "None has no attribute ..."; the None case is a
# real runtime fallback (extension not installed / disabled).
m3_core_rs: "Any" = None
if not _OXIDATION_DISABLED:
    try:
        import m3_core_rs  # type: ignore[no-redef]  # noqa: F811 — intentional rebind
    except ImportError:
        m3_core_rs = None  # extra not installed — Python path is the default


# ──────────────────────────────────────────────────────────────────────────────
# Mutable config (set at import from env, written at runtime by setters)
# ──────────────────────────────────────────────────────────────────────────────
# Embedder URL/model overrides. `set_embed_override()` (in memory_core.py for
# now; moves to embed.py in Phase 3) writes to these. Readers MUST go through
# the module attribute, not a local alias:
#
#   # Right:
#   from .. import config
#   url = config._EMBED_URL_OVERRIDE
#
#   # Wrong (binds at import time; misses later sets):
#   from ..config import _EMBED_URL_OVERRIDE
_EMBED_URL_OVERRIDE: str | None = (os.environ.get("M3_EMBED_URL") or "").strip() or None
_EMBED_MODEL_OVERRIDE: str | None = (os.environ.get("M3_EMBED_MODEL") or "").strip() or None


# ──────────────────────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────────────────────
from m3_sdk import get_m3_root

# BASE_DIR remains the m3-memory repo root for internal assets (e.g. config lists).
BASE_DIR: str = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Default state (DBs) now lives in the unified M3 root (~/.m3-memory/memory/).
# Precedence: M3_DATABASE env > get_m3_root()/memory/agent_memory.db.
_M3_ROOT = get_m3_root()
DB_PATH: str = os.environ.get("M3_DATABASE") or os.path.join(_M3_ROOT, "memory", "agent_memory.db")
ARCHIVE_DB_PATH: str = os.path.join(_M3_ROOT, "memory", "agent_memory_archive.db")

# files.db (FILE_INGESTION_PLAN.md). Separate physical store with its own
# lifecycle (high-volume, regeneratable, version-tracked, promotable).
# Resolution order: M3_FILES_DB_PATH env > get_m3_root()/memory/files_database.db.
FILES_DB_PATH: str = os.path.abspath(
    os.environ.get("M3_FILES_DB_PATH")
    or os.path.join(_M3_ROOT, "memory", "files_database.db")
)
# When true, the ingester prompts on first-ever invocation to confirm the
# files.db path (and offers to write M3_FILES_DB_PATH to a user-shell env
# file). Off in non-interactive contexts (CI, MCP server). The ingester
# auto-disables this when stdin is not a TTY.
FILES_DB_PROMPT_ON_FIRST_USE: bool = os.environ.get(
    "M3_FILES_DB_PROMPT_ON_FIRST_USE", "1"
).lower() in ("1", "true", "yes")


# ──────────────────────────────────────────────────────────────────────────────
# Embedding model + transport
# ──────────────────────────────────────────────────────────────────────────────
# BGE-M3 (1024-dim) is the canonical embedder for m3-memory. The store's
# dominant embeddings are `text-embedding-bge-m3`; Qwen embedding is retired.
# Mixing models in one store produces semantically incomparable vectors, so
# the default must name BGE-M3 — an operator can still override via EMBED_MODEL.
EMBED_MODEL: str = os.environ.get("EMBED_MODEL", "text-embedding-bge-m3")
EMBED_DIM: int = int(os.environ.get("EMBED_DIM", "1024"))
EMBED_TIMEOUT_READ: float = 30.0
ORIGIN_DEVICE: str = os.environ.get("ORIGIN_DEVICE", platform.node())

# Per-backend circuit-breaker thresholds for the embed cascade in
# `bin/memory/embed.py`. Each backend gets its own m3_core_rs.CircuitBreaker;
# after `_THRESHOLD` consecutive failures the breaker opens and skips that
# tier entirely (no FFI / HTTP attempt) for `_RESET_SECS` seconds. A single
# probe is then allowed (half-open); success closes the breaker, failure
# re-opens it. Defaults tuned for typical home-lab + production shapes:
#   - embedded: 5 failures / 30s reset — kernel hiccups recover fast,
#     and a stale CUDA context usually clears itself on the next call.
#   - cpu_fallback: 3 / 30s — when the local llama-server is down,
#     every call burns its full ~30s timeout. 3 strikes catches it quick.
#   - primary: 3 / 60s — primary outages are usually upstream
#     (LM Studio / Ollama crash), longer reset to avoid retry storms
#     during a real outage.
# Set any to 0 to disable that breaker (Python fallback retains the
# pre-breaker behavior — try every call, eat the timeout).
EMBED_BREAKER_EMBEDDED_THRESHOLD: int = int(
    os.environ.get("M3_EMBED_BREAKER_EMBEDDED_THRESHOLD", "5")
)
EMBED_BREAKER_EMBEDDED_RESET_SECS: float = float(
    os.environ.get("M3_EMBED_BREAKER_EMBEDDED_RESET_SECS", "30.0")
)
EMBED_BREAKER_CPU_FALLBACK_THRESHOLD: int = int(
    os.environ.get("M3_EMBED_BREAKER_CPU_FALLBACK_THRESHOLD", "3")
)
EMBED_BREAKER_CPU_FALLBACK_RESET_SECS: float = float(
    os.environ.get("M3_EMBED_BREAKER_CPU_FALLBACK_RESET_SECS", "30.0")
)
EMBED_BREAKER_PRIMARY_THRESHOLD: int = int(
    os.environ.get("M3_EMBED_BREAKER_PRIMARY_THRESHOLD", "3")
)
EMBED_BREAKER_PRIMARY_RESET_SECS: float = float(
    os.environ.get("M3_EMBED_BREAKER_PRIMARY_RESET_SECS", "60.0")
)


# ──────────────────────────────────────────────────────────────────────────────
# Dedup, contradiction, supersede
# ──────────────────────────────────────────────────────────────────────────────
DEDUP_LIMIT: int = int(os.environ.get("DEDUP_LIMIT", "1000"))
DEDUP_THRESHOLD: float = float(os.environ.get("DEDUP_THRESHOLD", "0.92"))
CONTRADICTION_THRESHOLD: float = float(os.environ.get("CONTRADICTION_THRESHOLD", "0.92"))
# SUPERSEDES_PENALTY: at retrieval time, hits that appear as the to_id of a
# 'supersedes' edge (i.e., their newer version exists) get score multiplied
# by this factor. 0.5 = visible but ranked below newer fact. 0.0 = hide.
# 1.0 = disable demotion (legacy pre-2026-04-27 behavior).
SUPERSEDES_PENALTY: float = float(os.environ.get("SUPERSEDES_PENALTY", "0.5"))

# CONTRADICTION_TITLE_GATE: 'strict' = require title substring match (legacy);
# 'loose' = cosine >= threshold + same type + content-differs only (default
# since 2026-04-27); 'off' = treat ALL high-cosine same-type pairs as
# supersedence regardless of title or content (research mode only).
CONTRADICTION_TITLE_GATE: str = os.environ.get("CONTRADICTION_TITLE_GATE", "loose").lower()

# CONTRADICTION_TYPE_EXCLUSIONS: comma-separated memory types skipped during
# contradiction-check. Default skips 'conversation'; set to
# 'conversation,message' to restore the legacy pre-2026-04-27 behavior. Empty
# string = check all types.
CONTRADICTION_TYPE_EXCLUSIONS: frozenset[str] = frozenset(
    (os.environ.get("CONTRADICTION_TYPE_EXCLUSIONS") or "conversation").lower().split(",")
)

# ──────────────────────────────────────────────────────────────────────────────
# Retrieval params
# ──────────────────────────────────────────────────────────────────────────────
SEARCH_ROW_CAP: int = int(os.environ.get("SEARCH_ROW_CAP", "5000"))
LLM_TIMEOUT: float = float(os.environ.get("LLM_TIMEOUT", "45.0"))
SPEAKER_IN_TITLE: bool = os.environ.get("SPEAKER_IN_TITLE", "1") == "1"
SHORT_TURN_THRESHOLD: int = int(os.environ.get("SHORT_TURN_THRESHOLD", "20"))
TITLE_MATCH_BOOST: float = float(os.environ.get("TITLE_MATCH_BOOST", "0.15"))
IMPORTANCE_WEIGHT: float = float(os.environ.get("IMPORTANCE_WEIGHT", "0.15"))

# Trim-by-elbow (MMR post-filter) params. Pre-Phase-7+8 the safety knobs
# defaulted to 20/8/0.05 (scale-aware, prevented the "1-result collapse"
# at large pools); the refactor lowered them to 5/3/0.08, which made the
# trimmer over-aggressive on small pools. Restored.
ELBOW_MIN_INPUT: int = int(os.environ.get("M3_ELBOW_MIN_INPUT", "20"))
ELBOW_MIN_RETURN: int = int(os.environ.get("M3_ELBOW_MIN_RETURN", "8"))
ELBOW_ABS_THRESHOLD: float = float(os.environ.get("M3_ELBOW_ABS_THRESHOLD", "0.05"))

# Routed-expansion params
# Expansion-displacement guard. Engine invariant, not a per-call tuning knob.
# Pre-Phase-7+8 default was 1.75x; the refactor accidentally changed this to
# 0.05 which is <= 1.0, disabling the guard entirely. Restored.
EXPANSION_DISPLACEMENT_MARGIN: float = float(
    os.environ.get("M3_EXPANSION_DISPLACEMENT_MARGIN", "2.0")
)
EXPANSION_PROTECTED_RANKS: int = int(
    os.environ.get("M3_EXPANSION_PROTECTED_RANKS", "3")
)

# Entity stoplist (case-insensitive) for BFS seeding/expansion
ENTITY_SEED_STOPLIST: frozenset[str] = frozenset(
    (os.environ.get("M3_ENTITY_SEED_STOPLIST") or "yes,no,ok,okay,thanks,thank,hi,hello,the,this,that,there,their,they,them,it,its").lower().split(",")
)

# ──────────────────────────────────────────────────────────────────────────────
# Ingestion emitters
# ──────────────────────────────────────────────────────────────────────────────
INGEST_WINDOW_CHUNKS: bool = os.environ.get("M3_INGEST_WINDOW_CHUNKS", "1") == "1"
INGEST_GIST_ROWS: bool = os.environ.get("M3_INGEST_GIST_ROWS", "1") == "1"
INGEST_EVENT_ROWS: bool = os.environ.get("M3_INGEST_EVENT_ROWS", "1") == "1"

INGEST_WINDOW_SIZE: int = int(os.environ.get("M3_INGEST_WINDOW_SIZE", "3"))
INGEST_GIST_MIN_TURNS: int = int(os.environ.get("M3_INGEST_GIST_MIN_TURNS", "10"))
INGEST_GIST_STRIDE: int = int(os.environ.get("M3_INGEST_GIST_STRIDE", "5"))

# ──────────────────────────────────────────────────────────────────────────────
# Query Routing
# ──────────────────────────────────────────────────────────────────────────────
QUERY_TYPE_ROUTING: bool = os.environ.get("M3_QUERY_TYPE_ROUTING", "1") == "1"
INTENT_ROUTING: bool = os.environ.get("M3_INTENT_ROUTING", "1") == "1"
INTENT_USER_FACT_BOOST: float = float(os.environ.get("M3_INTENT_USER_FACT_BOOST", "0.20"))

# ──────────────────────────────────────────────────────────────────────────────
# Fact Enrichment & Entities
# ──────────────────────────────────────────────────────────────────────────────
ENABLE_FACT_ENRICHED: bool = os.environ.get("M3_ENABLE_FACT_ENRICHED", "1").lower() in ("1", "true", "yes")
FACT_ENRICH_CONCURRENCY: int = int(os.environ.get("M3_FACT_ENRICH_CONCURRENCY", "2"))
FACT_ENRICH_MAX_ATTEMPTS: int = int(os.environ.get("M3_FACT_ENRICH_MAX_ATTEMPTS", "3"))

ENABLE_ENTITY_GRAPH: bool = os.environ.get("M3_ENABLE_ENTITY_GRAPH", "1").lower() in ("1", "true", "yes")
ENTITY_EXTRACT_CONCURRENCY: int = int(os.environ.get("M3_ENTITY_EXTRACT_CONCURRENCY", "2"))
ENTITY_EXTRACT_MAX_ATTEMPTS: int = int(os.environ.get("M3_ENTITY_EXTRACT_MAX_ATTEMPTS", "3"))

ENTITY_RESOLVE_FUZZY_MIN: float = float(os.environ.get("M3_ENTITY_RESOLVE_FUZZY_MIN", "0.85"))
ENTITY_RESOLVE_COSINE_MIN: float = float(os.environ.get("M3_ENTITY_RESOLVE_COSINE_MIN", "0.92"))

# ──────────────────────────────────────────────────────────────────────────────
# Misc
# ──────────────────────────────────────────────────────────────────────────────
VALID_SCOPES: set[str] = {"user", "session", "agent", "org"}

# ──────────────────────────────────────────────────────────────────────────────
# Entity vocab bootstrap defaults (mirrored in config/lists/entity_graph_default.yaml)
# ──────────────────────────────────────────────────────────────────────────────
_DEFAULT_VALID_ENTITY_TYPES: frozenset[str] = frozenset({
    # Human-life active (v2)
    "person", "place", "organization", "event", "date",
    "quantity", "preference", "product", "topic",
    # Human-life legacy (preserved-but-deprecated)
    "legacy_concept", "legacy_object",
    # Technical: homelab infrastructure (m3)
    "host", "container", "service", "device", "ip_address", "vlan",
    "port", "mac_address", "endpoint_url", "firewall_rule",
    # Technical: code + software engineering (m3)
    "file_path", "function", "class_or_table", "cli_flag", "env_var",
    "module", "commit_or_branch", "migration",
    # Technical: benchmark + ML (m3)
    "benchmark", "model", "variant", "metric", "dataset_field",
    "bench_artifact", "task_category", "bench_run_id",
    # Technical: memory-system primitives (m3)
    "memory_id", "memory_type", "task_id",
    # Technical: misc (m3)
    "protocol", "datetime",
})
_DEFAULT_VALID_ENTITY_PREDICATES: frozenset[str] = frozenset({
    # Cross-domain: provenance/aliasing
    "mentions", "same_as", "source_of",
    # Cross-domain: change
    "supersedes",
    # Human-life Layer 2: stable person attributes
    "located_in", "works_at", "family_of", "knows", "prefers", "owns",
    # Human-life Layer 3: event/object attributes
    "has_participant", "has_location", "has_time", "has_quantity",
    # Human-life legacy (preserved-but-deprecated)
    "before", "after",
    # Technical: infrastructure topology (m3)
    "runs_on", "hosts", "listens_on", "assigned_ip", "on_vlan",
    "fails_over_to",
    # Technical: code structure (m3)
    "defined_in", "imports", "calls",
    # Technical: provenance (m3)
    "references", "introduced_in", "deprecates",
    # Technical: bench/ML (m3)
    "measured_on", "uses_model", "judged_by", "produced_artifact",
    "affects_category",
    # Technical: human signals (m3)
    "authorizes_budget",
})

DEFAULT_ENTITY_VOCAB_YAML: Path = (
    Path(BASE_DIR) / "config" / "lists" / "entity_graph_default.yaml"
)
# Env override: when set, load_entity_vocab(None) reads this YAML instead of
# DEFAULT_ENTITY_VOCAB_YAML.
_ENV_ENTITY_VOCAB_YAML: str | None = os.environ.get("M3_ENTITY_VOCAB_YAML", "").strip() or None


# ──────────────────────────────────────────────────────────────────────────────
# Reranker (lazy-loaded in memory_core; this is just the default model name)
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_RERANK_MODEL: str = os.environ.get(
    "M3_RERANK_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2"
)


# ──────────────────────────────────────────────────────────────────────────────
# Change-agent + Chroma + federation
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_CHANGE_AGENT: str = "unknown"

CHROMA_BASE_URL: str | None = os.environ.get("CHROMA_BASE_URL")
CHROMA_COLLECTION: str = "agent_memory"
CHROMA_COLLECTIONS: list[str] = ["agent_memory", "home_memory", "user_facts"]
CHROMA_V2_PREFIX: str = "/api/v2/tenants/default_tenant/databases/default_database/collections"
CHROMA_CONNECT_T: float = 3.0
CHROMA_READ_T: float = 10.0
CHROMA_PULL_PAGE_SIZE: int = 100
CHROMA_CONTENT_MAX: int = 10_000

# Federation fires when the best local hit scores below this threshold.
# Override via M3_FEDERATION_LOW_SCORE_THRESHOLD env var.
FEDERATION_LOW_SCORE_THRESHOLD: float = float(
    os.environ.get("M3_FEDERATION_LOW_SCORE_THRESHOLD", "0.65")
)

AUTO_RELATED_LINK: bool = os.environ.get("M3_AUTO_RELATED_LINK", "1") == "1"
AUTO_RELATED_LINK_SCOPE_BY_VARIANT: bool = os.environ.get("M3_AUTO_RELATED_LINK_SCOPE_BY_VARIANT", "1") == "1"
