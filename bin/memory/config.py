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

from m3_sdk import getenv_compat

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
from m3_sdk import get_m3_config_root, get_m3_engine_root, get_m3_root

# BASE_DIR remains the m3-memory repo root for internal assets (e.g. config lists).
BASE_DIR: str = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Default state (DBs) now lives in the unified M3 root (~/.m3-memory/memory/).
_M3_CONFIG_ROOT = get_m3_config_root()
_M3_ENGINE_ROOT = get_m3_engine_root()
_M3_ROOT = get_m3_root()

# Path resolution (new-root-with-legacy-fallback) is the single source of truth
# in m3_core.paths; this local name is a thin alias.
from m3_core.paths import resolve_engine_file as _resolve_engine_file

DB_PATH: str = os.environ.get("M3_DATABASE") or _resolve_engine_file("agent_memory.db")
ARCHIVE_DB_PATH: str = _resolve_engine_file("agent_memory_archive.db")

# files.db (FILE_INGESTION_PLAN.md). Separate physical store with its own
# lifecycle (high-volume, regeneratable, version-tracked, promotable).
# Resolution order: M3_FILES_DB_PATH env > get_m3_root()/memory/files_database.db.
FILES_DB_PATH: str = os.path.abspath(
    os.environ.get("M3_FILES_DB_PATH")
    or _resolve_engine_file("files_database.db")
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

# ── Proper-embedder identity ───────────────────────────────────────────────────
# A stored vector is only comparable to the rest of the store if it came from the
# SAME embedder: same model name, dimension, normalization scheme, and embed
# space. These knobs define that identity (model-agnostic — driven by config, not
# a hardcoded model). A tier whose output fails this identity is treated as a
# failed tier (the cascade tries the next one); if no tier validates, embedding
# is DEFERRED (the row stays keyword-searchable and is retried next sweep).
#   require_unit_norm: output vectors must be L2-unit-length (the cosine/vector
#       store assumes this). Off only for an embedder that emits raw magnitudes.
#   norm_tol: tolerance for the sampled unit-norm check.
#   space_tag: an explicit embed-space id; defaults to EMBED_MODEL. Bump it (or
#       EMBED_MODEL) when the vector space changes so old vectors are excluded.
#   compatible_models: extra embed_model strings that map to the SAME space
#       (back-compat for vectors written under a different-but-equivalent tag).
EMBED_REQUIRE_UNIT_NORM: bool = (
    os.environ.get("M3_EMBED_REQUIRE_UNIT_NORM", "1").lower() not in ("0", "false", "no"))
EMBED_NORM_TOL: float = float(os.environ.get("M3_EMBED_NORM_TOL", "0.05"))
EMBED_SPACE_TAG: str = (os.environ.get("M3_EMBED_SPACE_TAG") or "").strip() or EMBED_MODEL
# Tier-2 (the local CPU HTTP embed server) historically mis-tagged its vectors
# with the tier-1 GGUF filename; it should carry the proper identity name.
EMBED_FALLBACK_MODEL_TAG: str = (
    (os.environ.get("M3_EMBED_FALLBACK_MODEL_TAG") or "").strip() or EMBED_MODEL)
# Operator-supplied extra compatible model tags (comma-separated).
EMBED_COMPATIBLE_MODELS: tuple[str, ...] = tuple(
    m.strip() for m in (os.environ.get("M3_EMBED_COMPATIBLE_MODELS") or "").split(",")
    if m.strip())

EMBED_TIMEOUT_READ: float = 30.0
# TCP connect timeout for the embed HTTP client (LM Studio / embed server).
EMBED_TIMEOUT_CONNECT: float = 3.0
# Hard wall-clock ceiling for embedding a QUERY on the interactive search path
# (bin/memory/search.py). The full _embed cascade can stack per-tier timeouts
# (30s read × ×2 × ×4 + retries + a 30s semaphore wait) into multiple minutes
# on a degraded box — and because the stdio MCP server is a single event loop,
# that stall freezes EVERY concurrent tool call (the "MCP server locked up"
# symptom). memory_write already sidesteps this by DEFERRING its embed when no
# fast tier is available; search cannot defer (no query vector, no semantic
# search), so instead it bounds the embed with this deadline and degrades to
# FTS-only results on timeout. Interactive-only: bulk/backfill paths keep the
# full EMBED_TIMEOUT_READ budget. 0 disables the ceiling (pre-fix behavior).
EMBED_SEARCH_DEADLINE_S: float = float(
    os.environ.get("M3_EMBED_SEARCH_DEADLINE_S", "8.0")
)
ORIGIN_DEVICE: str = getenv_compat("M3_ORIGIN_DEVICE", "ORIGIN_DEVICE") or os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or platform.node()

# Storage backend selection (Phase 0 of PostgreSQL-as-primary). Default 'sqlite'
# — the only required store (DESIGN_PHILOSOPHIES §1). 'postgres' is opt-in and
# lands in a later phase; selecting it now raises a clear error via
# backends.active_backend(). Read live at resolve time (backends.selector), not
# cached here, so tests can flip it without reimporting config.
DB_BACKEND: str = getenv_compat("M3_DB_BACKEND", "DB_BACKEND", "sqlite") or "sqlite"

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
SUPERSEDES_PENALTY: float = float(getenv_compat("M3_SUPERSEDES_PENALTY", "SUPERSEDES_PENALTY", "0.5"))

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
SEARCH_ROW_CAP: int = int(getenv_compat("M3_SEARCH_ROW_CAP", "SEARCH_ROW_CAP", "5000"))
LLM_TIMEOUT: float = float(os.environ.get("LLM_TIMEOUT", "45.0"))
SPEAKER_IN_TITLE: bool = getenv_compat("M3_SPEAKER_IN_TITLE", "SPEAKER_IN_TITLE", "1") == "1"
SHORT_TURN_THRESHOLD: int = int(getenv_compat("M3_SHORT_TURN_THRESHOLD", "SHORT_TURN_THRESHOLD", "20"))
TITLE_MATCH_BOOST: float = float(getenv_compat("M3_TITLE_MATCH_BOOST", "TITLE_MATCH_BOOST", "0.15"))
IMPORTANCE_WEIGHT: float = float(getenv_compat("M3_IMPORTANCE_WEIGHT", "IMPORTANCE_WEIGHT", "0.15"))

# ── Confidence & trust (knowledge-maintenance) ───────────────────────────────
# All default OFF / neutral: nothing about retrieval changes until explicitly
# enabled. See docs/plans/KNOWLEDGE_MAINTENANCE_PLAN.md.
#
# M3_CONFIDENCE_RANKING: when '1', blend a memory's stored `confidence` into the
# retrieval score as an additive term (like IMPORTANCE_WEIGHT). Default '0' so
# flag-off ranking stays byte-identical to today.
CONFIDENCE_RANKING: bool = os.environ.get("M3_CONFIDENCE_RANKING", "0") == "1"
# Weight of the confidence term when CONFIDENCE_RANKING is on.
CONFIDENCE_WEIGHT: float = float(os.environ.get("M3_CONFIDENCE_WEIGHT", "0.10"))
# Which confidence representation drives ranking: 'transparent' (the stored,
# user-facing aggregate) or 'bayesian' (the Beta posterior mean kept alongside,
# for experiments). The displayed `confidence` is always the transparent value.
CONFIDENCE_MODEL: str = os.environ.get("M3_CONFIDENCE_MODEL", "transparent").lower()

# M3_ENFORCE_AGENT_ISOLATION: when '1', a search that supplies a requesting agent
# is restricted to that agent's OWN scope='agent' (private) memories plus all
# shared scopes (org/user/session) — it can never see ANOTHER agent's private
# notes. Default '0' so behavior stays byte-identical to today (scope filtering
# remains caller-applied/advisory unless a caller opts in per-request). This is
# the SQL-layer enforcement of the scope model documented in MULTI_AGENT.md.
# Enforcement also activates per-request whenever a `requesting_agent` is passed,
# independent of this flag (see memory_search_scored_impl).
ENFORCE_AGENT_ISOLATION: bool = os.environ.get("M3_ENFORCE_AGENT_ISOLATION", "0") == "1"
# When '1', allow the daily maintenance pass to nudge agent trust from observed
# contradiction/corroboration. Default '0' — explicit agent_set_trust only.
TRUST_AUTOTUNE: bool = os.environ.get("M3_TRUST_AUTOTUNE", "0") == "1"
# When '1', allow the background job to run autonomous episodic->semantic
# belief consolidation. Default '0' — manual/curator-triggered only.
CONSOLIDATION_AUTO: bool = os.environ.get("M3_CONSOLIDATION_AUTO", "0") == "1"
# When '1', a near-identical re-write (cosine >= CORROBORATION_THRESHOLD AND same
# content) records a `corroborates` event + bumps the existing memory's
# corroboration_count/confidence instead of creating an orphan duplicate row.
# Default '0' — write behavior is unchanged until explicitly enabled.
CORROBORATION: bool = os.environ.get("M3_CORROBORATION", "0") == "1"
# Cosine floor for treating a high-similarity, same-content write as
# corroboration (vs. CONTRADICTION_THRESHOLD for different-content). Higher than
# the contradiction threshold so only true near-duplicates corroborate.
CORROBORATION_THRESHOLD: float = float(os.environ.get("CORROBORATION_THRESHOLD", "0.95"))

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
# Change-agent
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_CHANGE_AGENT: str = "unknown"

AUTO_RELATED_LINK: bool = os.environ.get("M3_AUTO_RELATED_LINK", "1") == "1"
AUTO_RELATED_LINK_SCOPE_BY_VARIANT: bool = os.environ.get("M3_AUTO_RELATED_LINK_SCOPE_BY_VARIANT", "1") == "1"

# ──────────────────────────────────────────────────────────────────────────────
# Tier 4 Cloud Enclave & Failover Configurations (Milestone 3)
# ──────────────────────────────────────────────────────────────────────────────
M3_ALLOW_CLOUD_FALLBACK: bool = os.environ.get("M3_ALLOW_CLOUD_FALLBACK", "0").lower() in ("1", "true", "yes")
M3_CLOUD_ENCLAVE_URL: str | None = (os.environ.get("M3_CLOUD_ENCLAVE_URL") or "").strip() or None
M3_CLOUD_AUTH_TOKEN_KEYRING: str | None = (os.environ.get("M3_CLOUD_AUTH_TOKEN_KEYRING") or "").strip() or None
M3_CLOUD_MINIMIZATION_LEVEL: str = (os.environ.get("M3_CLOUD_MINIMIZATION_LEVEL") or "standard").lower()

# Circuit Breaker for Tier 4 Cloud Enclave
EMBED_BREAKER_CLOUD_THRESHOLD: int = int(os.environ.get("M3_EMBED_BREAKER_CLOUD_THRESHOLD", "3"))
EMBED_BREAKER_CLOUD_RESET_SECS: float = float(os.environ.get("M3_EMBED_BREAKER_CLOUD_RESET_SECS", "60.0"))

