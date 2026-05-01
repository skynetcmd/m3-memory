"""Core memory primitives: single + bulk write, search, enrichment, emitters.

Not a CLI — imported by MCP server, bench drivers, and import scripts.

## Public async API (relevant to ingest)

`memory_write_impl(...)` — single-item insert with full enrichment chain.
Exposed as the `memory_write` MCP tool; accepts `variant` and `embed_text`.

`memory_write_bulk_impl(items, *, enrich=None, check_contradictions=None,
emit_conversation=None, variant=None)` — batch insert for benchmarks / imports.
Routes embeddings through `_embed_many`. Per-item fields (type, content,
metadata, conversation_id, variant, embed, embed_text, auto_classify) are
honored. Kwargs:

| Kwarg | Default | Default behavior |
|---|---|---|
| `enrich` | `None` | Inherit env gates `M3_INGEST_AUTO_TITLE` and `M3_INGEST_AUTO_ENTITIES`. `True` forces both on, `False` forces both off. |
| `check_contradictions` | `None` | OFF (bulk default differs from single-insert to protect throughput on large imports). `True` enables bounded contradiction check (Semaphore(8)), `False` explicit off. |
| `emit_conversation` | `None` | ON when items carry `conversation_id` and `type=='message'`. `False` disables event/window/gist emitters. Sub-emitters are additionally gated by env vars `M3_INGEST_EVENT_ROWS`, `M3_INGEST_WINDOW_CHUNKS`, `M3_INGEST_GIST_ROWS`. |
| `variant` | `None` | No default variant tag. When set, acts as fallback when an item doesn't carry its own `variant`. Per-item `variant` always wins. |

Of these, only `variant` is exposed on the MCP `memory_write` schema and via
`--variant` on bench CLIs. `enrich` / `check_contradictions` /
`emit_conversation` are kwarg-only perf knobs for bulk ingest drivers.

## Env-var gates read

Ingest: `M3_INGEST_AUTO_TITLE`, `M3_INGEST_AUTO_ENTITIES`,
`M3_INGEST_EVENT_ROWS`, `M3_INGEST_WINDOW_CHUNKS`, `M3_INGEST_GIST_ROWS`,
`M3_INGEST_WINDOW_SIZE`, `M3_INGEST_GIST_MIN_TURNS`, `M3_INGEST_GIST_STRIDE`.

Retrieval / ranking: `M3_QUERY_TYPE_ROUTING`, `M3_TITLE_MATCH_BOOST`,
`M3_SHORT_TURN_THRESHOLD`, `M3_SPEAKER_IN_TITLE`, `M3_IMPORTANCE_WEIGHT`,
`SEARCH_ROW_CAP`.

Embeddings: `EMBED_MODEL`, `EMBED_DIM`, `EMBED_BULK_CHUNK`,
`EMBED_BULK_CONCURRENCY`, `CHROMA_BASE_URL`.

Other: `CONTRADICTION_THRESHOLD`, `DEDUP_LIMIT`, `DEDUP_THRESHOLD`,
`LLM_TIMEOUT`, `ORIGIN_DEVICE`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import re
import sqlite3
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable  # noqa: F401 (used in annotations)

import yaml

from llm_failover import get_best_embed, get_best_llm, get_smallest_llm
from m3_sdk import M3Context, resolve_db_path


async def conversation_summarize_impl(conversation_id: str, threshold: int = 20) -> str:
    """Summarizes a conversation into key points using the local LLM."""
    # 1. Fetch all messages for the conversation
    with _db() as db:
        rows = db.execute(
            """SELECT mi.title AS role, mi.content
               FROM memory_relationships mr
               JOIN memory_items mi ON mr.to_id = mi.id
               WHERE mr.from_id = ? AND mr.relationship_type = 'message' AND mi.is_deleted = 0
               ORDER BY mi.created_at ASC""",
            (conversation_id,)
        ).fetchall()

    # 2. Threshold check
    if len(rows) < threshold:
        return f"Conversation too short to summarize ({len(rows)} messages, threshold={threshold})"

    # 3. Concatenate messages
    messages_text = "\n".join(f"{row['role']}: {row['content']}" for row in rows)

    # 4. Call the local LLM via failover logic
    token = ctx.get_secret("LM_API_TOKEN") or "lm-studio"
    client = _get_embed_client()
    result = await get_best_llm(client, token)
    if not result:
        return "Error: No local LLM available for summarization."

    base_url, model = result
    prompt = f"Summarize this conversation into 3-5 key points. Preserve facts, decisions, and action items.\n\n{messages_text}"

    try:
        resp = await client.post(
            f"{base_url}/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=LLM_TIMEOUT
        )
        resp.raise_for_status()
        summary_text = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"Error during LLM summarization: {type(e).__name__}: {e}"

    # 5. Store the summary as a new memory item
    summary_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _db() as db:
        db.execute(
            "INSERT INTO memory_items (id, type, title, content, created_at, content_hash) VALUES (?, 'summary', ?, ?, ?, ?)",
            (summary_id, f"Summary of {conversation_id[:8]}", summary_text, now, _content_hash(summary_text))
        )

    # 6. Link it to the conversation
    memory_link_impl(summary_id, conversation_id, "references")

    _record_history(summary_id, "create", None, summary_text, "content", "system")
    return summary_text
from embedding_utils import (
    batch_cosine as _batch_cosine,
)
from embedding_utils import (
    infer_change_agent as _infer_change_agent_util,
)
from embedding_utils import (
    pack as _pack,
)
from embedding_utils import (
    unpack as _unpack,
)

logger = logging.getLogger("memory_core")
# Default context (memory/agent_memory.db unless M3_DATABASE overrides at
# import time). Per-call DB overrides flow through the active_database
# ContextVar and _current_ctx() below — this attribute is kept for legacy
# callers that reference ctx.get_secret(), ctx.get_async_client(), etc.
ctx = M3Context.for_db(None)


def _current_ctx() -> M3Context:
    """Return the M3Context for the currently active DB path.

    Honors (in order): active_database() ContextVar > M3_DATABASE env > default.
    Cached per path so the hot path avoids repeat pool construction.
    """
    return M3Context.for_db(resolve_db_path(None))

# ── Constants ─────────────────────────────────────────────────────────────────
BASE_DIR            = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH             = os.path.join(BASE_DIR, "memory", "agent_memory.db")
ARCHIVE_DB_PATH     = os.path.join(BASE_DIR, "memory", "agent_memory_archive.db")
EMBED_MODEL         = os.environ.get("EMBED_MODEL", "qwen3-embedding")
EMBED_DIM           = int(os.environ.get("EMBED_DIM", "1024"))
EMBED_TIMEOUT_READ  = 30.0
ORIGIN_DEVICE       = os.environ.get("ORIGIN_DEVICE", platform.node())

# Task 1: Configurable Dedup/Search Limits (#46)
DEDUP_LIMIT            = int(os.environ.get("DEDUP_LIMIT", "1000"))
DEDUP_THRESHOLD        = float(os.environ.get("DEDUP_THRESHOLD", "0.92"))
CONTRADICTION_THRESHOLD = float(os.environ.get("CONTRADICTION_THRESHOLD", "0.92"))
# SUPERSEDES_PENALTY: at retrieval time, hits that appear as the to_id of a
# 'supersedes' edge (i.e., their newer version exists) get score multiplied
# by this factor. 0.5 = visible but ranked below newer fact. 0.0 = hide.
# 1.0 = disable demotion (legacy pre-2026-04-27 behavior).
SUPERSEDES_PENALTY = float(os.environ.get("SUPERSEDES_PENALTY", "0.5"))
# CONTRADICTION_TITLE_GATE: 'strict' = require title substring match (legacy
# 2026-04 and earlier behavior); 'loose' = use cosine ≥ threshold + same type
# + content-differs only (default since 2026-04-27, after KU-50% diagnostic
# revealed the title gate blocked 98% of would-be supersedences on bench
# corpora with empty/generic titles); 'off' = treat ALL high-cosine same-type
# pairs as supersedence regardless of title or content (research mode only).
CONTRADICTION_TITLE_GATE = os.environ.get("CONTRADICTION_TITLE_GATE", "loose").lower()
# CONTRADICTION_TYPE_EXCLUSIONS: comma-separated memory types skipped during
# contradiction-check (Phase 20 fix 2026-04-27). Default skips 'conversation'
# (whole-thread containers should never supersede each other) but ALLOWS
# 'message' so chat turns can contradict each other when contradiction-check
# is explicitly enabled. Set to 'conversation,message' to restore the
# legacy pre-2026-04-27 behavior. Empty string = check all types.
CONTRADICTION_TYPE_EXCLUSIONS = frozenset(
    t.strip() for t in os.environ.get("CONTRADICTION_TYPE_EXCLUSIONS", "conversation").split(",")
    if t.strip()
)
SEARCH_ROW_CAP         = int(os.environ.get("SEARCH_ROW_CAP", "500"))
LLM_TIMEOUT            = float(os.environ.get("LLM_TIMEOUT", "120.0"))

# Ranker/write-path tuning. See _augment_title_with_role and the scoring loop
# in memory_search_scored_impl. These are safe defaults; override via env var
# to disable or tune per deployment.
SPEAKER_IN_TITLE       = os.environ.get("M3_SPEAKER_IN_TITLE", "1").lower() in ("1", "true", "yes")
SHORT_TURN_THRESHOLD   = int(os.environ.get("M3_SHORT_TURN_THRESHOLD", "20"))
TITLE_MATCH_BOOST      = float(os.environ.get("M3_TITLE_MATCH_BOOST", "0.05"))
IMPORTANCE_WEIGHT      = float(os.environ.get("M3_IMPORTANCE_WEIGHT", "0.05"))

# Phase 1 ingestion optimizations. Three opt-in emitters (off by default) and
# one retrieval-side router. All safe-no-op when gated off. See the helpers
# _maybe_emit_event_rows / _maybe_emit_window_chunk / _maybe_emit_gist_row
# and _maybe_route_query for behavior.
INGEST_WINDOW_CHUNKS   = os.environ.get("M3_INGEST_WINDOW_CHUNKS", "0").lower() in ("1", "true", "yes")
INGEST_GIST_ROWS       = os.environ.get("M3_INGEST_GIST_ROWS", "0").lower() in ("1", "true", "yes")
INGEST_EVENT_ROWS      = os.environ.get("M3_INGEST_EVENT_ROWS", "0").lower() in ("1", "true", "yes")
QUERY_TYPE_ROUTING     = os.environ.get("M3_QUERY_TYPE_ROUTING", "0").lower() in ("1", "true", "yes")
# Intent-driven retrieval routing. When on, memory_search_scored_impl honors
# the intent_hint kwarg and applies two extras:
#   1. Role-biased score boost for user-authored turns when intent=user-fact.
#   2. Predecessor-turn pull (fetch turn N-1 when N was matched) so the user
#      statement behind an assistant echo lands in the result set.
# The hint is produced by bin/slm_intent.classify_intent() when its own gate
# is on; callers that already know the intent can pass it directly. Dormant
# otherwise — no caller, no cost. See the gate rationale in the SLM docstring.
INTENT_ROUTING         = os.environ.get("M3_INTENT_ROUTING", "0").lower() in ("1", "true", "yes")
INTENT_USER_FACT_BOOST = float(os.environ.get("M3_INTENT_USER_FACT_BOOST", "0.1"))
INGEST_WINDOW_SIZE     = int(os.environ.get("M3_INGEST_WINDOW_SIZE", "3"))
INGEST_GIST_MIN_TURNS  = int(os.environ.get("M3_INGEST_GIST_MIN_TURNS", "8"))
INGEST_GIST_STRIDE     = int(os.environ.get("M3_INGEST_GIST_STRIDE", "8"))

# Fact enrichment pipeline (Phase 4-5). Gated off by default.
ENABLE_FACT_ENRICHED   = os.environ.get("M3_ENABLE_FACT_ENRICHED", "false").lower() in ("1", "true", "yes")
FACT_ENRICH_CONCURRENCY = int(os.environ.get("M3_FACT_ENRICH_CONCURRENCY", "2"))
FACT_ENRICH_MAX_ATTEMPTS = int(os.environ.get("M3_FACT_ENRICH_MAX_ATTEMPTS", "5"))

# Entity-relation graph pipeline (Phase 4-5). Gated off by default.
ENABLE_ENTITY_GRAPH          = os.environ.get("M3_ENABLE_ENTITY_GRAPH", "false").lower() in ("1", "true", "yes")
ENTITY_EXTRACT_CONCURRENCY   = int(os.environ.get("M3_ENTITY_EXTRACT_CONCURRENCY", "2"))
ENTITY_EXTRACT_MAX_ATTEMPTS  = int(os.environ.get("M3_ENTITY_EXTRACTOR_MAX_ATTEMPTS",
                                   os.environ.get("M3_ENTITY_EXTRACT_MAX_ATTEMPTS", "3")))
ENTITY_RESOLVE_FUZZY_MIN     = float(os.environ.get("M3_ENTITY_RESOLVE_FUZZY_MIN", "0.8"))
ENTITY_RESOLVE_COSINE_MIN    = float(os.environ.get("M3_ENTITY_RESOLVE_COSINE_MIN", "0.85"))

# Entity/predicate enums — kept local to avoid circular import
# (mcp_tool_catalog imports memory_core; memory_core must not import mcp_tool_catalog).
# Wave 3 will re-export these from mcp_tool_catalog.py and memory_bridge.py.

# Bootstrap hardcoded defaults for when YAML is unavailable or malformed.
# These are mirrored exactly in config/lists/entity_graph_default.yaml for backward-compat.
_DEFAULT_VALID_ENTITY_TYPES = frozenset({
    "person", "place", "organization", "event", "concept", "object", "date",
})
_DEFAULT_VALID_ENTITY_PREDICATES = frozenset({
    "works_at", "located_in", "before", "after",
    "same_as", "contradicts", "mentions", "relates_to",
})

DEFAULT_ENTITY_VOCAB_YAML = Path(__file__).parent.parent / "config" / "lists" / "entity_graph_default.yaml"
# Env override: when set, load_entity_vocab(None) reads this YAML instead of
# DEFAULT_ENTITY_VOCAB_YAML. Production callers that import VALID_ENTITY_TYPES /
# VALID_ENTITY_PREDICATES at module load (e.g., _link_entity_relationship's
# validation) pick up the override automatically. Use config/lists/entity_graph_lme.yaml
# for LME-tuned vocab (adds 'attended' and 'purchased' predicates).
_ENV_ENTITY_VOCAB_YAML = os.environ.get("M3_ENTITY_VOCAB_YAML", "").strip() or None

# ──────────────────────────────────────────────────────────────────────────────
# Cross-encoder reranker (lazy-loaded)
# ──────────────────────────────────────────────────────────────────────────────
# Module-level singleton — pays the model-load cost only when rerank=True is
# first hit. Default model is the canonical ms-marco distilled cross-encoder
# (~120MB on disk, ~12MB resident weights), small + fast enough for per-query
# reranking at bench scale (~50ms / pair on GPU, ~200ms / pair on CPU).
#
# Alternative model: BAAI/bge-reranker-v2-m3 (~568MB, higher accuracy on
# multilingual; slower). Pass via rerank_model kwarg or M3_RERANK_MODEL env.
#
# CONTRACT: importing memory_core does NOT import sentence_transformers —
# only the first call to _get_reranker(...) does. This keeps cold-start fast
# for all callers that don't use rerank.
_RERANKER_MODEL = None  # CrossEncoder | None — lazy-init
_RERANKER_MODEL_NAME = ""
DEFAULT_RERANK_MODEL = os.environ.get(
    "M3_RERANK_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2"
).strip()


def _get_reranker(model_name: str):
    """Lazy-load + cache cross-encoder reranker.

    Reuses the cached instance if model_name matches the previously-loaded one;
    otherwise loads the new model (and discards the prior). GPU is used if
    available; falls back to CPU silently.

    Raises RuntimeError with a clear install hint if sentence-transformers is
    not importable (it is a hard dep in requirements.txt; missing import means
    the user has a broken install).
    """
    global _RERANKER_MODEL, _RERANKER_MODEL_NAME
    if _RERANKER_MODEL is not None and _RERANKER_MODEL_NAME == model_name:
        return _RERANKER_MODEL
    try:
        from sentence_transformers import CrossEncoder
    except ImportError as e:
        raise RuntimeError(
            f"rerank=True requires sentence-transformers (declared in "
            f"requirements.txt). Install/repair via: "
            f"pip install -r requirements.txt. Original error: {e}"
        ) from e
    try:
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        device = "cpu"
    _RERANKER_MODEL = CrossEncoder(model_name, device=device)
    _RERANKER_MODEL_NAME = model_name
    return _RERANKER_MODEL


def _apply_rerank(
    hits: list,
    query: str,
    *,
    pool_k: int,
    final_k: int,
    model_name: str,
    blend: float,
) -> list:
    """Re-score top-pool_k hits with cross-encoder; blend with hybrid score.

    Args:
        hits: list[tuple[float, dict]] — output shape of memory_search_*_impl
        query: user query string
        pool_k: how many top hits to rescore (rest are dropped if pool_k < len)
        final_k: how many top hits to return after rerank+blend
        model_name: cross-encoder model id (e.g. "cross-encoder/ms-marco-MiniLM-L-6-v2")
        blend: blend factor — final = blend * ce_score + (1 - blend) * hybrid_score
               1.0 = pure CE replacement (default), 0.5 = average, 0.0 = no-op

    Returns hits in same shape as input, sorted by blended score descending,
    truncated to final_k.

    CONTRACT: when blend=0.0, this is a no-op — returns input hits[:final_k]
    unmodified. Callers that pass rerank=True with blend=0.0 get the same
    behavior as rerank=False (no CE call made).
    """
    if not hits or final_k <= 0 or blend <= 0.0:
        return hits[:final_k]
    pool = hits[:max(pool_k, final_k)]  # never truncate below final_k
    if not pool:
        return []
    reranker = _get_reranker(model_name)
    # Build (query, content) pairs. Skip rows with empty content (rerank can't
    # score them; they fall back to hybrid score via blend).
    pairs = []
    pair_indices = []  # indices into pool that have content
    for i, (_, item) in enumerate(pool):
        content = (item.get("content") or "") if isinstance(item, dict) else ""
        if content:
            pairs.append([query, content])
            pair_indices.append(i)
    if not pairs:
        return pool[:final_k]
    ce_scores = reranker.predict(pairs, show_progress_bar=False)
    # Map ce_scores back to pool indices; rows with no content keep ce_score=0.
    pool_ce: list = [0.0] * len(pool)
    for idx, ce in zip(pair_indices, ce_scores):
        pool_ce[idx] = float(ce)
    # Blend
    blended: list = []
    for (hybrid_score, item), ce in zip(pool, pool_ce):
        new_score = blend * ce + (1.0 - blend) * hybrid_score
        blended.append((new_score, item))
    blended.sort(key=lambda t: t[0], reverse=True)
    return blended[:final_k]


def load_entity_vocab(yaml_path: str | Path | None = None) -> tuple[frozenset[str], frozenset[str]]:
    """Load entity-graph vocabulary (types + predicates) from YAML.

    Args:
        yaml_path: Path to YAML file. If None, loads default vocabulary.
                   If file's entity_types or entity_predicates is empty list, falls back to defaults.

    Returns:
        (entity_types, entity_predicates) — both frozensets.
    """
    # Resolution order: explicit yaml_path > M3_ENTITY_VOCAB_YAML env > default.
    # Env hook lets bench harnesses override the production VALID_ENTITY_PREDICATES
    # at import-time without code changes. See decision memory (this turn).
    if yaml_path is not None:
        path = Path(yaml_path)
    elif _ENV_ENTITY_VOCAB_YAML:
        path = Path(_ENV_ENTITY_VOCAB_YAML)
    else:
        path = DEFAULT_ENTITY_VOCAB_YAML
    if not path.exists():
        # Hard fallback to in-memory defaults if the file is missing
        return _DEFAULT_VALID_ENTITY_TYPES, _DEFAULT_VALID_ENTITY_PREDICATES

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        # Hard fallback if YAML is malformed
        return _DEFAULT_VALID_ENTITY_TYPES, _DEFAULT_VALID_ENTITY_PREDICATES

    types = data.get("entity_types") or []
    preds = data.get("entity_predicates") or []

    # Empty lists fall back to defaults (e.g., placeholder LME YAML before Task A populates it)
    if not types:
        types = list(_DEFAULT_VALID_ENTITY_TYPES)
    if not preds:
        preds = list(_DEFAULT_VALID_ENTITY_PREDICATES)

    return frozenset(types), frozenset(preds)


# Module-level: load defaults at import. Existing callers see same contents as before.
VALID_ENTITY_TYPES, VALID_ENTITY_PREDICATES = load_entity_vocab(None)

VALID_CHANGE_AGENTS = {"claude", "gemini", "aider", "openclaw", "deepseek", "grok", "manual", "system", "unknown", "legacy"}

_FTS_OPERATORS = re.compile(r'\b(OR|AND|NOT|NEAR)\b|[*()\[\]{}]')
def _sanitize_fts(query: str, max_len: int = 500) -> str:
    """Strip FTS5 operators from user input to prevent query injection."""
    if len(query) > max_len:
        query = query[:max_len]
    return _FTS_OPERATORS.sub(' ', query).strip()


_TOKEN_SPLIT = re.compile(r"[^\w]+", re.UNICODE)

def _augment_title_with_role(title: str, metadata: str | dict | None) -> str:
    """Prepend '[role] ' to title when metadata carries a person-name role.

    Makes the speaker visible to FTS so queries like 'what did Caroline say
    about X' can match turns by Caroline. Idempotent: skips when title is
    already bracket-prefixed. Gated by SPEAKER_IN_TITLE.
    """
    if not SPEAKER_IN_TITLE:
        return title or ""
    t = (title or "").strip()
    if t.startswith("["):
        return t
    if not metadata:
        return t
    try:
        meta = metadata if isinstance(metadata, dict) else json.loads(metadata)
    except (json.JSONDecodeError, TypeError):
        return t
    role = (meta.get("role") or "").strip()
    # Only prepend when role looks like a proper name (avoid 'user'/'assistant'
    # generics which add noise without helping real-world queries).
    if not role or role.lower() in ("user", "assistant", "system", "tool"):
        return t
    return f"[{role}] {t}".strip()


def _query_title_overlap(query: str, title: str) -> float:
    """Fraction of query tokens that also appear in title. 0.0 when no overlap.

    Used as a small ranker boost for titles that literally echo query terms.
    """
    if not query or not title:
        return 0.0
    q_tokens = {t for t in _TOKEN_SPLIT.split(query.lower()) if len(t) > 2}
    if not q_tokens:
        return 0.0
    t_tokens = {t for t in _TOKEN_SPLIT.split(title.lower()) if len(t) > 2}
    if not t_tokens:
        return 0.0
    overlap = q_tokens & t_tokens
    return len(overlap) / len(q_tokens)


# Always-on: lift resolved temporal anchors into embed_text so FTS and vector
# search can match on absolute dates even when the original text says
# "yesterday" or "last month". Caller supplies anchors via
# metadata["temporal_anchors"] (list of iso strings); a no-op when absent.
def _augment_embed_text_with_anchors(embed_text: str, metadata: str | dict | None) -> str:
    if not embed_text:
        return embed_text
    if not metadata:
        return embed_text
    try:
        meta = metadata if isinstance(metadata, dict) else json.loads(metadata)
    except (json.JSONDecodeError, TypeError):
        return embed_text
    anchors = meta.get("temporal_anchors")
    if not isinstance(anchors, (list, tuple)) or not anchors:
        return embed_text
    tags: list[str] = []
    for a in anchors:
        if not a:
            continue
        if isinstance(a, str):
            tags.append(a[:10])
        elif isinstance(a, dict):
            v = a.get("iso") or a.get("date") or a.get("value")
            if isinstance(v, str):
                tags.append(v[:10])
    if not tags:
        return embed_text
    return "[" + ", ".join(tags) + "] " + embed_text


# Heuristic event extraction. Matches "<Name> <verb> ... <date-ish>" patterns
# in a single turn. Returns a list of (sentence, verb) pairs. Emitted as
# type='event_extraction' rows by _maybe_emit_event_rows.
_EVENT_VERB_LIST = (
    "went", "visited", "met", "started", "joined", "attended", "bought",
    "moved", "celebrated", "finished", "began", "saw", "watched", "played",
    "traveled", "arrived", "left", "returned", "called", "texted", "married",
    "graduated", "quit", "hired", "adopted", "painted",
)
_EVENT_PROPER_NOUN = re.compile(r"\b([A-Z][a-z]{2,})\b")
_EVENT_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_EVENT_DATE_HINT = re.compile(
    r"\b(yesterday|today|tomorrow|last|this|next|ago|on\s+\d|"
    r"january|february|march|april|may|june|july|august|"
    r"september|october|november|december|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday|\d{4})\b",
    re.IGNORECASE,
)
_EVENT_VERB_RE = re.compile(
    r"\b(" + "|".join(_EVENT_VERB_LIST) + r")\b", re.IGNORECASE
)


def _extract_event_sentences(content: str) -> list[tuple[str, str]]:
    """Return list of (sentence, verb) for sentences that mention a proper
    noun, one of the event verbs, and a date-ish token. Cheap regex only."""
    if not content:
        return []
    out: list[tuple[str, str]] = []
    for sent in _EVENT_SENT_SPLIT.split(content):
        s = sent.strip()
        if len(s) < 12 or len(s) > 400:
            continue
        if not _EVENT_PROPER_NOUN.search(s):
            continue
        m = _EVENT_VERB_RE.search(s)
        if not m:
            continue
        if not _EVENT_DATE_HINT.search(s):
            continue
        out.append((s, m.group(1).lower()))
        if len(out) >= 4:
            break
    return out


# Query-type routing for retrieval. When QUERY_TYPE_ROUTING is on and a query
# looks like "When/what date ... <ProperNoun>", shift vector_weight toward
# BM25 so proper-noun signal doesn't get diluted by embedding similarity.
_TEMPORAL_QUERY_RE = re.compile(
    r"\b(when|what\s+date|which\s+day|on\s+what)\b", re.IGNORECASE
)


def _pull_predecessor_turns(scored: list) -> None:
    """Append turn N-1 to ``scored`` when turn N is already present.

    Used under M3_INTENT_ROUTING with intent_hint="user-fact" — bridges
    the gap where the assistant echo is the best FTS match but the
    user's original statement (one turn earlier) carries the actual
    fact. Mutates the list in-place with the predecessor scored at
    ~85% of the original turn's score so it competes but doesn't
    automatically displace.

    Caps at the top 10 current hits to bound extra DB work; most
    user-fact queries only need a few predecessors, not a bulk pull.
    Items without ``conversation_id`` or ``metadata_json.turn_index``
    are skipped.
    """
    candidates: list[tuple[str, int, float]] = []  # (cid, target_idx, parent_score)
    seen_ids = {item.get("id") for _, item in scored if item.get("id")}
    for score, item in scored[:10]:
        cid = item.get("conversation_id")
        meta_raw = item.get("metadata_json")
        if not cid or not meta_raw:
            continue
        try:
            meta = json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
            t_idx = meta.get("turn_index")
            if isinstance(t_idx, int) and t_idx > 0:
                candidates.append((cid, t_idx - 1, score))
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
    if not candidates:
        return
    # Single query per candidate keeps the code simple; at top-10 * 1 row
    # each that's 10 point-lookups on an indexed table, well under 10ms.
    try:
        with _db() as db:
            for cid, target_idx, parent_score in candidates:
                row = db.execute(
                    "SELECT id, content, title, type, importance, metadata_json, conversation_id "
                    "FROM memory_items "
                    "WHERE conversation_id = ? AND is_deleted = 0 "
                    "  AND json_extract(metadata_json, '$.turn_index') = ?",
                    (cid, target_idx),
                ).fetchone()
                if row is None or row["id"] in seen_ids:
                    continue
                seen_ids.add(row["id"])
                pre_item = {
                    "id": row["id"],
                    "content": row["content"],
                    "title": row["title"],
                    "type": row["type"],
                    "importance": row["importance"],
                    "metadata_json": row["metadata_json"],
                    "conversation_id": row["conversation_id"],
                }
                scored.append((parent_score * 0.85, pre_item))
    except Exception as e:  # defensive — predecessor pull is best-effort
        logger.debug(f"predecessor pull skipped: {type(e).__name__}: {e}")


def _maybe_route_query(query: str, vector_weight: float, intent_hint: str = "") -> float:
    """Decide whether to shift vector_weight toward BM25 based on query shape.

    Two triggers — an SLM-supplied intent hint takes precedence, then the
    heuristic fires as a fallback:
      - intent_hint in {"temporal-reasoning", "multi-session"} → 0.3
      - QUERY_TYPE_ROUTING on AND query starts with "when/what date/..."
        AND contains a proper noun → 0.3
    Both require the M3_QUERY_TYPE_ROUTING env gate. intent-hint path
    ALSO works standalone when M3_INTENT_ROUTING is on (so bench callers
    can opt in without touching both knobs).
    """
    # Intent-hint path: trusted signal from an upstream classifier.
    if intent_hint and (QUERY_TYPE_ROUTING or INTENT_ROUTING):
        if intent_hint in ("temporal-reasoning", "multi-session"):
            return 0.3
    # Heuristic path: unchanged from before.
    if not QUERY_TYPE_ROUTING:
        return vector_weight
    if not query:
        return vector_weight
    if not _TEMPORAL_QUERY_RE.search(query):
        return vector_weight
    if not _EVENT_PROPER_NOUN.search(query):
        return vector_weight
    return 0.3


async def _maybe_emit_event_rows(
    content: str,
    metadata: str | dict | None,
    conversation_id: str,
    user_id: str,
    parent_id: str,
) -> None:
    """Extract event-like sentences from a message and emit one
    type='event_extraction' row per match, linked back to the parent via
    `references`. Embed_text includes resolved temporal anchors so date
    queries can hit these rows directly. Idempotent: skipped if the caller
    did not provide a conversation_id."""
    if not conversation_id:
        return
    events = _extract_event_sentences(content)
    if not events:
        return
    meta_dict: dict[str, Any] = {}
    if metadata:
        try:
            meta_dict = metadata if isinstance(metadata, dict) else json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            meta_dict = {}
    session_id = meta_dict.get("session_id", "")
    for sent, verb in events:
        ev_meta = {
            "source_message_id": parent_id,
            "verb": verb,
            "session_id": session_id,
            "temporal_anchors": meta_dict.get("temporal_anchors") or [],
        }
        try:
            created = await memory_write_impl(
                type="event_extraction",
                content=sent,
                title=f"event:{verb}",
                metadata=json.dumps(ev_meta),
                user_id=user_id,
                source="event_extraction",
                conversation_id=conversation_id,
                embed=True,
            )
            m = re.search(r"Created:\s*([a-f0-9-]+)", created or "")
            if m:
                try:
                    memory_link_impl(m.group(1), parent_id, "references")
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"event_extraction emit failed: {e}")


async def _maybe_emit_window_chunk(conversation_id: str, user_id: str) -> None:
    """Emit a sliding 3-turn (INGEST_WINDOW_SIZE) summary row that embeds the
    concatenated text of the most recent N message rows in a conversation.
    Fires only on turns whose count is a multiple of the window size, so a
    conversation of 9 turns emits 3 window rows rather than 9 overlapping
    ones. Does not fire until at least INGEST_WINDOW_SIZE turns exist."""
    if not conversation_id:
        return
    try:
        with _db() as db:
            rows = db.execute(
                "SELECT id, content, title FROM memory_items "
                "WHERE conversation_id = ? AND type = 'message' "
                "AND is_deleted = 0 ORDER BY created_at ASC",
                (conversation_id,),
            ).fetchall()
    except Exception as e:
        logger.debug(f"window chunk query failed: {e}")
        return
    n = len(rows)
    if n < INGEST_WINDOW_SIZE or (n % INGEST_WINDOW_SIZE) != 0:
        return
    window_rows = rows[-INGEST_WINDOW_SIZE:]
    joined = "\n".join((r["content"] or "") for r in window_rows if r["content"])
    if not joined.strip():
        return
    try:
        await memory_write_impl(
            type="summary",
            content=joined,
            title=f"window:{conversation_id}:{n}",
            metadata=json.dumps({
                "kind": "window_chunk",
                "window_end_turn": n,
                "window_size": INGEST_WINDOW_SIZE,
                "source_message_ids": [r["id"] for r in window_rows],
            }),
            user_id=user_id,
            source="window_chunk",
            conversation_id=conversation_id,
            embed=True,
        )
    except Exception as e:
        logger.debug(f"window chunk emit failed: {e}")


async def _maybe_emit_gist_row(conversation_id: str, user_id: str) -> None:
    """Emit a heuristic gist row for a conversation once it has passed
    INGEST_GIST_MIN_TURNS turns, and every INGEST_GIST_STRIDE additional
    turns thereafter. The gist concatenates the first sentence of each
    message and a deduped list of capitalized tokens seen across the
    conversation — cheap, deterministic, no LLM."""
    if not conversation_id:
        return
    try:
        with _db() as db:
            rows = db.execute(
                "SELECT id, content FROM memory_items "
                "WHERE conversation_id = ? AND type = 'message' "
                "AND is_deleted = 0 ORDER BY created_at ASC",
                (conversation_id,),
            ).fetchall()
    except Exception as e:
        logger.debug(f"gist query failed: {e}")
        return
    n = len(rows)
    if n < INGEST_GIST_MIN_TURNS:
        return
    if ((n - INGEST_GIST_MIN_TURNS) % INGEST_GIST_STRIDE) != 0:
        return
    sentences: list[str] = []
    entities: list[str] = []
    seen_ent: set[str] = set()
    for r in rows:
        c = (r["content"] or "").strip()
        if not c:
            continue
        first = _EVENT_SENT_SPLIT.split(c, maxsplit=1)[0]
        if first:
            sentences.append(first[:200])
        for m in _EVENT_PROPER_NOUN.findall(c):
            if m not in seen_ent:
                seen_ent.add(m)
                entities.append(m)
            if len(entities) >= 16:
                break
    if not sentences:
        return
    gist = " | ".join(sentences[:12])
    if entities:
        gist = f"[{', '.join(entities[:16])}] {gist}"
    try:
        await memory_write_impl(
            type="summary",
            content=gist,
            title=f"gist:{conversation_id}:{n}",
            metadata=json.dumps({
                "kind": "conversation_gist",
                "turn_count": n,
                "entities": entities[:16],
            }),
            user_id=user_id,
            source="conversation_gist",
            conversation_id=conversation_id,
            embed=True,
        )
    except Exception as e:
        logger.debug(f"gist emit failed: {e}")


_POISON_PATTERNS = [
    re.compile(r'<script\b', re.I),
    re.compile(r'(?:DROP|DELETE|ALTER)\s+TABLE', re.I),
    re.compile(r'__import__|\bexec\s*\(|\beval\s*\(', re.I),
    re.compile(r'(?:ignore|disregard)\s+(?:all\s+)?(?:previous|prior)\s+instructions', re.I),
]

def _check_content_safety(content: str) -> str | None:
    """Returns error message if content appears malicious, None if safe."""
    if not content:
        return None
    for pattern in _POISON_PATTERNS:
        if pattern.search(content):
            return f"Error: content rejected — matches safety pattern: {pattern.pattern[:50]}"
    return None

DEFAULT_CHANGE_AGENT = "unknown"

CHROMA_BASE_URL     = os.environ.get("CHROMA_BASE_URL")
CHROMA_COLLECTION   = "agent_memory"
CHROMA_COLLECTIONS  = ["agent_memory", "home_memory", "user_facts"]
CHROMA_V2_PREFIX    = "/api/v2/tenants/default_tenant/databases/default_database/collections"
CHROMA_CONNECT_T    = 3.0
CHROMA_READ_T       = 10.0
CHROMA_PULL_PAGE_SIZE = 100
CHROMA_CONTENT_MAX    = 10_000
# Federation fires when the best local hit scores below this threshold.
# Lower = less federation; higher = more aggressive cross-peer supplementation.
# Override via M3_FEDERATION_LOW_SCORE_THRESHOLD env var (float, default 0.65).
FEDERATION_LOW_SCORE_THRESHOLD = float(os.environ.get("M3_FEDERATION_LOW_SCORE_THRESHOLD", "0.65"))

_local = threading.local()
_init_lock = threading.RLock()
_initialized = False
_EMBED_SEM = asyncio.Semaphore(4)
_FACT_ENRICH_SEM = asyncio.Semaphore(FACT_ENRICH_CONCURRENCY)
_ENTITY_EXTRACT_SEM = asyncio.Semaphore(ENTITY_EXTRACT_CONCURRENCY)
_EMBED_DIM_VALIDATED = False

_COST_COUNTERS = {"embed_calls": 0, "embed_tokens_est": 0, "search_calls": 0, "write_calls": 0}
_PENDING_FACT_TASKS: set[asyncio.Task] = set()
_PENDING_ENTITY_TASKS: set[asyncio.Task] = set()
_CLASSIFY_CACHE = {}

async def _auto_classify(content: str, title: str) -> str:
    """Uses the local LLM to classify a memory into a valid type."""
    c_hash = _content_hash(content + title)
    if c_hash in _CLASSIFY_CACHE:
        return _CLASSIFY_CACHE[c_hash]

    # Localized copy of mcp_tool_catalog.VALID_MEMORY_TYPES minus "auto"
    # (auto is the sentinel that requests classification, not a classifier output).
    # Kept local to avoid circular import: mcp_tool_catalog imports memory_core.
    # Keep this list in sync with mcp_tool_catalog.VALID_MEMORY_TYPES.
    valid_types = {
        "note", "fact", "decision", "preference", "conversation", "message",
        "task", "code", "config", "observation", "plan", "summary", "snippet",
        "reference", "log", "home", "user_fact", "scratchpad", "knowledge",
        "event_extraction", "chat_log",
        "local_device", "network_config", "infrastructure", "home_automation",
        "migration-log", "security",
        "windows_only", "macos_only", "linux_only", "to_do",
    }

    token = ctx.get_secret("LM_API_TOKEN") or "lm-studio"
    client = _get_embed_client()
    result = await get_best_llm(client, token)
    if not result:
        return "note"

    base_url, model = result
    prompt = (
        f"Classify this memory into exactly one type. Valid types: {', '.join(sorted(valid_types))}\n"
        f"Title: {title}\n"
        f"Content: {content[:500]}\n"
        f"Reply with ONLY the type name, nothing else."
    )

    try:
        resp = await client.post(
            f"{base_url}/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0
        )
        resp.raise_for_status()
        m_type = resp.json()["choices"][0]["message"]["content"].strip().lower()
        if m_type in valid_types:
            _CLASSIFY_CACHE[c_hash] = m_type
            return m_type
    except Exception as e:
        logger.debug(f"Auto-classification failed: {e}")

    return "note"


# ── Ingest-time LLM enrichment (opt-in) ──────────────────────────────────────
# Gated by env vars so behavior matches today's default (no extra LLM calls at
# write time) unless explicitly enabled. Intended for production callers that
# pass blank titles / want entity-tagged metadata without running heuristics
# themselves. All helpers fail-open: on any error, they return the untouched
# input so ingest never fails because LLM enrichment did.

def _ingest_llm_enabled(flag: str) -> bool:
    return os.environ.get(flag, "0").strip().lower() in ("1", "true", "yes", "on")

# ── Phase L: auto-activation of retrieval gates by data presence ───────────
# Phase J added M3_PREFER_OBSERVATIONS / M3_TWO_STAGE_OBSERVATIONS /
# M3_ENABLE_ENTITY_GRAPH as default-off env gates for back-compat. Phase L
# auto-flips them ON when the underlying tables have meaningful population,
# so users don't have to remember to flip env vars + restart after enrichment
# data lands. Escape hatch: M3_DISABLE_AUTO_ACTIVATION=1 falls back to
# explicit-env-only (used by bench harnesses for reproducibility).
_GATE_CACHE: dict[str, tuple[bool, float]] = {}
_GATE_CACHE_TTL = 300  # seconds; counts can change as drains run

def _gate_count_query(query: str) -> int:
    """Run a COUNT(*) query against the active SQLite DB. Returns 0 on error."""
    try:
        with _db() as db:
            row = db.execute(query).fetchone()
            if row is None:
                return 0
            return int(row[0] if not hasattr(row, "keys") else list(row)[0])
    except Exception:
        return 0

def _gate_active(env_var: str, count_query: str, threshold: int = 1) -> bool:
    """True if env var is explicitly on, or auto-activated by data presence.

    Cached per (env_var, count_query) for ~5 min; the cache is invalidated by
    process restart or natural TTL expiry. Single-process; no thread lock —
    a stampede on first miss would just run COUNT(*) twice, harmless.
    """
    if os.environ.get(env_var, "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    if os.environ.get("M3_DISABLE_AUTO_ACTIVATION", "").strip().lower() in ("1", "true", "yes", "on"):
        return False
    import time as _time
    cache_key = f"{env_var}::{count_query}"
    cached = _GATE_CACHE.get(cache_key)
    now = _time.monotonic()
    if cached is not None and (now - cached[1]) < _GATE_CACHE_TTL:
        return cached[0]
    count = _gate_count_query(count_query)
    active = count >= threshold
    _GATE_CACHE[cache_key] = (active, now)
    return active

_OBS_COUNT_QUERY = "SELECT COUNT(*) FROM memory_items WHERE type='observation' AND COALESCE(is_deleted,0)=0"
_ENTITY_COUNT_QUERY = "SELECT COUNT(*) FROM entities"

def _prefer_observations_gate() -> bool:
    return _gate_active("M3_PREFER_OBSERVATIONS", _OBS_COUNT_QUERY, threshold=100)

def _two_stage_observations_gate() -> bool:
    # Paired with PREFER_OBSERVATIONS: same trigger.
    return _gate_active("M3_TWO_STAGE_OBSERVATIONS", _OBS_COUNT_QUERY, threshold=100)

def _enable_entity_graph_gate() -> bool:
    return _gate_active("M3_ENABLE_ENTITY_GRAPH", _ENTITY_COUNT_QUERY, threshold=1)


_AUTO_TITLE_CACHE: dict[str, str] = {}
_AUTO_ENTITIES_CACHE: dict[str, list[str]] = {}

async def _maybe_auto_title(content: str, title: str, force: bool = False) -> str:
    """If M3_INGEST_AUTO_TITLE=1 and title is empty/trivial, ask a small LLM
    for a 4-8 word descriptive title derived from content. Returns the
    original title on any error or when the gate is off.

    A title is considered "trivial" if it is empty, a bare role prefix like
    "user:" or "assistant:", or shorter than 4 chars.

    Pass `force=True` to bypass both the env gate and the trivial-title
    check — callers that want to force LLM enrichment for a specific
    pipeline variant can opt in regardless of M3_INGEST_AUTO_TITLE.
    """
    if not force and not _ingest_llm_enabled("M3_INGEST_AUTO_TITLE"):
        return title
    if not content:
        return title
    if not force:
        t = (title or "").strip()
        trivial = (not t) or len(t) < 4 or t.rstrip(":").lower() in {
            "user", "assistant", "system", "tool", "msg", "note"
        }
        if not trivial:
            return title

    c_hash = _content_hash(content[:800])
    if c_hash in _AUTO_TITLE_CACHE:
        return _AUTO_TITLE_CACHE[c_hash]

    try:
        token = ctx.get_secret("LM_API_TOKEN") or "lm-studio"
        client = _get_embed_client()
        result = await get_smallest_llm(client, token)
        if not result:
            return title
        base_url, model = result
        prompt = (
            "Summarize the following text as a concise title of 4 to 8 words. "
            "Do not use quotes. Do not add a trailing period. No prefix.\n\n"
            f"{content[:600]}"
        )
        resp = await client.post(
            f"{base_url}/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 32,
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=20.0,
        )
        resp.raise_for_status()
        out = (resp.json()["choices"][0]["message"]["content"] or "").strip()
        # Strip wrapping quotes and trailing punctuation
        out = out.strip("\"'").rstrip(".!?,;:").strip()
        if not out or len(out) > 120:
            return title
        _AUTO_TITLE_CACHE[c_hash] = out
        return out
    except Exception as e:
        logger.debug(f"auto-title failed: {e}")
        return title


async def _maybe_auto_entities(content: str, force: bool = False) -> list[str]:
    """If M3_INGEST_AUTO_ENTITIES=1, ask a small LLM for up to 8 salient
    entities / named concepts in `content`. Returns [] on any error or when
    the gate is off. Callers typically store the result under
    metadata["entities"] and include it in embed_text for retrieval boost.

    Pass `force=True` to bypass the env gate — callers that want per-variant
    LLM enrichment can opt in regardless of M3_INGEST_AUTO_ENTITIES.
    """
    if not force and not _ingest_llm_enabled("M3_INGEST_AUTO_ENTITIES"):
        return []
    if not content:
        return []
    c_hash = _content_hash(content[:800])
    if c_hash in _AUTO_ENTITIES_CACHE:
        return list(_AUTO_ENTITIES_CACHE[c_hash])

    try:
        token = ctx.get_secret("LM_API_TOKEN") or "lm-studio"
        client = _get_embed_client()
        result = await get_smallest_llm(client, token)
        if not result:
            return []
        base_url, model = result
        prompt = (
            "List up to 8 salient entities or named concepts from the text. "
            "Reply with a JSON array of strings, nothing else.\n\n"
            f"{content[:600]}"
        )
        resp = await client.post(
            f"{base_url}/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 128,
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=20.0,
        )
        resp.raise_for_status()
        raw = (resp.json()["choices"][0]["message"]["content"] or "").strip()
        # Be lenient: strip code fences and pull the first JSON array.
        raw = raw.strip("`").strip()
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end < 0 or end <= start:
            return []
        parsed = json.loads(raw[start:end + 1])
        if not isinstance(parsed, list):
            return []
        ents = [str(x).strip() for x in parsed if isinstance(x, (str, int, float)) and str(x).strip()]
        ents = ents[:8]
        _AUTO_ENTITIES_CACHE[c_hash] = ents
        return list(ents)
    except Exception as e:
        logger.debug(f"auto-entities failed: {e}")
        return []


def _track_cost(operation: str, tokens_est: int = 0):
    _COST_COUNTERS[operation] = _COST_COUNTERS.get(operation, 0) + 1
    if tokens_est:
        _COST_COUNTERS["embed_tokens_est"] += tokens_est

def _ensure_sync_tables(db_path: str | None = None) -> None:
    """Run pending migrations against the active DB.

    Fast path: if the schema is already at the latest version on disk
    (compared against the migration files in memory/migrations/ or
    memory/chatlog_migrations/), skip the subprocess entirely. The
    `migrate_memory.py up` invocation triggers a backup-then-apply
    cycle that takes a noticeable amount of time on multi-GB DBs
    (#46) and timed out at 300s on the 41 GB agent_test_bench.db.
    A cheap `SELECT MAX(version) FROM schema_versions` against the
    target file lets us skip when there's nothing to apply.

    Belt-and-braces: when the active DB is a chatlog DB (path matches
    the chatlog config OR schema fingerprint says so), pass --target
    chatlog so the runner doesn't try to apply main-stack migrations
    to it. The runner now also refuses such mismatches in F1 hardening,
    but invoking the right --target up-front avoids a noisy refusal
    log line on every chatlog-context call.
    """
    import re
    import sqlite3
    import subprocess
    try:
        migration_script = os.path.join(BASE_DIR, "bin", "migrate_memory.py")

        # Detect chatlog context via schema fingerprint. Path equality with
        # chatlog_config.chatlog_db_path() is unreliable here because
        # chatlog_config inherits from M3_DATABASE, so a misdirected
        # M3_DATABASE makes both paths agree without telling us anything
        # about the file's actual schema. The classifier reads the file.
        active = db_path or resolve_db_path(None)
        target_flag: list[str] = []
        target_kind = "main"
        try:
            sys.path.insert(0, os.path.join(BASE_DIR, "bin"))
            from migrate_memory import _classify_db
            if _classify_db(active) == "chatlog":
                target_flag = ["--target", "chatlog"]
                target_kind = "chatlog"
        except Exception:
            pass

        # Fast path: compare DB's applied version vs. the highest .up.sql file
        # number for the resolved target. If equal, no migrations to apply,
        # skip the subprocess + backup + load entirely. Failure to read either
        # side falls through to the subprocess (which is what we'd do anyway).
        try:
            mig_dir = os.path.join(
                BASE_DIR, "memory",
                "chatlog_migrations" if target_kind == "chatlog" else "migrations",
            )
            file_versions = []
            pattern = re.compile(r"^(\d+)_.*\.up\.sql$")
            for fn in os.listdir(mig_dir):
                m = pattern.match(fn)
                if m:
                    file_versions.append(int(m.group(1)))
            latest_on_disk = max(file_versions) if file_versions else -1

            db_latest = -1
            conn = sqlite3.connect(f"file:{active}?mode=ro", uri=True, timeout=2.0)
            try:
                cur = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('schema_versions','schema_migrations')"
                ).fetchall()
                tables = {r[0] for r in cur}
                if "schema_versions" in tables:
                    row = conn.execute("SELECT MAX(version) FROM schema_versions").fetchone()
                    db_latest = int(row[0]) if row and row[0] is not None else -1
                elif "schema_migrations" in tables:
                    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
                    db_latest = int(row[0]) if row and row[0] is not None else -1
            finally:
                conn.close()

            if latest_on_disk >= 0 and db_latest >= latest_on_disk:
                # Already at latest — nothing to do. Avoids the multi-GB
                # backup-then-noop subprocess.
                return
        except Exception:
            # Any failure here just means we fall through to the full
            # subprocess path; never block the caller because the
            # fast-path check tripped.
            pass

        # "up --yes" + stdin=DEVNULL are both required: without --yes the prompt
        # would EOF-read an empty string and silently skip migrations, leaving
        # the DB missing tasks/chatlog tables. DEVNULL belt-and-braces in case
        # any future code path still reaches input(). Timeout 300s handles
        # backups of multi-GB databases (#46).
        env = os.environ.copy()
        if target_flag:
            # Pin the runner at the chatlog file we resolved, so a misdirected
            # M3_DATABASE in the parent env doesn't repoint it elsewhere.
            env["M3_DATABASE"] = active
        subprocess.run(
            [sys.executable, migration_script, "up", "--yes", *target_flag],
            check=True,
            timeout=300,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except Exception as e:
        logger.exception(f"_ensure_sync_tables failed: {e}")

def _backfill_change_agent() -> None:
    try:
        with _db() as db:
            rows = db.execute("SELECT id, agent_id, model_id FROM memory_items WHERE change_agent IS NULL").fetchall()
            for row in rows:
                agent = _infer_change_agent_util(row["agent_id"] or "", row["model_id"] or "", default="legacy")
                db.execute("UPDATE memory_items SET change_agent = ? WHERE id = ?", (agent, row["id"]))
    except Exception as e:
        logger.warning(f"Backfill failed: {e}")

_initialized_dbs: set[str] = set()

def _lazy_init(db_path: str | None = None) -> None:
    """Run one-time schema + backfill per DB path.

    Previously a single module flag guarded init; multi-DB requires per-path
    tracking so a fresh test/benchmark DB gets its sync tables created on
    first touch. Uses _init_lock for cross-thread safety.
    """
    global _initialized  # kept for backward compat with any external probes
    key = db_path or resolve_db_path(None)
    with _init_lock:
        if key in _initialized_dbs:
            return
        _initialized_dbs.add(key)
        _initialized = True  # legacy flag — once true, stays true
        try:
            _ensure_sync_tables(key)
            _backfill_change_agent()
        except Exception:
            # Do not trap init in a permanently-failed state — let the next
            # caller retry (removes the key so it's reattempted).
            _initialized_dbs.discard(key)
            raise

@contextmanager
def _db():
    active_ctx = _current_ctx()
    _lazy_init(active_ctx.db_path)
    with active_ctx.get_sqlite_conn() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

@contextmanager
def _conn():
    """Legacy alias for _db context manager (C7)."""
    with _db() as db:
        yield db

def _record_history(memory_id: str, event: str, prev_value: str = None, new_value: str = None, field: str = "content", actor_id: str = "", db=None):
    """Records a change event in the memory_history audit trail.

    Pass ``db`` when the caller already holds an open connection (e.g. inside
    a ``with _db() as db:`` block). Opening a second pool connection while
    the outer one has an uncommitted writer causes SQLite WAL writer
    contention, which burns the full ``busy_timeout`` per call.
    """
    row = (str(uuid.uuid4()), memory_id, event, prev_value, new_value, field, actor_id)
    sql = "INSERT INTO memory_history (id, memory_id, event, prev_value, new_value, field, actor_id) VALUES (?,?,?,?,?,?,?)"
    try:
        if db is not None:
            db.execute(sql, row)
        else:
            with _db() as inner:
                inner.execute(sql, row)
    except Exception as e:
        logger.debug(f"History recording failed: {e}")

def memory_history_impl(memory_id: str, limit: int = 20) -> str:
    """Returns the change history for a memory item."""
    with _db() as db:
        rows = db.execute(
            "SELECT event, field, prev_value, new_value, actor_id, created_at FROM memory_history WHERE memory_id = ? ORDER BY created_at DESC LIMIT ?",
            (memory_id, limit)
        ).fetchall()
    if not rows:
        return f"No history found for {memory_id}"
    lines = [f"History for {memory_id} ({len(rows)} events):"]
    for r in rows:
        prev = (r["prev_value"] or "")[:80]
        new = (r["new_value"] or "")[:80]
        lines.append(f"  [{r['created_at']}] {r['event']} ({r['field']}) by {r['actor_id'] or 'unknown'}: {prev!r} -> {new!r}")
    return "\n".join(lines)

def _content_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()

# Shared Async Client
import httpx as _httpx

_shared_embed_client: _httpx.AsyncClient | None = None

def _get_embed_client() -> _httpx.AsyncClient:
    return ctx.get_async_client()

async def _embed(text: str) -> tuple[list[float] | None, str]:
    global _EMBED_DIM_VALIDATED
    c_hash = _content_hash(text)
    try:
        with _db() as db:
            cached = db.execute("SELECT embedding, embed_model FROM memory_embeddings WHERE content_hash = ? AND embed_model = ? LIMIT 1", (c_hash, EMBED_MODEL)).fetchone()
            if cached: return _unpack(cached["embedding"]), cached["embed_model"]
    except Exception as e:
        logger.debug(f"Embedding cache lookup failed: {e}")

    # Acquire semaphore with timeout to prevent deadlock under load
    try:
        await asyncio.wait_for(_EMBED_SEM.acquire(), timeout=30.0)
    except asyncio.TimeoutError:
        logger.error("Embedding semaphore acquire timed out after 30s")
        return None, EMBED_MODEL

    try:
        _track_cost("embed_calls", len(text.split()) * 2)
        token = ctx.get_secret("LM_API_TOKEN") or "lm-studio"
        client = _get_embed_client()
        result = await get_best_embed(client, token)
        if not result: return None, EMBED_MODEL
        base_url, model = result

        last_exc = None
        for attempt in range(3):
            try:
                resp = await client.post(
                    f"{base_url}/embeddings",
                    json={"model": model, "input": text},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=_httpx.Timeout(CHROMA_CONNECT_T, read=EMBED_TIMEOUT_READ)
                )
                resp.raise_for_status()
                emb = resp.json()["data"][0]["embedding"]

                if not _EMBED_DIM_VALIDATED:
                    if len(emb) != EMBED_DIM:
                        logger.error(f"Embedding dimension mismatch: got {len(emb)}, expected EMBED_DIM={EMBED_DIM}. Update EMBED_DIM env var.")
                    _EMBED_DIM_VALIDATED = True

                return emb, model
            except Exception as e:
                last_exc = e
                if attempt < 2:
                    wait = 2 * (2 ** attempt)
                    logger.warning(f"Embedding attempt {attempt + 1} failed: {e}. Retrying in {wait}s...")
                    await asyncio.sleep(wait)

        logger.error(f"Embedding generation failed after 3 attempts: {last_exc}")
        # Forget the cached endpoint so the next call re-discovers (endpoint
        # may have gone down or model may have been unloaded).
        from llm_failover import clear_embed_cache
        clear_embed_cache()
        return None, model
    finally:
        _EMBED_SEM.release()


# Tuned against llama-server --parallel 4 + --ubatch-size 4096:
# 4 in-flight chunks × 1024 texts/chunk ≈ 161 embeds/sec on RTX 5080.
EMBED_BULK_CHUNK = int(os.environ.get("EMBED_BULK_CHUNK", "1024"))
EMBED_BULK_CONCURRENCY = int(os.environ.get("EMBED_BULK_CONCURRENCY", "4"))
_EMBED_BULK_SEM = asyncio.Semaphore(EMBED_BULK_CONCURRENCY)


async def _embed_many(texts: list[str]) -> list[tuple[list[float] | None, str]]:
    """Batched embed path that bypasses the per-call semaphore and posts many
    inputs in a single /embeddings request. Honors the content-hash cache so
    repeated texts cost nothing. Returns a list aligned with `texts`."""
    if not texts:
        return []

    out: list[tuple[list[float] | None, str] | None] = [None] * len(texts)

    # Cache lookup: dedupe by content_hash, fetch any cached rows in one pass.
    hashes = [_content_hash(t) for t in texts]
    uniq_hashes = list(set(hashes))
    cached_vecs: dict[str, tuple[list[float], str]] = {}
    try:
        with _db() as db:
            placeholders = ",".join("?" * len(uniq_hashes))
            rows = db.execute(
                f"SELECT content_hash, embedding, embed_model FROM memory_embeddings "
                f"WHERE embed_model = ? AND content_hash IN ({placeholders})",
                (EMBED_MODEL, *uniq_hashes),
            ).fetchall()
            for r in rows:
                cached_vecs[r["content_hash"]] = (_unpack(r["embedding"]), r["embed_model"])
    except Exception as e:
        logger.debug(f"Bulk embed cache lookup failed: {e}")

    # Fill cached slots; collect misses to embed.
    miss_indices: list[int] = []
    miss_texts: list[str] = []
    for i, (t, h) in enumerate(zip(texts, hashes)):
        hit = cached_vecs.get(h)
        if hit is not None:
            out[i] = hit
        else:
            miss_indices.append(i)
            miss_texts.append(t)

    if not miss_texts:
        return out  # type: ignore[return-value]

    _track_cost("embed_calls", sum(len(t.split()) * 2 for t in miss_texts))
    token = ctx.get_secret("LM_API_TOKEN") or "lm-studio"
    client = _get_embed_client()
    result = await get_best_embed(client, token)
    if not result:
        for i in miss_indices:
            out[i] = (None, EMBED_MODEL)
        return out  # type: ignore[return-value]
    base_url, model = result

    # Captured by _post_once's except handlers so the drop log can surface
    # the real reason. Shared across all concurrent chunks in this call.
    _last_embed_err: dict[str, str] = {"msg": ""}

    async def _post_once(chunk_texts: list[str]) -> list[list[float] | None] | None:
        """One POST. Returns vectors on success, None on failure (caller decides bisect).

        On failure, stashes the last error so the final drop log can surface
        why (e.g. HTTP 400 with "exceeds context size" — invisible before).
        """
        try:
            resp = await client.post(
                f"{base_url}/embeddings",
                json={"model": model, "input": chunk_texts},
                headers={"Authorization": f"Bearer {token}"},
                timeout=_httpx.Timeout(CHROMA_CONNECT_T, read=EMBED_TIMEOUT_READ * 4),
            )
            resp.raise_for_status()
            data = resp.json()["data"]
            ordered = sorted(data, key=lambda d: d.get("index", 0))
            return [d["embedding"] for d in ordered]
        except _httpx.HTTPStatusError as e:
            _last_embed_err["msg"] = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
            return None
        except Exception as e:
            _last_embed_err["msg"] = f"{type(e).__name__}: {e}"
            return None

    async def _post_chunk(chunk_texts: list[str]) -> list[list[float] | None]:
        """Post a chunk with retry + bisection on failure.

        If a batch fails 3 attempts, split it in half and recurse on both halves.
        Single-text failures (len==1) are surfaced as [None] so a single bad
        input never takes down its neighbors.
        """
        async with _EMBED_BULK_SEM:
            # Try up to 3 times at this chunk size.
            for attempt in range(3):
                result = await _post_once(chunk_texts)
                if result is not None:
                    return result
                if attempt < 2:
                    await asyncio.sleep(2 * (2 ** attempt))

        # All retries failed. Bisect if we can.
        if len(chunk_texts) == 1:
            reason = _last_embed_err.get("msg") or "unknown"
            logger.warning(
                f"Bulk embed: dropping single input of len={len(chunk_texts[0])} "
                f"after 3 attempts — last error: {reason}"
            )
            return [None]
        mid = len(chunk_texts) // 2
        logger.info(
            f"Bulk embed: bisecting failed chunk of {len(chunk_texts)} into "
            f"{mid} + {len(chunk_texts) - mid}"
        )
        left, right = await asyncio.gather(
            _post_chunk(chunk_texts[:mid]),
            _post_chunk(chunk_texts[mid:]),
        )
        return [*left, *right]

    # Split misses into chunks and fan out under _EMBED_BULK_SEM.
    chunks = [
        miss_texts[i : i + EMBED_BULK_CHUNK]
        for i in range(0, len(miss_texts), EMBED_BULK_CHUNK)
    ]
    chunk_results = await asyncio.gather(*(_post_chunk(c) for c in chunks))

    global _EMBED_DIM_VALIDATED
    flat: list[list[float] | None] = []
    for cr in chunk_results:
        flat.extend(cr)
    for local_i, vec in enumerate(flat):
        if vec is not None and not _EMBED_DIM_VALIDATED:
            if len(vec) != EMBED_DIM:
                logger.error(
                    f"Embedding dimension mismatch: got {len(vec)}, expected {EMBED_DIM}"
                )
            _EMBED_DIM_VALIDATED = True
        out[miss_indices[local_i]] = (vec, model)

    return out  # type: ignore[return-value]


async def memory_write_bulk_impl(
    items: list[dict],
    *,
    enrich: bool | None = None,
    check_contradictions: bool | None = None,
    emit_conversation: bool | None = None,
    variant: str | None = None,
    embed_key_enricher: "Callable[[str, dict], Awaitable[str]] | None" = None,
    embed_key_enricher_concurrency: int = 4,
    dual_embed: bool = False,
    fact_enricher: "Callable[[str], Awaitable[list[dict]]] | None" = None,
    fact_enricher_concurrency: int = 2,
    fact_enricher_variant_allowlist: set[str] | None = None,
    entity_extractor: "Callable[[str], Awaitable[dict]] | None" = None,
    entity_extractor_concurrency: int = 2,
    entity_extractor_variant_allowlist: "set[str] | None" = None,
) -> list[str]:
    """Bulk write that routes embeddings through `_embed_many`. Intended for
    benchmark / import paths where per-item contradiction detection would
    dominate wall-clock. Returns a list of item_ids (or empty string on failure).

    enrich=None means "inherit env gates" (M3_INGEST_AUTO_TITLE, M3_INGEST_AUTO_ENTITIES).
    True forces on, False forces off.

    check_contradictions=None means "off by default in bulk" (perf), True enables,
    False disables. Differs from single path because bulk may have thousands of items.

    emit_conversation=None means "on if conversation_id present and type==message"
    (mirror single path), False disables.

    variant is used as default when items don't set their own variant.

    enrich, check_contradictions, and emit_conversation are intentionally not
    exposed via MCP — they are bulk-only perf knobs used by benchmark and
    import drivers. Only variant is advertised on the memory_write MCP schema
    and via --variant on bench CLIs.

    dual_embed=True (default False) combines with embed_key_enricher to write
    TWO vectors per item instead of one: a 'default'-kind vector from the
    raw `content` (what single-session terse queries match best) AND an
    'enriched'-kind vector from the SLM-enriched embed_text (what multi-hop
    aggregation queries match best). Requires v022+ schema. When dual_embed
    is False (default), the enricher's output replaces the raw content in
    embed_text as before — single-vector, original behavior. When True but
    enricher is None, dual_embed is a no-op (only one thing to embed).

    Retrieval-side fusion (vector_kind_strategy kwarg on
    memory_search_scored_impl, upcoming commit) decides how to combine the
    two vectors at query time. 'max' takes per-memory_id max score across
    kinds.
    """
    if not items:
        return []

    now = datetime.now(timezone.utc).isoformat()
    prepared: list[dict] = []
    for it in items:
        mid = it.get("id") or str(uuid.uuid4())
        meta = it.get("metadata", "{}")
        if isinstance(meta, dict):
            meta = json.dumps(meta)
        scope = it.get("scope", "agent")
        if scope not in VALID_SCOPES:
            scope = "agent"
        content = it.get("content") or ""
        title = it.get("title") or ""
        agent = (
            (it.get("change_agent") or "").strip().lower()
            or _infer_change_agent_util(
                it.get("agent_id", ""), it.get("model_id", ""), default=DEFAULT_CHANGE_AGENT
            )
        )
        try:
            importance = float(it.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        expires_at = None
        if scope == "session":
            from datetime import timedelta
            expires_at = (
                datetime.now(timezone.utc) + timedelta(hours=24)
            ).isoformat()

        # Resolve auto_classify before adding to prepared
        item_type = it.get("type", "note")
        if it.get("auto_classify") and (not item_type or item_type == "auto"):
            item_type = await _auto_classify(content, title)

        # Resolve effective variant once so the leak gate below can check it.
        eff_variant = (it.get("variant") or variant) or None

        # Leak gate: reject `window:*` summary rows when the variant is NULL
        # (i.e. would land in real core memory). The bench harness emits
        # session-window summaries with title like 'window:<sessionhash>::<i>:<j>'
        # for retrieval debugging — those are valid when stamped under a
        # bench variant, but historically leaked into core memory via
        # bulk writes that didn't pass --variant. 644 such rows had to be
        # cleaned manually on 2026-04-28 (memory 372f49b0).
        # See task #189, decision b5abb7cc.
        if (
            item_type == "summary"
            and isinstance(title, str)
            and title.startswith("window:")
            and eff_variant is None
        ):
            logger.warning(
                f"memory_write_bulk_impl: rejecting window:* summary leak "
                f"(title={title[:60]!r}) — provide an explicit variant if intentional."
            )
            continue

        prepared.append(
            {
                "id": mid,
                "type": item_type,
                "title": title,
                "content": content,
                "metadata": meta,
                "agent_id": it.get("agent_id", ""),
                "model_id": it.get("model_id", ""),
                "change_agent": agent,
                "importance": importance,
                "source": it.get("source", "agent"),
                "user_id": it.get("user_id", ""),
                "scope": scope,
                "expires_at": expires_at,
                "valid_from": it.get("valid_from") or now,
                "valid_to": it.get("valid_to") or None,
                "conversation_id": it.get("conversation_id") or None,
                "refresh_on": it.get("refresh_on") or None,
                "refresh_reason": it.get("refresh_reason") or None,
                "embed": it.get("embed", True),
                "embed_text": None,  # Will be set after enrichment
                "variant": eff_variant,
            }
        )

    # Pre-enrichment phase: auto-title, auto-entities, augment embed_text.
    # This runs before embedding so enriched text is included in the embed vector.
    for p in prepared:
        # Resolve enrich flag: None -> check env gates, True -> force on, False -> force off
        if enrich is True:
            p["title"] = await _maybe_auto_title(p["content"], p["title"], force=True)
        elif enrich is None:
            p["title"] = await _maybe_auto_title(p["content"], p["title"], force=False)
        # else: enrich is False, skip auto-title

        # Auto-entities: similar gating pattern
        if enrich is True or (enrich is None and _ingest_llm_enabled("M3_INGEST_AUTO_ENTITIES")):
            ents = await _maybe_auto_entities(p["content"], force=(enrich is True))
            if ents:
                try:
                    meta_dict = json.loads(p["metadata"]) if isinstance(p["metadata"], str) else (p["metadata"] or {})
                except json.JSONDecodeError:
                    meta_dict = {}
                if isinstance(meta_dict, dict) and "entities" not in meta_dict:
                    meta_dict["entities"] = ents
                    p["metadata"] = json.dumps(meta_dict)

        # Augment title with role (single path does this at L2056)
        p["title"] = _augment_title_with_role(p["title"], p["metadata"])

        # Set embed_text with anchors after enrichment
        p["embed_text"] = _augment_embed_text_with_anchors(
            p["content"] or p["title"], p["metadata"]
        )

    # Optional hook: rewrite embed_text via caller-supplied async enricher.
    # The enricher receives (content, metadata_dict) and returns a string
    # that REPLACES embed_text for the vector / FTS-index path. The stored
    # `content` column is not touched — this is a "keys only, values verbatim"
    # enrichment. Intended for bench / import drivers that want to prepend
    # SLM-extracted atomic facts (LoCoMo `llm_v1` / LongMemEval contextual-keys
    # pattern). Errors fall back to the un-enriched embed_text for that item.
    #
    # When enrichment fires, we also persist the enriched text to
    # `metadata_json.enriched_embed_text` so post-hoc analysis can audit
    # SLM output quality without rerunning the embedder or the enricher.
    # The raw content stays verbatim in the `content` column; only the
    # metadata grows. Callers who want to strip this for disk-space
    # reasons can filter it out in a later pass.
    if embed_key_enricher is not None and prepared:
        sem = asyncio.Semaphore(max(1, int(embed_key_enricher_concurrency)))

        async def _enrich_one(p: dict) -> None:
            if not p.get("embed_text") or not p.get("embed"):
                return
            try:
                meta = p.get("metadata") or "{}"
                meta_dict = json.loads(meta) if isinstance(meta, str) else (meta or {})
            except (json.JSONDecodeError, TypeError):
                meta_dict = {}
            raw_content = p.get("content") or ""
            async with sem:
                try:
                    enriched = await embed_key_enricher(raw_content, meta_dict)
                except Exception as e:
                    logger.debug(f"embed_key_enricher failed on item {p.get('id')}: {e}")
                    return
                # Skip the pass-through case where the enricher returned the
                # raw content unchanged (e.g. bench short-turn skip shortcut).
                # Nothing to persist if nothing changed.
                if not enriched or enriched == raw_content:
                    return
                # Keep the anchor-prefix semantics: run anchors AFTER enrichment
                # so time-aware retrieval still works.
                enriched = _augment_embed_text_with_anchors(enriched, p.get("metadata"))
                # When dual_embed=True, preserve the pre-enrichment embed_text
                # so Phase 2 can emit a SECOND vector (vector_kind='default')
                # from the raw content. embed_text itself becomes the enriched
                # string so Phase 2's existing path emits the 'enriched' vector.
                if dual_embed:
                    p["_dual_default_embed_text"] = p["embed_text"]
                p["embed_text"] = enriched
                # Persist the enriched text into metadata for post-hoc audit.
                meta_dict["enriched_embed_text"] = enriched
                p["metadata"] = json.dumps(meta_dict)

        await asyncio.gather(*(_enrich_one(p) for p in prepared))

    # Phase 1: INSERT memory_items + chroma queue + history in one transaction.
    with _db() as db:
        for p in prepared:
            db.execute(
                "INSERT INTO memory_items (id, type, title, content, metadata_json, agent_id, model_id, "
                "change_agent, importance, source, origin_device, user_id, scope, expires_at, created_at, "
                "valid_from, valid_to, conversation_id, refresh_on, refresh_reason, content_hash, variant) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    p["id"], p["type"], p["title"], p["content"], p["metadata"],
                    p["agent_id"], p["model_id"], p["change_agent"], p["importance"],
                    p["source"], ORIGIN_DEVICE, p["user_id"], p["scope"], p["expires_at"],
                    now, p["valid_from"], p["valid_to"], p["conversation_id"],
                    p["refresh_on"], p["refresh_reason"],
                    hashlib.sha256((p["content"] or "").encode("utf-8")).hexdigest(),
                    p["variant"],
                ),
            )
            # NOTE: chroma_sync_queue insert moved to Phase 2 (post-embed) so
            # we don't enqueue rows whose embedding fails (orphan accumulation).
            _record_history(
                p["id"], "create", None, p["content"], "content",
                p["agent_id"] or p["change_agent"], db=db,
            )

    # Phase 2: batched embeddings for items that requested them.
    # Dedup by content_hash(text) so variants/kinds that share identical
    # text don't trigger duplicate embedder calls. Cache hits inside
    # _embed_many already handle DB-cached vectors, but this additionally
    # deduplicates within the current batch.
    #
    # Dual-embed: when p["_dual_default_embed_text"] is present, emit TWO
    # rows — vector_kind='default' from the raw pre-enrichment text and
    # vector_kind='enriched' from p["embed_text"]. Otherwise emit a single
    # vector_kind='default' row from p["embed_text"].
    to_embed = [p for p in prepared if p["embed"] and p["embed_text"]]
    if to_embed:
        hash_to_first: dict[str, int] = {}
        unique_texts: list[str] = []
        # List of (p, kind, idx) triples — one per vector to emit.
        emit_plan: list[tuple[dict, str, int]] = []

        def _schedule(p: dict, kind: str, text: str) -> None:
            h = _content_hash(text)
            if h not in hash_to_first:
                hash_to_first[h] = len(unique_texts)
                unique_texts.append(text)
            emit_plan.append((p, kind, hash_to_first[h]))

        for p in to_embed:
            raw = p.get("_dual_default_embed_text")
            if raw:
                _schedule(p, "default", raw)
                _schedule(p, "enriched", p["embed_text"])
            else:
                _schedule(p, "default", p["embed_text"])

        unique_vecs = await _embed_many(unique_texts)
        # Track per-item default-kind embed success so we only enqueue once.
        default_ok: set[str] = set()
        default_fail: set[str] = set()
        with _db() as db:
            for p, kind, idx in emit_plan:
                vec, m = unique_vecs[idx]
                if not vec:
                    if kind == "default":
                        default_fail.add(p["id"])
                    continue
                text_for_hash = (
                    p["_dual_default_embed_text"] if kind == "default" and p.get("_dual_default_embed_text")
                    else p["embed_text"]
                )
                db.execute(
                    "INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim, created_at, content_hash, vector_kind) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()), p["id"], _pack(vec), m, len(vec), now,
                        _content_hash(text_for_hash), kind,
                    ),
                )
                if kind == "default":
                    default_ok.add(p["id"])
            # Only enqueue chroma sync for items whose canonical default-kind
            # vector landed. This prevents orphan queue rows when the embed
            # server fails (e.g. context-size 400) — see chroma_sync_queue
            # orphan accumulation 2026-04-22.
            for p in to_embed:
                if p["id"] in default_ok:
                    db.execute(
                        "INSERT INTO chroma_sync_queue (memory_id, operation) VALUES (?,?)",
                        (p["id"], "upsert"),
                    )
        for mid in default_fail - default_ok:
            logger.warning(
                f"memory_write_bulk_impl: embed failed for {mid}; "
                f"skipping memory_embeddings + chroma_sync_queue insert"
            )

    # Phase 2.5: Fact enrichment (Phase 4 on-write hook).
    # Non-blocking per-row dispatch: tries semaphore, enqueues on miss.
    # Mirrors embed_key_enricher pattern at lines 1290-1327.
    if fact_enricher is not None and ENABLE_FACT_ENRICHED:
        for p in prepared:
            # Skip variant rows unless explicitly allowed
            item_variant = p.get("variant")
            if item_variant is not None and (fact_enricher_variant_allowlist is None or item_variant not in fact_enricher_variant_allowlist):
                continue

            # Get a DB connection for the non-blocking dispatch
            with _db() as db:
                try:
                    await _try_enrich_or_enqueue(
                        p["id"],
                        p.get("content") or "",
                        fact_enricher,
                        db,
                        variant=item_variant,
                        allowlist=fact_enricher_variant_allowlist
                    )
                except Exception as e:
                    logger.debug(f"fact enrichment dispatch failed for {p['id']}: {e}")

    # Phase 2.6: Entity extraction (Phase 4 on-write hook).
    # Non-blocking per-row dispatch: tries semaphore, enqueues on miss.
    # Mirrors Phase 2.5 fact enrichment pattern above.
    # fact_enriched rows are NOT extracted to prevent recursion.
    if entity_extractor is not None:
        for p in prepared:
            if p.get("type") == "fact_enriched":
                continue
            item_variant = p.get("variant")
            with _db() as db:
                try:
                    await _try_extract_or_enqueue(
                        p["id"],
                        p.get("content") or "",
                        entity_extractor,
                        db,
                        variant=item_variant,
                        allowlist=entity_extractor_variant_allowlist,
                    )
                except Exception as e:
                    logger.debug(f"entity extraction dispatch failed for {p['id']}: {e}")

    # Phase 3: Contradiction detection (if requested, with bounded concurrency).
    # Default is off in bulk (perf), must explicitly enable with check_contradictions=True.
    if check_contradictions is True:
        # Use semaphore to limit concurrency (avoid overwhelming LLM/search)
        sem = asyncio.Semaphore(8)

        async def check_one(p: dict) -> tuple[str, list[str]]:
            async with sem:
                # Only check if we have an embedding and type is not conversation/message
                vec_row = None
                with _db() as db:
                    r = db.execute(
                        "SELECT embedding FROM memory_embeddings WHERE memory_id = ? LIMIT 1",
                        (p["id"],)
                    ).fetchone()
                    if r:
                        vec_row = r

                if not vec_row or p["type"] in CONTRADICTION_TYPE_EXCLUSIONS:
                    return p["id"], []

                vec = _unpack(vec_row["embedding"])
                superseded_ids, _ = await _check_contradictions(
                    p["id"], p["content"], p["title"], vec, p["type"], p["agent_id"],
                    new_valid_from=p.get("valid_from"),
                )
                return p["id"], superseded_ids

        results = await asyncio.gather(*[check_one(p) for p in prepared], return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.debug(f"Contradiction check in bulk failed: {result}")

    # Phase 4: Conversation emitters (event rows, window chunks, gist rows).
    # Default behavior: emit if conversation_id is present and type==message (mirror single path).
    # Can be disabled with emit_conversation=False.
    if emit_conversation is not False:  # None or True
        # Group items by conversation_id for emitter calls
        by_conv: dict[str, list[dict]] = {}
        for p in prepared:
            cid = p.get("conversation_id")
            if cid and p["type"] == "message":
                if cid not in by_conv:
                    by_conv[cid] = []
                by_conv[cid].append(p)

        for cid, conv_items in by_conv.items():
            # Sort items by valid_from to preserve turn order (mirror single path L2119-2126)
            conv_items.sort(key=lambda x: x.get("valid_from") or now)

            # Process each message in conversation
            for p in conv_items:
                user_id = p.get("user_id", "")
                try:
                    if INGEST_EVENT_ROWS:
                        await _maybe_emit_event_rows(
                            p["content"] or "", p["metadata"], cid, user_id, p["id"]
                        )
                except Exception as e:
                    logger.debug(f"event_extraction emit failed in bulk: {e}")

            # Window and gist emitters (run once per conversation group, not per message)
            user_id = conv_items[0].get("user_id", "") if conv_items else ""
            try:
                if INGEST_WINDOW_CHUNKS:
                    await _maybe_emit_window_chunk(cid, user_id)
            except Exception as e:
                logger.debug(f"window chunk emit failed in bulk: {e}")

            try:
                if INGEST_GIST_ROWS:
                    await _maybe_emit_gist_row(cid, user_id)
            except Exception as e:
                logger.debug(f"gist row emit failed in bulk: {e}")

    return [p["id"] for p in prepared]


def _queue_chroma(memory_id: str, operation: str) -> None:
    try:
        with _db() as db:
            db.execute("INSERT INTO chroma_sync_queue (memory_id, operation) VALUES (?,?)", (memory_id, operation))
    except Exception as e:
        logger.debug(f"ChromaDB queue insert failed: {e}")

async def _check_contradictions(
    item_id: str,
    content: str,
    title: str,
    vec: list[float],
    type_: str,
    agent_id: str,
    new_valid_from: str | None = None,
) -> tuple[list[str], list[tuple[str, float]]]:
    """
    Detects contradictions with existing memories of the same type.
    Returns (superseded_ids, related_candidates) where related_candidates
    are (id, score) pairs with cosine > 0.7 that are NOT contradictions.
    """
    superseded = []
    related = []
    try:
        with _db() as db:
            # Find top-5 similar memories of the same type
            where = "mi.is_deleted = 0 AND mi.type = ? AND mi.id != ?"
            params = [type_, item_id]
            if agent_id:
                where += " AND mi.agent_id = ?"
                params.append(agent_id)
            rows = db.execute(
                f"SELECT mi.id, mi.title, mi.content, me.embedding FROM memory_items mi "
                f"JOIN memory_embeddings me ON mi.id = me.memory_id WHERE {where} LIMIT 200",
                params
            ).fetchall()

        if not rows:
            return superseded, related

        embeddings = [_unpack(r["embedding"]) for r in rows]
        scores = _batch_cosine(vec, embeddings)

        for i, row in enumerate(rows):
            score = scores[i]
            if score > CONTRADICTION_THRESHOLD:
                # High similarity — check if it's a contradiction (same topic, different content).
                # Title-match gate is configurable via CONTRADICTION_TITLE_GATE env var:
                #   'strict' = legacy substring match required
                #   'loose'  = cosine + content-differs is enough (default since 2026-04-27)
                #   'off'    = bypass content check too (research mode)
                old_title = (row["title"] or "").strip().lower()
                new_title = (title or "").strip().lower()
                titles_match = old_title == new_title or (old_title and new_title and (
                    old_title in new_title or new_title in old_title
                ))
                content_differs = (row["content"] or "").strip() != (content or "").strip()

                if CONTRADICTION_TITLE_GATE == "strict":
                    fires = titles_match and content_differs
                elif CONTRADICTION_TITLE_GATE == "loose":
                    fires = content_differs
                else:  # 'off'
                    fires = True

                if fires:
                    # Contradiction detected — supersede old memory.
                    # Bi-temporal validity (Zep/Graphiti pattern, 2026-04-27):
                    # close the older memory's validity interval at new memory's
                    # valid_from. Falls back to now() when caller didn't supply
                    # a valid_from. Lets retrieval that filters by `as_of` see
                    # the older fact as still-valid before the supersession point.
                    _now_iso = datetime.now(timezone.utc).isoformat()
                    _close_at = new_valid_from or _now_iso
                    with _db() as db:
                        db.execute(
                            "UPDATE memory_items SET is_deleted = 1, "
                            "valid_to = COALESCE(valid_to, ?), updated_at = ? "
                            "WHERE id = ?",
                            (_close_at, _now_iso, row["id"]),
                        )
                        db.execute(
                            "INSERT INTO memory_relationships (id, from_id, to_id, relationship_type, created_at) VALUES (?,?,?,?,?)",
                            (str(uuid.uuid4()), item_id, row["id"], "supersedes", _now_iso)
                        )
                    _record_history(row["id"], "supersede", row["content"], item_id, "content")
                    superseded.append(row["id"])
                    logger.info(f"Memory {item_id} supersedes {row['id']} (contradiction detected, valid_to={_close_at})")
            elif score > 0.7:
                related.append((row["id"], score))
    except Exception as e:
        logger.debug(f"Contradiction check failed: {e}")
    return superseded, related


# ── Fact enrichment pipeline (Phase 4-5) ──────────────────────────────────────
async def _try_enrich_or_enqueue(memory_id: str, content: str, fact_enricher, db, variant: str | None = None, allowlist: set[str] | None = None) -> None:
    """Non-blocking: try enrichment under semaphore; on miss, enqueue.

    Variant-skip rule: if variant is not None and (allowlist is None or variant not in allowlist),
    return without doing anything.
    """
    if not ENABLE_FACT_ENRICHED or fact_enricher is None:
        return

    # Skip variant rows unless explicitly allowed
    if variant is not None and (allowlist is None or variant not in allowlist):
        return

    # Try non-blocking acquire with very short timeout
    try:
        async with asyncio.timeout(0.001):  # try-acquire only
            await _FACT_ENRICH_SEM.acquire()
    except (asyncio.TimeoutError, Exception):
        # Semaphore full or error — enqueue and return immediately
        _enqueue_fact_enrichment(memory_id, db)
        return

    # Acquired semaphore — spawn task and track it
    task = asyncio.create_task(_run_fact_enricher(memory_id, content, fact_enricher))
    _PENDING_FACT_TASKS.add(task)
    task.add_done_callback(lambda t: _PENDING_FACT_TASKS.discard(t))


def _enqueue_fact_enrichment(memory_id: str, db) -> None:
    """INSERT OR IGNORE into fact_enrichment_queue."""
    try:
        db.execute(
            "INSERT OR IGNORE INTO fact_enrichment_queue(memory_id) VALUES (?)",
            (memory_id,)
        )
    except Exception as e:
        logger.debug(f"Failed to enqueue fact enrichment for {memory_id}: {e}")


async def _run_fact_enricher(memory_id: str, content: str, fact_enricher) -> None:
    """Run the actual fact extractor with error handling and retries."""
    try:
        facts = await fact_enricher(content)
        if facts:
            await _write_fact_rows(memory_id, facts)
    except Exception as e:
        # Record error and bump attempts in queue
        try:
            with _db() as db:
                db.execute("""
                    INSERT OR REPLACE INTO fact_enrichment_queue(memory_id, attempts, last_error, last_attempt_at)
                    VALUES (?, COALESCE((SELECT attempts FROM fact_enrichment_queue WHERE memory_id=?),0)+1, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                """, (memory_id, memory_id, str(e)[:500]))
        except Exception as db_err:
            logger.debug(f"Failed to record enrichment error for {memory_id}: {db_err}")
    finally:
        _FACT_ENRICH_SEM.release()


async def _write_fact_rows(memory_id: str, facts: list[dict]) -> None:
    """Write one fact_enriched row per fact, with references edge and metadata."""
    for fact_dict in facts:
        fact_text = fact_dict.get("text", "").strip()
        if not fact_text:
            continue

        confidence = float(fact_dict.get("confidence", 0.5))
        fact_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        # Build metadata with source and confidence
        metadata = {
            "source_turn_id": memory_id,
            "confidence": confidence,
        }

        try:
            with _db() as db:
                # Insert the fact row
                db.execute(
                    "INSERT INTO memory_items (id, type, title, content, metadata_json, change_agent, source, origin_device, scope, created_at, content_hash) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        fact_id,
                        "fact_enriched",
                        fact_text[:100],  # Use fact text as title (truncated)
                        fact_text,
                        json.dumps(metadata),
                        "fact_enricher",
                        "fact_enricher",
                        ORIGIN_DEVICE,
                        "agent",
                        now,
                        hashlib.sha256(fact_text.encode("utf-8")).hexdigest(),
                    )
                )
                # Link via references edge: fact_id -> memory_id (from fact to source)
                db.execute(
                    "INSERT INTO memory_relationships (id, from_id, to_id, relationship_type, created_at) VALUES (?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        fact_id,
                        memory_id,
                        "references",
                        now,
                    )
                )
                _record_history(fact_id, "create", None, fact_text, "content", "fact_enricher", db=db)
        except Exception as e:
            logger.debug(f"Failed to write fact row for {memory_id}: {e}")


# ── Entity-relation graph pipeline (Phase 4-5) ───────────────────────────────

_TOKEN_PUNCT_RE = re.compile(r"[^\w\s]")

def _token_jaccard(a: str, b: str) -> float:
    """Token-set Jaccard similarity, lowercased, punctuation-stripped, whitespace-tokenized.

    Strips ASCII punctuation before tokenization so that "Alex Johnson," tokenizes
    the same way as "Alex Johnson" — important when entity strings come out of an
    SLM extractor that occasionally emits trailing commas/periods.
    """
    ta = {t for t in _TOKEN_PUNCT_RE.sub(" ", a.lower()).split() if t}
    tb = {t for t in _TOKEN_PUNCT_RE.sub(" ", b.lower()).split() if t}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _resolve_entity(canonical_name: str, entity_type: str, db) -> str | None:
    """3-tier resolution (sync, tiers 1+2 only). Returns existing entity_id if matched, else None.

    Tier 1: exact (canonical_name, entity_type) match.
    Tier 2: fuzzy token-Jaccard >= ENTITY_RESOLVE_FUZZY_MIN within same entity_type.
    Tier 3 (embedding cosine) is handled by the async variant _resolve_entity_async.
    """
    # Tier 1: exact match
    row = db.execute(
        "SELECT id FROM entities WHERE canonical_name = ? AND entity_type = ? LIMIT 1",
        (canonical_name, entity_type),
    ).fetchone()
    if row:
        return row["id"]

    # Tier 2: fuzzy token-Jaccard within same entity_type
    candidates = db.execute(
        "SELECT id, canonical_name FROM entities WHERE entity_type = ?",
        (entity_type,),
    ).fetchall()
    best_score, best_id = 0.0, None
    for c in candidates:
        s = _token_jaccard(canonical_name, c["canonical_name"])
        if s > best_score:
            best_score, best_id = s, c["id"]
    if best_score >= ENTITY_RESOLVE_FUZZY_MIN and best_id is not None:
        return best_id

    return None  # Tiers 1+2 only in sync path


async def _resolve_entity_async(canonical_name: str, entity_type: str, db) -> str | None:
    """Full 3-tier resolution including embedding cosine. Use from async context."""
    sync_id = _resolve_entity(canonical_name, entity_type, db)
    if sync_id is not None:
        return sync_id

    # Tier 3: embedding cosine within same entity_type.
    # TODO: pre-embed and cache canonical_name embeddings to avoid per-candidate
    #       embed calls when the entity table is large.
    # Cap candidates to 100 most-recently created to bound embed cost.
    candidates = db.execute(
        "SELECT id, canonical_name FROM entities WHERE entity_type = ? ORDER BY created_at DESC LIMIT 100",
        (entity_type,),
    ).fetchall()
    if not candidates:
        return None

    qvec, _ = await _embed(canonical_name)
    if qvec is None:
        return None

    best_score, best_id = 0.0, None
    for c in candidates:
        cvec, _ = await _embed(c["canonical_name"])
        if cvec is None:
            continue
        s = _cosine(qvec, cvec)
        if s > best_score:
            best_score, best_id = s, c["id"]

    if best_score >= ENTITY_RESOLVE_COSINE_MIN and best_id is not None:
        return best_id
    return None


def _create_entity(canonical_name: str, entity_type: str, attributes: dict, db) -> str:
    """INSERT new row into entities; return new uuid id."""
    entity_id = str(uuid.uuid4())
    attrs_json = json.dumps(attributes or {})
    content_hash = hashlib.sha256(
        f"{canonical_name}|{entity_type}|{attrs_json}".encode("utf-8")
    ).hexdigest()
    db.execute(
        "INSERT INTO entities (id, canonical_name, entity_type, attributes_json, content_hash) "
        "VALUES (?, ?, ?, ?, ?)",
        (entity_id, canonical_name, entity_type, attrs_json, content_hash),
    )
    return entity_id


def _link_memory_to_entity(
    memory_id: str,
    entity_id: str,
    mention_text: str,
    mention_offset: int,
    confidence: float,
    db,
) -> None:
    """INSERT OR IGNORE into memory_item_entities."""
    db.execute(
        "INSERT OR IGNORE INTO memory_item_entities "
        "(memory_id, entity_id, mention_text, mention_offset, confidence) "
        "VALUES (?, ?, ?, ?, ?)",
        (memory_id, entity_id, mention_text, mention_offset, confidence),
    )


def _link_entity_relationship(
    from_entity_id: str,
    to_entity_id: str,
    predicate: str,
    confidence: float,
    source_memory_id: str,
    db,
) -> None:
    """INSERT into entity_relationships. Raises ValueError for unknown predicates."""
    if predicate not in VALID_ENTITY_PREDICATES:
        raise ValueError(
            f"Invalid predicate '{predicate}'. "
            f"Valid predicates: {', '.join(sorted(VALID_ENTITY_PREDICATES))}"
        )
    db.execute(
        "INSERT INTO entity_relationships "
        "(from_entity, to_entity, predicate, confidence, source_memory_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (from_entity_id, to_entity_id, predicate, confidence, source_memory_id),
    )


def _enqueue_entity_extraction(memory_id: str, db) -> None:
    """INSERT OR IGNORE into entity_extraction_queue."""
    try:
        db.execute(
            "INSERT OR IGNORE INTO entity_extraction_queue(memory_id) VALUES (?)",
            (memory_id,),
        )
    except Exception as e:
        logger.debug(f"Failed to enqueue entity extraction for {memory_id}: {e}")


async def _run_entity_extractor(
    memory_id: str,
    content: str,
    entity_extractor,
    *,
    valid_types: frozenset | None = None,       # None = use VALID_ENTITY_TYPES
    valid_predicates: frozenset | None = None,  # None = use VALID_ENTITY_PREDICATES
) -> None:
    """Background task. Calls extractor, parses result, resolves+writes entities and
    relationships. Releases _ENTITY_EXTRACT_SEM in finally block.

    Reliability hardening (Phase E1):
    - Vocabulary validation is centralized here against active_types/active_predicates.
      Callers may pass override frozensets; None means use module-level constants.
    - Bitemporal valid_from is inherited from the source memory (not extraction-time now()).
    - entity_relationships idempotency: DELETE old rows with the same
      (from_entity, to_entity, predicate, source_memory_id) before re-inserting, so
      re-extraction of a memory doesn't create duplicate relationship rows.
      We use delete-then-insert rather than a content-hash UNIQUE index so the schema
      stays unchanged (migration 024 is already applied).
    - Failure handling: on any exception, increment attempts in the queue. Items with
      attempts >= ENTITY_EXTRACT_MAX_ATTEMPTS are excluded from the eligible set by
      _select_pending_entity_extraction (poisoned-item guard).
    """
    # Resolve active vocabularies — callers may pass custom lists for bench/experiments.
    active_types: frozenset = valid_types if valid_types is not None else VALID_ENTITY_TYPES
    active_predicates: frozenset = valid_predicates if valid_predicates is not None else VALID_ENTITY_PREDICATES

    try:
        result = await entity_extractor(content)
        entities_raw = result.get("entities", []) if isinstance(result, dict) else []
        relationships_raw = result.get("relationships", []) if isinstance(result, dict) else []

        # Inherit valid_from from the source memory so bitemporal validity is correct.
        # e.g. an entity extracted from a 2024 memory should have valid_from='2024-...',
        # not the extraction-time timestamp.
        with _db() as db:
            src_row = db.execute(
                "SELECT valid_from FROM memory_items WHERE id = ? LIMIT 1",
                (memory_id,),
            ).fetchone()
        source_valid_from: str | None = src_row["valid_from"] if src_row else None

        # Resolve/create entities and record IDs by canonical_name for relationship linking.
        canonical_to_id: dict[str, str] = {}
        with _db() as db:
            for ent in entities_raw:
                cname = (ent.get("canonical_name") or "").strip()
                etype = (ent.get("entity_type") or "").strip()
                # Centralized vocabulary validation — reject unknown entity types.
                if not cname or etype not in active_types:
                    if not cname or etype:
                        logger.debug(
                            f"Entity extractor: rejected entity_type='{etype}' "
                            f"(not in active vocabulary) for memory {memory_id}"
                        )
                    continue
                entity_id = await _resolve_entity_async(cname, etype, db)
                if entity_id is None:
                    try:
                        entity_id = _create_entity(cname, etype, {}, db)
                        # Set valid_from on the newly created entity to inherit from source.
                        if source_valid_from:
                            db.execute(
                                "UPDATE entities SET valid_from = ? WHERE id = ? AND valid_from IS NULL",
                                (source_valid_from, entity_id),
                            )
                    except Exception as e:
                        logger.debug(f"Entity create failed for '{cname}': {e}")
                        continue
                canonical_to_id[cname] = entity_id
                mention_text = ent.get("mention_text") or cname
                confidence = float(ent.get("confidence", 0.85))
                try:
                    # _link_memory_to_entity uses INSERT OR IGNORE — idempotent.
                    _link_memory_to_entity(memory_id, entity_id, mention_text, 0, confidence, db)
                except Exception as e:
                    logger.debug(f"Entity link failed for {memory_id}->{entity_id}: {e}")

            # Write relationships — both ends must have been resolved above.
            for rel in relationships_raw:
                from_cname = (rel.get("from_entity") or "").strip()
                to_cname = (rel.get("to_entity") or "").strip()
                predicate = (rel.get("predicate") or "").strip()
                confidence = float(rel.get("confidence", 0.85))
                from_id = canonical_to_id.get(from_cname)
                to_id = canonical_to_id.get(to_cname)
                # Centralized vocabulary validation — reject unknown predicates.
                if not from_id or not to_id:
                    continue
                if predicate not in active_predicates:
                    logger.debug(
                        f"Entity extractor: rejected predicate='{predicate}' "
                        f"(not in active vocabulary) for memory {memory_id}"
                    )
                    continue
                try:
                    # Idempotency for entity_relationships: entity_relationships.id is
                    # AUTOINCREMENT so INSERT OR IGNORE would silently skip on PK conflict
                    # (there is none — autoincrement always inserts a new row). Instead we
                    # use delete-then-insert to ensure re-extraction of the same memory
                    # doesn't accumulate duplicate relationship rows.
                    db.execute(
                        "DELETE FROM entity_relationships "
                        "WHERE from_entity = ? AND to_entity = ? AND predicate = ? "
                        "AND source_memory_id = ?",
                        (from_id, to_id, predicate, memory_id),
                    )
                    rel_valid_from = rel.get("valid_from") or source_valid_from
                    db.execute(
                        "INSERT INTO entity_relationships "
                        "(from_entity, to_entity, predicate, confidence, source_memory_id, valid_from) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (from_id, to_id, predicate, confidence, memory_id, rel_valid_from),
                    )
                except Exception as e:
                    logger.debug(f"Relationship link error for {from_cname}->{to_cname} ({predicate}): {e}")

        # On success, remove any queue entry so the item isn't re-processed.
        try:
            with _db() as db:
                db.execute(
                    "DELETE FROM entity_extraction_queue WHERE memory_id = ?",
                    (memory_id,),
                )
        except Exception as db_err:
            logger.debug(f"Failed to remove queue entry for {memory_id} after success: {db_err}")

    except Exception as e:
        # Record error and bump attempts in queue so the item is retried on next pass.
        # Items with attempts >= ENTITY_EXTRACT_MAX_ATTEMPTS are excluded from the
        # eligible set by _select_pending_entity_extraction (poisoned-item guard).
        try:
            with _db() as db:
                db.execute(
                    """
                    INSERT OR REPLACE INTO entity_extraction_queue
                        (memory_id, attempts, last_error, last_attempt_at)
                    VALUES (
                        ?,
                        COALESCE((SELECT attempts FROM entity_extraction_queue WHERE memory_id=?), 0) + 1,
                        ?,
                        strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                    )
                    """,
                    (memory_id, memory_id, str(e)[:500]),
                )
        except Exception as db_err:
            logger.debug(f"Failed to record entity extraction error for {memory_id}: {db_err}")
    finally:
        _ENTITY_EXTRACT_SEM.release()


async def _try_extract_or_enqueue(
    memory_id: str,
    content: str,
    entity_extractor,
    db,
    variant: str | None = None,
    allowlist: set[str] | None = None,
    *,
    valid_types: frozenset | None = None,       # None = use VALID_ENTITY_TYPES
    valid_predicates: frozenset | None = None,  # None = use VALID_ENTITY_PREDICATES
) -> None:
    """Non-blocking: try entity extraction under semaphore; on miss, enqueue.

    Read ENABLE_ENTITY_GRAPH at call time so tests can monkeypatch.
    Variant-skip rule: if variant is not None and (allowlist is None or variant not in
    allowlist), return without doing anything — mirrors fact_enricher pattern.

    valid_types / valid_predicates are forwarded to _run_entity_extractor unchanged.
    None means use the module-level VALID_ENTITY_TYPES / VALID_ENTITY_PREDICATES constants.
    Bench harnesses and production callers may pass custom frozensets; default keeps
    existing behavior.
    """
    if not _enable_entity_graph_gate():
        return
    if entity_extractor is None:
        return

    # Skip variant rows unless explicitly allowed
    if variant is not None and (allowlist is None or variant not in allowlist):
        return

    # Try non-blocking acquire with very short timeout
    try:
        async with asyncio.timeout(0.001):
            await _ENTITY_EXTRACT_SEM.acquire()
    except (asyncio.TimeoutError, Exception):
        # Semaphore full or error — enqueue and return immediately
        _enqueue_entity_extraction(memory_id, db)
        return

    # Acquired semaphore — spawn task and track it
    task = asyncio.create_task(
        _run_entity_extractor(
            memory_id, content, entity_extractor,
            valid_types=valid_types,
            valid_predicates=valid_predicates,
        )
    )
    _PENDING_ENTITY_TASKS.add(task)
    task.add_done_callback(lambda t: _PENDING_ENTITY_TASKS.discard(t))


async def _query_chroma(
    query_vec: list[float],
    k: int = 5,
    scope_filter: dict | None = None,
) -> list[dict]:
    """Queries the remote ChromaDB instance for federated results.

    Args:
        query_vec: Embedding vector for the query.
        k: Maximum number of results to return.
        scope_filter: Optional dict of {field: value} pairs to filter results
            by metadata (e.g. {'user_id': ..., 'scope': ..., 'agent_id': ...}).
            Empty/None values are skipped. Translated to ChromaDB v2 where syntax.
    """
    if not CHROMA_BASE_URL or not CHROMA_BASE_URL.startswith("http"):
        return []
    try:
        client = _get_embed_client()
        # 1. Resolve collection ID
        resp = await client.get(f"{CHROMA_BASE_URL}{CHROMA_V2_PREFIX}/{CHROMA_COLLECTION}", timeout=CHROMA_CONNECT_T)
        resp.raise_for_status()
        col_data = resp.json()
        col_id = col_data.get("id")
        if not col_id:
            logger.warning("ChromaDB collection response missing 'id' field")
            return []

        # 2. Build query payload
        col_path = f"{CHROMA_BASE_URL}{CHROMA_V2_PREFIX}/{col_id}"
        payload: dict = {
            "query_embeddings": [query_vec],
            "n_results": k,
            "include": ["documents", "metadatas", "distances"],
        }

        # Translate scope_filter to ChromaDB v2 where-clause syntax
        source_tag = "federated_chroma_unscoped"
        if scope_filter:
            where_clauses = []
            for field, value in scope_filter.items():
                if value:  # skip empty strings / None
                    where_clauses.append({field: {"$eq": value}})
            if where_clauses:
                payload["where"] = (
                    where_clauses[0]
                    if len(where_clauses) == 1
                    else {"$and": where_clauses}
                )
                source_tag = "federated_chroma_scoped"

        # 3. Perform query
        query_resp = await client.post(f"{col_path}/query", json=payload, timeout=CHROMA_READ_T)
        query_resp.raise_for_status()

        data = query_resp.json()
        results = []
        if data["ids"] and data["ids"][0]:
            for i in range(len(data["ids"][0])):
                # Chroma distance is often squared L2, but we'll treat it as a score component
                score = 1.0 - (data["distances"][0][i] / 2.0) if data["distances"] else 0.5
                results.append({
                    "id": data["ids"][0][i],
                    "content": data["documents"][0][i],
                    "title": data["metadatas"][0][i].get("title", ""),
                    "type": data["metadatas"][0][i].get("type", "federated"),
                    "score": score,
                    "_explanation": {"source": source_tag},
                })
        return results
    except Exception as e:
        logger.debug(f"ChromaDB federated query failed: {e}")
        return []

def _apply_recency_bonus(scored, recency_bias, explain=False):
    """Add a rank-based recency bonus to each (score, item) pair.

    Items are ranked lexicographically by `valid_from` (ISO-8601 sorts
    correctly as strings). The oldest dated item receives bonus 0, the
    newest receives `recency_bias`, with linear interpolation between.
    Items with empty `valid_from` receive bonus 0. If fewer than two dated
    items exist, the input is returned unchanged.

    Used to break ties in favor of supersession evidence for "what is my
    current X" queries without parsing timestamps.
    """
    if not scored or recency_bias <= 0:
        return scored
    with_vf = [(i, (it.get("valid_from") or "")) for i, (_, it) in enumerate(scored)]
    dated = [(i, v) for i, v in with_vf if v]
    if len(dated) < 2:
        return scored
    dated.sort(key=lambda x: x[1])
    n = len(dated) - 1
    rank_of = {idx: rank for rank, (idx, _) in enumerate(dated)}
    rescored = []
    for i, (s, it) in enumerate(scored):
        bonus = recency_bias * (rank_of[i] / n) if i in rank_of else 0.0
        if explain and "_explanation" in it:
            it["_explanation"]["recency_bonus"] = bonus
        rescored.append((s + bonus, it))
    return rescored


def _trim_by_elbow(ranked: list[tuple[float, dict]], sensitivity: float = 1.5) -> list[tuple[float, dict]]:
    """Trims results where the score drop-off is significantly higher than average."""
    if len(ranked) < 3:
        return ranked

    # Calculate score differences between consecutive results
    diffs = [ranked[i][0] - ranked[i+1][0] for i in range(len(ranked) - 1)]
    avg_diff = sum(diffs) / len(diffs)

    # Find the first 'elbow' where the drop is significantly larger than the average
    for i, d in enumerate(diffs):
        if d > avg_diff * sensitivity:
            # We found an elbow, trim here
            return ranked[:i+1]

    return ranked


def _apply_temporal_boost(scored, query, explain=False):
    """Detects dates in query and boosts items with matching or nearby valid_from dates."""
    # 1. Extract potential dates from query (YYYY-MM-DD)
    date_patterns = [
        r"\b(\d{4})-(\d{2})-(\d{2})\b",
        r"\b(\d+)\s+(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{4})\b",
    ]
    query_dates = []
    for pattern in date_patterns:
        for match in re.finditer(pattern, query.lower()):
            try:
                if "-" in match.group(0):
                    query_dates.append(datetime.fromisoformat(match.group(0)).date())
                else:
                    d, m, y = match.groups()
                    months = ["january", "february", "march", "april", "may", "june",
                              "july", "august", "september", "october", "november", "december"]
                    query_dates.append(date(int(y), months.index(m) + 1, int(d)))
            except Exception:
                continue

    if not query_dates:
        return scored

    rescored = []
    for s, it in scored:
        boost = 0.0
        vf_str = it.get("valid_from", "")
        if vf_str:
            try:
                vf_date = datetime.fromisoformat(vf_str.split("T")[0]).date()
                for qd in query_dates:
                    diff = abs((vf_date - qd).days)
                    if diff == 0: boost = max(boost, 0.25)
                    elif diff <= 2: boost = max(boost, 0.15)
                    elif diff <= 7: boost = max(boost, 0.05)
            except Exception:
                pass

        if explain and boost > 0:
            if "_explanation" not in it: it["_explanation"] = {}
            it["_explanation"]["temporal_boost"] = boost
        rescored.append((s + boost, it))
    return rescored


async def memory_search_scored_impl(
    query,
    k=8,
    type_filter="",
    agent_filter="",
    search_mode="hybrid",
    user_id="",
    scope="",
    as_of="",
    conversation_id="",
    explain=False,
    extra_columns=None,
    recency_bias=0.0,
    vector_weight=0.7,
    adaptive_k=False,
    elbow_sensitivity=1.5,
    adaptive_k_min=0,
    adaptive_k_max=0,
    smart_time_boost=0.0,
    smart_neighbor_sessions=0,
    variant="",
    intent_hint="",
    vector_kind_strategy="default",
    _depth=0,
):
    """Hybrid FTS5+vector+MMR search returning a list of (score, item_dict).

    Structured sibling of `memory_search_impl`. Used by benchmarks and other
    callers that need raw result rows (with metadata_json, conversation_id,
    valid_from, etc.) rather than the formatted text output.

    `extra_columns` is an optional list of extra `mi.<column>` names to include
    in each item dict (e.g. ["metadata_json", "conversation_id", "valid_from",
    "valid_to", "user_id"]). Federated Chroma fallback results will NOT have
    these extra fields.

    `intent_hint` is consumed only when M3_INTENT_ROUTING is on (or the
    narrower M3_QUERY_TYPE_ROUTING handles the weight shift). Supported
    values — "user-fact", "temporal-reasoning", "multi-session", "general"
    — match the labels emitted by bin/slm_intent.classify_intent(). Off by
    default; callers can pass the hint without enabling the gate and it'll
    be silently ignored, which is what makes this safe to thread through
    existing call sites.

    `vector_kind_strategy` picks which rows from memory_embeddings to score
    against when v022 dual-embedding is in play:
      - "default" (back-compat): only vector_kind='default' rows.
      - "max": score against every vector_kind; dedupe by memory_id keeping
        the highest vector similarity. Used with dual_embed ingests where
        both a raw ('default') and SLM-enriched ('enriched') vector exist
        per turn, so a turn wins its bucket on whichever representation
        the query favors.
    """
    try:
        k = int(k)
    except (TypeError, ValueError):
        k = 8
    _track_cost("search_calls")
    if _depth > 1:
        return []

    vector_weight = _maybe_route_query(query, vector_weight, intent_hint=intent_hint)

    q_vec, _ = await _embed(query)
    if not q_vec:
        return []

    extra_columns = list(extra_columns or [])
    _BASE_COLS = ["id", "content", "title", "type", "importance"]
    _allowed_extra = {
        "metadata_json", "conversation_id", "valid_from", "valid_to",
        "user_id", "scope", "agent_id", "created_at", "source",
    }
    if recency_bias and "valid_from" not in extra_columns:
        extra_columns = extra_columns + ["valid_from"]
    safe_extra = [c for c in extra_columns if c in _allowed_extra and c not in _BASE_COLS]
    extra_sql = (", " + ", ".join(f"mi.{c}" for c in safe_extra)) if safe_extra else ""

    where_clauses = ["mi.is_deleted = 0"]
    params = []

    if type_filter:
        is_exact = (type_filter.startswith('"') and type_filter.endswith('"')) or (type_filter.startswith("'") and type_filter.endswith("'"))
        actual_type = type_filter[1:-1] if is_exact else type_filter
        if is_exact:
            where_clauses.append("mi.type = ?")
        else:
            where_clauses.append("mi.type LIKE ?")
        params.append(actual_type)

    if agent_filter:
        is_exact = (agent_filter.startswith('"') and agent_filter.endswith('"')) or (agent_filter.startswith("'") and agent_filter.endswith("'"))
        actual_agent = agent_filter[1:-1] if is_exact else agent_filter
        if is_exact:
            where_clauses.append("mi.agent_id = ?")
        else:
            where_clauses.append("LOWER(mi.agent_id) = LOWER(?)")
        params.append(actual_agent)

    if user_id:
        where_clauses.append("mi.user_id = ?")
        params.append(user_id)
    if scope:
        where_clauses.append("mi.scope = ?")
        params.append(scope)
    if conversation_id:
        where_clauses.append("mi.conversation_id = ?")
        params.append(conversation_id)
    if variant:
        # Accept "<name>" for exact-variant, "" for unfiltered (default),
        # or the sentinel "__none__" to filter for untagged legacy rows.
        if variant == "__none__":
            where_clauses.append("mi.variant IS NULL")
        else:
            where_clauses.append("mi.variant = ?")
            params.append(variant)

    if as_of:
        # Open-ended validity is represented as NULL by new writes; legacy
        # rows may still carry "". Match both so a future write-path change
        # to use NULL exclusively doesn't break historical data.
        where_clauses.append("(mi.valid_from IS NULL OR mi.valid_from = '' OR mi.valid_from <= ?)")
        where_clauses.append("(mi.valid_to   IS NULL OR mi.valid_to   = '' OR mi.valid_to   > ?)")
        params.extend([as_of, as_of])

    # v022: filter the embeddings join by vector_kind unless caller opted
    # into cross-kind fusion. Legacy rows (pre-v022 / single-embed ingests)
    # carry vector_kind='default' via the migration's NOT NULL DEFAULT, so
    # "default" strategy is a strict superset of pre-v022 behavior.
    if vector_kind_strategy == "default":
        where_clauses.append("me.vector_kind = 'default'")
    elif vector_kind_strategy != "max":
        raise ValueError(
            f"vector_kind_strategy must be 'default' or 'max', got {vector_kind_strategy!r}"
        )

    where_sql = " AND ".join(where_clauses)

    def _recurse_semantic():
        return memory_search_scored_impl(
            query, k=k, type_filter=type_filter, agent_filter=agent_filter,
            search_mode="semantic", user_id=user_id, scope=scope, as_of=as_of,
            conversation_id=conversation_id, explain=explain,
            extra_columns=extra_columns, recency_bias=recency_bias,
            vector_weight=vector_weight, adaptive_k=adaptive_k,
            smart_time_boost=smart_time_boost,
            smart_neighbor_sessions=smart_neighbor_sessions,
            variant=variant,
            intent_hint=intent_hint,
            vector_kind_strategy=vector_kind_strategy,
            _depth=_depth + 1,
        )

    # When strategy="max" the memory_embeddings join returns one row per
    # (memory_id, vector_kind) pair, so a straight LIMIT 1000 would see
    # each item N times (N = distinct kinds stored) and the effective
    # unique-item pool would shrink to 1000/N. Double the SQL-level cap
    # for max-kind so the unique pool stays near 1000. Strategy="default"
    # pins to a single kind, so the base cap already counts unique items.
    sql_row_limit = 2000 if vector_kind_strategy == "max" else 1000

    try:
        with _db() as db:
            if search_mode == "semantic":
                sql = f"""
                    SELECT mi.id, mi.content, mi.title, mi.type, mi.importance, me.embedding, 0.0 as bm25_score{extra_sql}
                    FROM memory_items mi
                    JOIN memory_embeddings me ON mi.id = me.memory_id
                    WHERE {where_sql}
                    LIMIT {sql_row_limit}
                """
                rows = db.execute(sql, params).fetchall()
            else:
                sql = f"""
                    SELECT mi.id, mi.content, mi.title, mi.type, mi.importance, me.embedding,
                           bm25(memory_items_fts) as bm25_score{extra_sql}
                    FROM memory_items mi
                    JOIN memory_embeddings me ON mi.id = me.memory_id
                    JOIN memory_items_fts fts ON mi.rowid = fts.rowid
                    WHERE {where_sql} AND memory_items_fts MATCH ?
                    ORDER BY bm25_score ASC
                    LIMIT {sql_row_limit}
                """
                is_exact_query = (query.startswith('"') and query.endswith('"')) or (query.startswith("'") and query.endswith("'"))
                clean_query = query[1:-1] if is_exact_query else query
                if is_exact_query:
                    fts_query = f'"{clean_query}"'
                else:
                    clean_query = _sanitize_fts(clean_query)
                    if not clean_query:
                        return await _recurse_semantic()
                    fts_query = f"{clean_query}*" if " " not in clean_query and clean_query.isalnum() else clean_query

                rows = db.execute(sql, (*params, fts_query)).fetchall()
                if not rows:
                    return await _recurse_semantic()
    except sqlite3.OperationalError:
        return await _recurse_semantic()

    scored = []
    # Under max-kind, trim AFTER dedup so SEARCH_ROW_CAP counts unique items,
    # not kind-duplicated rows. Under default (pins to one kind) the dupes
    # don't exist, so the cap already counts unique items and we trim up-front
    # to avoid an unnecessary cosine batch.
    if vector_kind_strategy != "max" and len(rows) > SEARCH_ROW_CAP:
        rows = rows[:SEARCH_ROW_CAP]
    page_matrix = [_unpack(r["embedding"]) for r in rows]
    page_scores = _batch_cosine(q_vec, page_matrix)

    # Max-kind fusion: when the SQL let through multiple vector_kind rows
    # per memory_id, keep the row with the highest vector similarity so
    # each item scores exactly once downstream. The FTS bm25 value is the
    # same across a memory_id's rows (bm25 is per-item), so dropping the
    # losing vector only discards vector-similarity information.
    if vector_kind_strategy == "max" and rows:
        best: dict[str, int] = {}
        for i, row in enumerate(rows):
            mid = row["id"]
            if mid not in best or page_scores[i] > page_scores[best[mid]]:
                best[mid] = i
        keep_idx = sorted(best.values())
        rows = [rows[i] for i in keep_idx]
        page_scores = [page_scores[i] for i in keep_idx]
        # Now trim to the cap — count unique items, not kind-duplicated rows.
        if len(rows) > SEARCH_ROW_CAP:
            rows = rows[:SEARCH_ROW_CAP]
            page_scores = page_scores[:SEARCH_ROW_CAP]

    for i, row in enumerate(rows):
        item = dict(row)
        del item["embedding"]
        vector_score = page_scores[i]
        bm25_norm = (1.0 / (1.0 + abs(row["bm25_score"])))
        final_score = vector_score * vector_weight + bm25_norm * (1.0 - vector_weight)

        # Short-turn length penalty: stubs like "ok cool" rank identically to
        # substantive content under FTS+vector alone. Scale by length up to the
        # threshold, full-weight beyond.
        content_len = len(row["content"] or "")
        length_penalty = 1.0
        if content_len < SHORT_TURN_THRESHOLD:
            length_penalty = max(0.3, content_len / SHORT_TURN_THRESHOLD)
        final_score *= length_penalty

        # Title-match boost: titles that echo query tokens are high-signal.
        title_overlap = _query_title_overlap(query, row["title"] or "")
        title_boost = TITLE_MATCH_BOOST * title_overlap
        final_score += title_boost

        # Importance blend: caller-supplied importance nudges ranking.
        importance_boost = IMPORTANCE_WEIGHT * (float(row["importance"] or 0.0))
        final_score += importance_boost

        # Role-biased boost (Piece 2 of intent routing). When the caller
        # signals "user-fact" intent, bump user-authored turns so the
        # user's original statement outranks the assistant's echo. Needs
        # metadata_json available — `extra_columns` must include it or
        # the boost silently no-ops.
        role_boost = 0.0
        if INTENT_ROUTING and intent_hint == "user-fact":
            try:
                meta_raw = row["metadata_json"] if "metadata_json" in row.keys() else None
                if meta_raw:
                    meta = json.loads(meta_raw)
                    if isinstance(meta, dict) and meta.get("role") == "user":
                        role_boost = INTENT_USER_FACT_BOOST
                        final_score += role_boost
            except (json.JSONDecodeError, TypeError, IndexError, KeyError):
                pass

        if explain:
            item["_explanation"] = {
                "vector": vector_score,
                "bm25": bm25_norm,
                "importance": row["importance"],
                "raw_hybrid": vector_score * vector_weight + bm25_norm * (1.0 - vector_weight),
                "length_penalty": length_penalty,
                "title_overlap": title_overlap,
                "title_boost": title_boost,
                "importance_boost": importance_boost,
                "vector_weight": vector_weight,
                "intent_hint": intent_hint,
                "role_boost": role_boost,
            }
        scored.append((final_score, item))

    # Apply temporal boost if dates detected in query
    if scored:
        scored = _apply_temporal_boost(scored, query, explain=explain)

    if recency_bias > 0 and scored:
        scored = _apply_recency_bonus(scored, recency_bias, explain=explain)

    # Predecessor pull (Piece 3 of intent routing). For user-fact intent
    # the top-ranked turn is often the assistant echo at index N+1; fetch
    # turn N from the same conversation so the user's original statement
    # enters the candidate set. Only runs at _depth==0 to avoid unbounded
    # recursion, and is capped to the current top 10 hits so the extra
    # DB work stays bounded.
    if INTENT_ROUTING and intent_hint == "user-fact" and _depth == 0 and scored:
        _pull_predecessor_turns(scored)

    _MMR_LAMBDA = 0.7
    pre_ranked_all = sorted(scored, key=lambda x: x[0], reverse=True)

    # Adaptive K: Trim by elbow if requested
    if adaptive_k:
        pre_ranked_all = _trim_by_elbow(pre_ranked_all, sensitivity=elbow_sensitivity)
        if adaptive_k_min and len(pre_ranked_all) < adaptive_k_min:
            # Floor: undo the trim when it leaves fewer than the requested minimum.
            pre_ranked_all = sorted(scored, key=lambda x: x[0], reverse=True)[:adaptive_k_min]
        if adaptive_k_max and len(pre_ranked_all) > adaptive_k_max:
            pre_ranked_all = pre_ranked_all[:adaptive_k_max]
        if len(pre_ranked_all) < k:
            k = len(pre_ranked_all)

    seen_content: set[str] = set()
    pre_ranked: list = []
    for entry in pre_ranked_all:
        c = (entry[1].get("content") or "").strip()
        if c and c in seen_content:
            continue
        if c:
            seen_content.add(c)
        pre_ranked.append(entry)
        if len(pre_ranked) >= k * 3:
            break
    if len(pre_ranked) > k and len(page_matrix) > 0:
        _emb_lookup = {rows[i]["id"]: page_matrix[i] for i in range(len(rows))}
        selected = [pre_ranked[0]]
        candidates = list(pre_ranked[1:])
        while candidates and len(selected) < k:
            best_idx, best_mmr = 0, -float('inf')
            for ci, (c_score, c_item) in enumerate(candidates):
                c_vec = _emb_lookup.get(c_item["id"])
                if c_vec is None:
                    best_idx = ci
                    break
                similarities = [_batch_cosine(c_vec, [_emb_lookup[s[1]["id"]]])[0]
                                for s in selected if s[1]["id"] in _emb_lookup]
                max_sim = max(similarities, default=0.0)
                mmr = _MMR_LAMBDA * c_score - (1 - _MMR_LAMBDA) * max_sim
                if mmr > best_mmr:
                    best_mmr = mmr
                    best_idx = ci
                if explain:
                    if "_explanation" not in c_item:
                        c_item["_explanation"] = {}
                    c_item["_explanation"]["max_sim_to_selected"] = max_sim
                    c_item["_explanation"]["mmr_penalty"] = (1 - _MMR_LAMBDA) * max_sim
            selected.append(candidates.pop(best_idx))
        ranked = selected
    else:
        ranked = pre_ranked

    # Hard skip: conversation_id is a strict scope boundary we never cross-peer;
    # type_filter should stay local to avoid type pollution from remote stores.
    _skip_federated_hard = bool(conversation_id or type_filter)

    # Soft condition: fire federation when local results are weak (too few or low confidence).
    local_top_score = ranked[0][0] if ranked else 0.0
    _local_weak = (
        len(ranked) < 3
        or local_top_score < FEDERATION_LOW_SCORE_THRESHOLD
    )

    if _local_weak and not _skip_federated_hard:
        fed_results = await _query_chroma(
            q_vec, k=3,
            scope_filter={"user_id": user_id, "scope": scope, "agent_id": agent_filter},
        )
        for fr in fed_results:
            if not any(r[1]["id"] == fr["id"] for r in ranked):
                if not explain:
                    # Still tag so audit tooling can identify federation hits
                    fr.setdefault("_explanation", {"source": fr.get("_explanation", {}).get("source", "federated_chroma_scoped")})
                ranked.append((fr["score"], fr))

    if ranked:
        ids = [item[1]["id"] for item in ranked if "bm25_score" in item[1]]
        if ids:
            try:
                with _db() as db:
                    placeholders = ",".join(["?"] * len(ids))
                    db.execute(
                        f"UPDATE memory_items SET last_accessed_at = ?, access_count = access_count + 1 WHERE id IN ({placeholders})",
                        (datetime.now(timezone.utc).isoformat(), *ids),
                    )
            except Exception as e:
                logger.debug(f"Search result timestamp update failed: {e}")

    # Time-aware boost + neighbor-session expansion. Both are off unless the
    # caller opts in with smart_time_boost > 0 or smart_neighbor_sessions > 0.
    # Caller must include "metadata_json" in extra_columns so referenced_dates
    # / session_index metadata is available on rows.
    if ranked and (smart_time_boost > 0.0 or smart_neighbor_sessions > 0):
        from temporal_utils import extract_referenced_dates, has_temporal_cues

        def _meta_for(item: dict) -> dict:
            m = item.get("metadata")
            if isinstance(m, dict):
                return m
            raw = item.get("metadata_json") or "{}"
            try:
                m = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except (json.JSONDecodeError, TypeError):
                m = {}
            item["metadata"] = m
            return m

        query_dates = extract_referenced_dates(query) if smart_time_boost > 0.0 else []
        query_has_temporal = has_temporal_cues(query)

        if smart_time_boost > 0.0 and query_dates:
            query_dt_set: list[datetime] = []
            for ds in query_dates:
                try:
                    query_dt_set.append(datetime.strptime(ds, "%Y-%m-%d").replace(tzinfo=timezone.utc))
                except ValueError:
                    pass
            if query_dt_set:
                boosted: list[tuple[float, dict]] = []
                for score, item in ranked:
                    new_score = score
                    vf = item.get("valid_from") or ""
                    if vf:
                        try:
                            h_dt = datetime.fromisoformat(vf)
                            for qdt in query_dt_set:
                                if abs((h_dt - qdt).days) <= 30:
                                    new_score += smart_time_boost
                                    break
                        except (ValueError, TypeError):
                            pass
                    meta = _meta_for(item)
                    for rd in meta.get("referenced_dates") or []:
                        try:
                            rd_dt = datetime.strptime(rd, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                            for qdt in query_dt_set:
                                if abs((rd_dt - qdt).days) <= 14:
                                    new_score += smart_time_boost
                                    break
                            if new_score != score:
                                break
                        except (ValueError, TypeError):
                            continue
                    boosted.append((new_score, item))
                boosted.sort(key=lambda t: t[0], reverse=True)
                ranked = boosted

        if smart_neighbor_sessions > 0 and ranked:
            hit_session_indices: set[int] = set()
            hit_user_ids: set[str] = set()
            for _s, item in ranked:
                meta = _meta_for(item)
                si = meta.get("session_index")
                if si is not None:
                    try:
                        hit_session_indices.add(int(si))
                    except (TypeError, ValueError):
                        pass
                uid = item.get("user_id")
                if uid:
                    hit_user_ids.add(uid)
            multi_session_signal = len(hit_session_indices) >= 2
            if (query_has_temporal or multi_session_signal) and hit_session_indices and hit_user_ids:
                neighbor_indices: set[int] = set()
                for si in hit_session_indices:
                    for offset in range(-smart_neighbor_sessions, smart_neighbor_sessions + 1):
                        if offset != 0 and (si + offset) >= 0:
                            neighbor_indices.add(si + offset)
                neighbor_indices -= hit_session_indices
                if neighbor_indices:
                    already = {item["id"] for _s, item in ranked}
                    try:
                        with _db() as db:
                            for uid in hit_user_ids:
                                for si in neighbor_indices:
                                    rows = db.execute(
                                        "SELECT id, content, title, type, metadata_json, conversation_id "
                                        "FROM memory_items "
                                        "WHERE user_id = ? AND is_deleted = 0 AND type = 'message' "
                                        "  AND metadata_json LIKE ? ",
                                        (uid, f'%"session_index": {si}%'),
                                    ).fetchall()
                                    for r in rows:
                                        if r["id"] in already:
                                            continue
                                        already.add(r["id"])
                                        meta_raw = r["metadata_json"] or "{}"
                                        try:
                                            meta = json.loads(meta_raw)
                                        except (json.JSONDecodeError, TypeError):
                                            meta = {}
                                        neighbor_item = {
                                            "id": r["id"], "content": r["content"],
                                            "title": r["title"], "type": r["type"],
                                            "metadata_json": meta_raw, "metadata": meta,
                                            "conversation_id": r["conversation_id"],
                                            "_smart_neighbor": True,
                                        }
                                        ranked.append((0.0, neighbor_item))
                    except Exception as e:
                        logger.debug(f"smart_neighbor_sessions expansion failed: {e}")

    # Phase 11 architectural fix: supersedence-aware demotion. Memories that
    # have been superseded (i.e., they appear as `to_id` on a 'supersedes'
    # edge) get their score multiplied by SUPERSEDES_PENALTY (default 0.5x).
    # Default penalty is "demote, not delete" so the older fact stays
    # retrievable for "what did I previously say about X?" questions, but
    # ranks below the newer version for "what's my current X?" Set
    # SUPERSEDES_PENALTY=0 to hide superseded items entirely; 1.0 to disable
    # demotion (no-op).
    if ranked and SUPERSEDES_PENALTY < 1.0:
        ranked_ids = [item.get("id") for _, item in ranked if isinstance(item, dict) and item.get("id")]
        if ranked_ids:
            try:
                with _db() as db:
                    placeholders = ",".join("?" * len(ranked_ids))
                    sup_rows = db.execute(
                        f"SELECT to_id FROM memory_relationships "
                        f"WHERE relationship_type = 'supersedes' "
                        f"AND to_id IN ({placeholders})",
                        ranked_ids,
                    ).fetchall()
                    superseded_ids: set = {r["to_id"] for r in sup_rows}
                if superseded_ids:
                    ranked = [
                        (
                            (s * SUPERSEDES_PENALTY) if isinstance(item, dict) and item.get("id") in superseded_ids else s,
                            item,
                        )
                        for s, item in ranked
                    ]
                    # Re-sort once after demotion so ordering reflects new scores.
                    ranked.sort(key=lambda t: t[0], reverse=True)
            except Exception as e:
                logger.debug(f"supersedence-aware demotion failed: {e}")

    # Phase D Mastra: post-rank preference for type='observation' rows.
    # When M3_PREFER_OBSERVATIONS=1, partition the ranked list into
    # obs_hits (type='observation') and raw_hits (everything else). If the
    # observations alone supply enough context (sum of token estimates above
    # M3_OBSERVATION_BUDGET_TOKENS, default 4000), return only obs_hits[:k].
    # Otherwise interleave: obs first, then raw to fill k slots. The point
    # is to favor synthesized atomic facts over raw turns when both are
    # retrieved for the same query.
    #
    # Off by default; bench harness opts in via --observer-variant flag
    # (Phase D Task 8) or callers set M3_PREFER_OBSERVATIONS=1 directly.
    if ranked and _prefer_observations_gate():
        try:
            obs_budget = int(os.environ.get("M3_OBSERVATION_BUDGET_TOKENS", "4000"))
        except ValueError:
            obs_budget = 4000
        obs_hits = [(s, it) for s, it in ranked
                    if isinstance(it, dict) and it.get("type") == "observation"]
        raw_hits = [(s, it) for s, it in ranked
                    if not (isinstance(it, dict) and it.get("type") == "observation")]
        if obs_hits:
            # Cheap token estimate: 1 token per 4 chars. The Mastra paper's
            # rationale is that an observation log displaces equivalent raw
            # turns when its summary is dense enough; we don't need precise
            # tokenization for the gate, just an order-of-magnitude check.
            obs_tokens = sum(len((it.get("content") or "")) // 4 for _, it in obs_hits)
            if obs_tokens >= obs_budget:
                # Observation-only return — observations supply enough.
                ranked = obs_hits[:k]
            else:
                # Interleave: observations first, then raw to fill remaining slots.
                slots = max(0, k - len(obs_hits))
                ranked = obs_hits + raw_hits[:slots]

    # Phase B3 (chatlog-recall plan, 2026-04-26): two-stage retrieval —
    # expand top-k observations to include their source turns. The
    # Observer's write_observation stores source_turn_ids in metadata_json;
    # when M3_TWO_STAGE_OBSERVATIONS=1 fires, we look up those rows and
    # append them to the ranked list at a small score discount so the
    # observation still ranks highest but the answerer sees the underlying
    # turns when it needs verbatim quotes.
    #
    # Off by default. The discount factor is M3_TWO_STAGE_TURN_PENALTY
    # (default 0.7 — turns rank just below their observation but ahead of
    # other raw hits).
    if ranked and _two_stage_observations_gate():
        try:
            turn_penalty = float(os.environ.get("M3_TWO_STAGE_TURN_PENALTY", "0.7"))
        except ValueError:
            turn_penalty = 0.7
        try:
            max_turns_per_obs = int(os.environ.get("M3_TWO_STAGE_MAX_TURNS_PER_OBS", "3"))
        except ValueError:
            max_turns_per_obs = 3
        # Collect source_turn_ids from observation hits (top-N only — no
        # point expanding tail-rank observations the user won't see).
        # Scope to top-k since obs_hits / raw_hits may have already been
        # collapsed back into `ranked` above.
        topk = ranked[: k]
        source_turn_ids: list[str] = []
        existing_ids = {it.get("id") for _, it in topk if isinstance(it, dict) and it.get("id")}
        for s, it in topk:
            if not isinstance(it, dict) or it.get("type") != "observation":
                continue
            # Inline meta lookup — _meta_for is scoped to a different block
            # earlier in this function. Same logic: prefer the parsed
            # metadata dict if attached, else parse metadata_json on demand.
            md = it.get("metadata") if isinstance(it.get("metadata"), dict) else None
            if md is None:
                raw = it.get("metadata_json") or "{}"
                try:
                    md = json.loads(raw) if isinstance(raw, str) else (raw if isinstance(raw, dict) else {})
                except (json.JSONDecodeError, TypeError):
                    md = {}
            stids = md.get("source_turn_ids") or []
            if isinstance(stids, list):
                # Cap how many turns we pull per observation.
                for tid in stids[:max_turns_per_obs]:
                    if isinstance(tid, str) and tid not in existing_ids:
                        source_turn_ids.append(tid)
                        existing_ids.add(tid)
        if source_turn_ids:
            try:
                with _db() as db:
                    placeholders = ",".join("?" * len(source_turn_ids))
                    turn_rows = db.execute(
                        f"SELECT id, content, title, type, importance "
                        f"FROM memory_items "
                        f"WHERE id IN ({placeholders}) AND COALESCE(is_deleted,0)=0",
                        source_turn_ids,
                    ).fetchall()
                # Find the lowest score among existing top-k as the floor,
                # then place expanded turns at floor * turn_penalty so they
                # rank below existing hits but get included in formatted output.
                base_score = min((s for s, _ in topk), default=0.5)
                floor = max(0.01, base_score * turn_penalty)
                for r in turn_rows:
                    expanded_item = dict(r) if hasattr(r, "keys") else {
                        "id": r[0], "content": r[1], "title": r[2],
                        "type": r[3], "importance": r[4] or 0.0,
                    }
                    expanded_item["_two_stage_expanded"] = True
                    ranked.append((floor, expanded_item))
                # Re-sort once so the expanded turns settle in correctly.
                ranked.sort(key=lambda t: t[0], reverse=True)
            except Exception as e:
                logger.debug(f"two-stage observation expansion failed: {e}")

    return ranked


# Module-level temporal regex - same patterns memory 2d1d5812 documented;
# 100% recall on LongMemEval temporal-reasoning, low FPR on others.
_TEMPORAL_ROUTER_PATTERNS = (
    r"\bwhen\b", r"\bhow long\b", r"\bwhat\s+(?:date|day|month|year|time)\b",
    r"\bbefore\b", r"\bafter\b", r"\bsince\b", r"\buntil\b",
    r"\b(?:days?|weeks?|months?|years?)\s+ago\b",
    r"\bfirst\b", r"\blast\b", r"\brecent(?:ly)?\b",
    r"\bearliest\b", r"\blatest\b",
    r"\bwhich\s+\w+\s+first\b", r"\bin\s+what\s+order\b",
    r"\b(?:mon|tue|wed|thu|fri|sat|sun)(?:day)?\b",
    r"\bvalentine'?s?\s+day\b", r"\bchristmas\b", r"\bthanksgiving\b", r"\bnew\s+year'?s?\b",
)
_TEMPORAL_ROUTER_RE = re.compile("|".join(_TEMPORAL_ROUTER_PATTERNS), re.IGNORECASE)

# Module-level entity mention patterns for question-time parsing (Phase 6).
# Regex-only, no SLM — same compilation style as _TEMPORAL_ROUTER_PATTERNS.
_ENTITY_MENTION_PATTERNS = (
    r'"[^"]+"',                            # double-quoted strings
    r"'[^']+'",                            # single-quoted strings
    r"\b(?:19|20)\d{2}\b",                # 4-digit years (1900–2099)
    r"\b(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2}\b",   # Month Day
    r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*",   # Capitalized noun phrases
)
_ENTITY_MENTION_RE = re.compile("|".join(_ENTITY_MENTION_PATTERNS))


# ---------------------------------------------------------------------------
# AUTO routing helpers — Phase 1 refactor.  auto_route=False (default) is a
# strict no-op; the helpers below are only invoked when auto_route=True.
# ---------------------------------------------------------------------------

_UNSET = object()  # module-level sentinel distinguishing "not passed" from "passed default"


def _extract_caller_overrides(local_args: dict, sig_defaults: dict) -> dict:
    """Return only params the caller actually changed from function-signature defaults.

    local_args: the dict of param names → values actually in use (e.g. a subset of locals())
    sig_defaults: dict of param_name -> default_value from the function's signature

    A value is considered an override when it differs from the signature default by
    identity or equality.  String/numeric/bool comparisons use ==; object sentinels
    use `is not`.
    """
    overrides = {}
    for k, v in local_args.items():
        if k not in sig_defaults:
            continue
        default = sig_defaults[k]
        # Use identity check first (catches sentinel objects), then equality.
        if v is not default and v != default:
            overrides[k] = v
    return overrides


def _apply_auto_layer(
    query: str,
    primary_candidates: list,
    current_params: dict,
    sig_defaults: dict,
) -> tuple:
    """Apply AUTO branch values to params. Caller overrides are always preserved.

    current_params: the kwargs dict reflecting what the caller actually passed
    sig_defaults: function-signature defaults (for override detection)

    Resolution order (lowest → highest priority):
      1. sig_defaults         — function-signature concrete defaults
      2. branch_vals          — AUTO branch values for the chosen branch
      3. caller_overrides     — what the caller explicitly changed from defaults

    Returns:
        (resolved_params: dict, auto_metadata: dict)

    auto_metadata contains: auto_branch, auto_branch_values, caller_overrides, auto_signals
    """
    import auto_route  # local import avoids circular; auto_route has no memory_core deps

    branch = auto_route.decide_branch(query, primary_candidates, current_params)
    branch_vals = auto_route.branch_values(branch, current_params)
    caller_overrides = _extract_caller_overrides(current_params, sig_defaults)

    # Merge layers: defaults → AUTO branch values → caller overrides
    resolved = {**sig_defaults, **branch_vals, **caller_overrides}

    return resolved, {
        "auto_branch": branch,
        "auto_branch_values": branch_vals,
        "caller_overrides": caller_overrides,
        "auto_signals": auto_route.signals_summary(query, primary_candidates),
    }


def _apply_sharp_trim(hits, *, threshold_ratio, k_min, k_max):
    """Sharp-branch post-process: keep hits within threshold_ratio of top score, bounded [k_min, k_max].

    hits: list of (score, item_dict) tuples (the canonical routed_impl output shape)
    """
    if not hits:
        return hits
    if k_max and len(hits) > k_max:
        hits = hits[:k_max]
    top_score = hits[0][0] if hits else 0.0
    if top_score <= 0:
        return hits[:max(k_min, 1)]
    threshold = top_score * threshold_ratio
    kept = [h for h in hits if h[0] >= threshold]
    if k_min and len(kept) < k_min:
        kept = hits[:k_min]
    return kept


def is_temporal_query(query: str) -> bool:
    """Returns True if the query uses temporal vocabulary (regex-based, no LLM)."""
    if not query:
        return False
    return bool(_TEMPORAL_ROUTER_RE.search(query))


def _graph_neighbor_ids(seed_ids: list, depth: int) -> set:
    """Return the set of memory_item ids reachable within `depth` hops from any
    item in `seed_ids` via memory_relationships, excluding the seeds themselves.

    Used by memory_search_routed_impl when graph_depth > 0. Returns set[str].
    """
    if depth <= 0 or not seed_ids:
        return set()
    depth = min(int(depth), 3)
    seen: set = set(seed_ids)
    frontier: set = set(seed_ids)
    with _db() as db:
        for _ in range(depth):
            if not frontier:
                break
            placeholders = ",".join(["?"] * len(frontier))
            rows = db.execute(
                f"SELECT from_id, to_id FROM memory_relationships "
                f"WHERE from_id IN ({placeholders}) OR to_id IN ({placeholders})",
                list(frontier) + list(frontier),
            ).fetchall()
            next_frontier: set = set()
            for r in rows:
                a, b = r["from_id"], r["to_id"]
                for nid in (a, b):
                    if nid not in seen:
                        seen.add(nid)
                        next_frontier.add(nid)
            frontier = next_frontier
    seen.difference_update(seed_ids)
    return seen


def _session_neighbor_ids(seed_ids: list, session_cap: int = 12) -> dict:
    """For each conversation_id present in `seed_ids`' rows, return up to
    session_cap turns from that conversation (excluding seeds themselves).

    Returns dict[memory_id -> row_dict]. Used by memory_search_routed_impl
    when expand_sessions=True. The session_cap is applied per session.
    """
    if not seed_ids:
        return {}
    out: dict = {}
    with _db() as db:
        placeholders = ",".join(["?"] * len(seed_ids))
        seed_rows = db.execute(
            f"SELECT id, conversation_id FROM memory_items WHERE id IN ({placeholders})",
            seed_ids,
        ).fetchall()
        seed_set = set(seed_ids)
        seen_conv: set = set()
        for sr in seed_rows:
            cid = sr["conversation_id"]
            if not cid or cid in seen_conv:
                continue
            seen_conv.add(cid)
            cap = max(1, int(session_cap))
            rows = db.execute(
                "SELECT id, type, title, content, metadata_json, conversation_id, "
                "valid_from, user_id FROM memory_items "
                "WHERE conversation_id = ? AND COALESCE(is_deleted, 0) = 0 "
                "ORDER BY valid_from LIMIT ?",
                (cid, cap),
            ).fetchall()
            for r in rows:
                if r["id"] in seed_set or r["id"] in out:
                    continue
                out[r["id"]] = dict(r)
    return out


async def _entity_graph_neighbor_ids(
    query: str, depth: int, max_neighbors: int, db,
    valid_types: list = None,
    valid_predicates: list = None,
) -> set:
    """Parse query for entity mentions, traverse entity_relationships up to `depth`
    hops, and return a set of memory_id values linked to the discovered entities.

    Algorithm (Phase 6, regex-only — no SLM):
      1. Extract candidate mentions from query via _ENTITY_MENTION_RE.
      2. Lookup each candidate in `entities` table (exact then LIKE, cap 5/candidate).
         If valid_types is given, restrict entity lookup to those entity_type values.
      3. BFS over `entity_relationships` up to min(depth, 3) hops,
         capped at min(max_neighbors, 100) total entity nodes.
         If valid_predicates is given, only traverse edges with matching predicate.
      4. Fetch memory_ids from `memory_item_entities` for all discovered entities.

    valid_types: list of allowed entity_type strings; None = use VALID_ENTITY_TYPES defaults.
    valid_predicates: list of allowed predicate strings; None = use VALID_ENTITY_PREDICATES defaults.

    Returns set[str] of memory_ids. Returns empty set on any early-exit condition.
    """
    if not query or not query.strip():
        return set()

    # Clamp to safe limits (mirrors memory_graph_impl clamp for depth)
    depth = min(int(depth), 3)
    max_neighbors = min(int(max_neighbors), 100)

    # Step 1 — extract candidate mention strings
    candidates: list[str] = []
    seen_cands: set[str] = set()
    for m in _ENTITY_MENTION_RE.finditer(query):
        text = m.group(0).strip("\"'")
        if text and text not in seen_cands:
            seen_cands.add(text)
            candidates.append(text)

    if not candidates:
        return set()

    # Step 2 — entity lookup: collect matched entity_ids
    try:
        # Quick check: is the entities table populated at all?
        count_row = db.execute("SELECT COUNT(*) AS cnt FROM entities").fetchone()
        if count_row["cnt"] == 0:
            return set()
    except Exception:  # noqa: BLE001
        return set()

    # Build optional entity_type filter clause (caller-provided list overrides core defaults)
    _type_clause = ""
    _type_params: list = []
    if valid_types:
        _type_ph = ",".join(["?"] * len(valid_types))
        _type_clause = f" AND entity_type IN ({_type_ph})"
        _type_params = list(valid_types)

    matched_entity_ids: set[str] = set()
    for candidate in candidates:
        try:
            # Tier 1: exact canonical_name match
            rows = db.execute(
                f"SELECT id FROM entities WHERE canonical_name = ?{_type_clause} LIMIT 5",
                [candidate] + _type_params,
            ).fetchall()
            if not rows:
                # Tier 2: case-insensitive LIKE match
                rows = db.execute(
                    f"SELECT id FROM entities WHERE LOWER(canonical_name) LIKE LOWER(?){_type_clause} LIMIT 5",
                    [f"%{candidate}%"] + _type_params,
                ).fetchall()
            for r in rows:
                matched_entity_ids.add(r["id"])
        except Exception:  # noqa: BLE001
            continue

    if not matched_entity_ids:
        return set()

    # Build optional predicate filter clause for BFS (caller-provided list overrides core defaults)
    _pred_clause = ""
    _pred_params: list = []
    if valid_predicates:
        _pred_ph = ",".join(["?"] * len(valid_predicates))
        _pred_clause = f" AND predicate IN ({_pred_ph})"
        _pred_params = list(valid_predicates)

    # Step 3 — BFS over entity_relationships up to `depth` hops
    seen_entities: set[str] = set(matched_entity_ids)
    frontier: set[str] = set(matched_entity_ids)
    for _ in range(depth):
        if not frontier or len(seen_entities) >= max_neighbors:
            break
        placeholders = ",".join(["?"] * len(frontier))
        try:
            frontier_list = list(frontier)
            rel_rows = db.execute(
                f"SELECT from_entity, to_entity FROM entity_relationships "
                f"WHERE (from_entity IN ({placeholders}) OR to_entity IN ({placeholders}))"
                f"{_pred_clause}",
                frontier_list + frontier_list + _pred_params,
            ).fetchall()
        except Exception:  # noqa: BLE001
            break
        next_frontier: set[str] = set()
        for r in rel_rows:
            for eid in (r["from_entity"], r["to_entity"]):
                if eid not in seen_entities:
                    seen_entities.add(eid)
                    next_frontier.add(eid)
                    if len(seen_entities) >= max_neighbors:
                        break
            if len(seen_entities) >= max_neighbors:
                break
        frontier = next_frontier

    # Step 4 — memory_item lookup
    if not seen_entities:
        return set()
    try:
        placeholders = ",".join(["?"] * len(seen_entities))
        mie_rows = db.execute(
            f"SELECT DISTINCT memory_id FROM memory_item_entities "
            f"WHERE entity_id IN ({placeholders})",
            list(seen_entities),
        ).fetchall()
        return {r["memory_id"] for r in mie_rows}
    except Exception:  # noqa: BLE001
        return set()


async def _score_extra_rows(query: str, rows_by_id: dict, base_score: float = 0.0) -> list:
    """Score additional rows (from graph or session expansion) against the query.

    Reuses the standard embedding path. Each returned tuple is (score, item_dict)
    matching memory_search_scored_impl's shape. Items are scored by cosine vs
    query embedding. If embedding lookup fails for a row, it gets `base_score`.
    """
    if not rows_by_id:
        return []
    out: list = []
    qvec, _ = await _embed(query)
    if qvec is None:
        # No embedding model available — fall back to base_score for all
        for rid, item in rows_by_id.items():
            out.append((base_score, item))
        return out
    with _db() as db:
        ids = list(rows_by_id.keys())
        placeholders = ",".join(["?"] * len(ids))
        emb_rows = db.execute(
            f"SELECT memory_id, embedding FROM memory_embeddings "
            f"WHERE memory_id IN ({placeholders})",
            ids,
        ).fetchall()
        emb_by_id: dict = {}
        for er in emb_rows:
            try:
                emb_by_id[er["memory_id"]] = _unpack(er["embedding"])
            except Exception:  # noqa: BLE001
                continue
    for rid, item in rows_by_id.items():
        vec = emb_by_id.get(rid)
        if vec is None:
            out.append((base_score, item))
            continue
        try:
            score = _cosine(qvec, vec)
        except Exception:  # noqa: BLE001
            score = base_score
        out.append((float(score), item))
    return out


async def memory_search_routed_impl(
    query: str,
    k: int = 10,
    fact_variant: str = "",
    temporal_k_bump: int = 5,
    graph_depth: int = 0,
    expand_sessions: bool = False,
    session_cap: int = 12,
    entity_graph: bool = False,
    entity_graph_depth: int = 1,
    entity_graph_max_neighbors: int = 20,
    entity_graph_valid_types: list = None,          # None = use VALID_ENTITY_TYPES; [] from MCP treated as None
    entity_graph_valid_predicates: list = None,     # None = use VALID_ENTITY_PREDICATES; [] from MCP treated as None
    # Cross-encoder rerank — default off; production behavior unchanged when False.
    # When True: rescores top (rerank_pool_k or 3*k) hits with sentence-transformers
    # CrossEncoder, blends with hybrid score, re-sorts. See _apply_rerank() docstring
    # and decision memory for the resolution chain.
    rerank: bool = False,
    rerank_model: str = "",                         # empty = DEFAULT_RERANK_MODEL
    rerank_pool_k: int = 0,                         # 0 = 3*k (sensible default; never below k)
    rerank_blend: float = 1.0,                      # 1.0 = pure CE replacement, 0.5 = avg, 0.0 = no-op
    user_id: str = "",
    scope: str = "",
    type_filter: str = "",
    agent_filter: str = "",
    search_mode: str = "hybrid",
    variant: str = "",
    as_of: str = "",
    conversation_id: str = "",
    explain: bool = False,
    extra_columns=None,
    recency_bias: float = 0.0,
    vector_weight: float = 0.7,
    adaptive_k: bool = False,
    elbow_sensitivity: float = 1.5,
    adaptive_k_min: int = 0,
    adaptive_k_max: int = 0,
    smart_time_boost: float = 0.0,
    smart_neighbor_sessions: int = 0,
    intent_hint: str = "",
    vector_kind_strategy: str = "default",
    # --- AUTO routing layer (opt-in, default off) ---
    # Invariant: auto_route=False produces byte-identical output to pre-refactor.
    auto_route: bool = False,
    # Signal-detection thresholds (overridable)
    auto_top1_sharp_min: float = 0.89,                     # top-1 score above which query is "sharp"
    auto_slope_at_3_sharp_min: float = 0.08,               # slope-at-3 above which query is "sharp"
    auto_conv_id_diversity_threshold: int = 5,             # conv_id diversity above which → multi_session
    auto_top1_low_threshold: float = 0.50,                 # OOD guard — below this, not sharp
    # Branch values: temporal
    auto_temporal_k: int = 15,                             # k for temporal branch
    auto_temporal_recency_bias: float = 0.05,              # recency_bias for temporal branch
    auto_temporal_expand_sessions: bool = True,            # expand_sessions for temporal branch
    auto_temporal_graph_depth: int = 1,                    # graph_depth for temporal branch (AUTO_v2 fix)
    # Branch values: multi_session
    auto_multi_k: int = 20,                                # k for multi_session branch
    auto_multi_expand_sessions: bool = True,               # expand_sessions for multi_session branch
    # Branch values: sharp (post-process trim)
    auto_sharp_threshold_ratio: float = 0.85,              # trim hits below top_score * ratio
    auto_sharp_k_min: int = 3,                             # floor after threshold trim
    auto_sharp_k_max: int = 10,                            # ceiling after threshold trim
    # Branch values: entity_anchored (AUTO entity-graph expansion)
    auto_entity_graph_enabled: bool = True,                # AUTO fires entity branch when query has named entities
    auto_entity_graph_depth: int = 1,                      # entity_graph_depth for entity_anchored branch
    auto_entity_graph_max_neighbors: int = 20,             # entity_graph_max_neighbors for entity_anchored branch
    auto_entity_graph_named_entity_threshold: int = 1,     # min named entities to fire entity_anchored branch
    # Capture mechanism (option b): caller passes a dict, function populates it
    _capture_dict: dict = None,
) -> list:
    """Temporal-aware routed retrieval, with optional graph + session expansion.

    Rule:
      - if is_temporal_query(query): retrieve verbatim only at (k + temporal_k_bump)
        with vector_kind_strategy='default'
      - else: retrieve at k. If fact_variant is non-empty, fuse base-variant hits
        with fact-variant hits client-side (max-fusion by score per memory_id).
        If fact_variant is empty, this collapses to a standard memory_search_scored_impl
        call at vector_kind_strategy='max' (so any pre-existing dual-embed rows
        on the base variant get used).

    Optional post-retrieval expansions (both opt-in, default off):
      - graph_depth > 0: traverse memory_relationships from each top-k hit up
        to N hops (clamped to 3), score the new rows against the query, and
        max-fuse them into the result before re-trimming to k.
      - expand_sessions=True: pull all turns sharing each top-k hit's
        conversation_id (capped at session_cap per conversation), score them
        against the query, and max-fuse. Useful for supersession / context-
        recovery questions.

    AUTO routing layer (opt-in via auto_route=True):
      When auto_route=True, a two-pass strategy is used. First an overshoot
      retrieval at k=20 is run to obtain post-retrieval signals (score curve,
      conv_id diversity). The branch decision then sets unset retrieval
      parameters before the main retrieval proceeds. Caller-explicit values
      always win over AUTO branch values. When auto_route=False (the default),
      none of this runs and behaviour is byte-identical to pre-refactor.

      If _capture_dict is passed (a mutable dict), it is populated with:
        auto_branch, auto_branch_values, caller_overrides, auto_signals.

    Returns the same shape as memory_search_scored_impl: list[tuple[score, dict]].
    """
    # AUTO routing layer (opt-in, default off — invariant: off = byte-identical to today)
    auto_metadata = None
    resolved = None
    if auto_route:
        # Run a quick overshoot to obtain post-retrieval signals for branch decision.
        # Pre-retrieval signals (temporal cue regex) may short-circuit this — but we
        # always run the overshoot to keep the code path simple and branch_values
        # consistent across all branches.
        overshoot_candidates = await memory_search_scored_impl(
            query, k=20, user_id=user_id, scope=scope,
            type_filter=type_filter, agent_filter=agent_filter,
            search_mode=search_mode, variant=variant, as_of=as_of,
            conversation_id=conversation_id, extra_columns=extra_columns,
            vector_kind_strategy=vector_kind_strategy,
        )

        # Signature defaults for all overridable retrieval knobs.
        # These must match the function signature defaults above exactly.
        _sig_defaults = {
            "k": 10,
            "temporal_k_bump": 5,
            "graph_depth": 0,
            "expand_sessions": False,
            "session_cap": 12,
            "recency_bias": 0.0,
            "vector_weight": 0.7,
            # Entity-graph knobs (for override detection)
            "entity_graph": False,
            "entity_graph_depth": 1,
            "entity_graph_max_neighbors": 20,
            # AUTO threshold defaults (for override detection only)
            "auto_top1_sharp_min": 0.89,
            "auto_slope_at_3_sharp_min": 0.08,
            "auto_conv_id_diversity_threshold": 5,
            "auto_top1_low_threshold": 0.50,
            # AUTO branch value defaults
            "auto_temporal_k": 15,
            "auto_temporal_recency_bias": 0.05,
            "auto_temporal_expand_sessions": True,
            "auto_temporal_graph_depth": 1,
            "auto_multi_k": 20,
            "auto_multi_expand_sessions": True,
            "auto_sharp_threshold_ratio": 0.85,
            "auto_sharp_k_min": 3,
            "auto_sharp_k_max": 10,
            # AUTO entity_anchored branch defaults
            "auto_entity_graph_enabled": True,
            "auto_entity_graph_depth": 1,
            "auto_entity_graph_max_neighbors": 20,
            "auto_entity_graph_named_entity_threshold": 1,
        }

        # Current param values (what the caller actually passed or defaulted to).
        _current_params = {
            "k": k,
            "temporal_k_bump": temporal_k_bump,
            "graph_depth": graph_depth,
            "expand_sessions": expand_sessions,
            "session_cap": session_cap,
            "recency_bias": recency_bias,
            "vector_weight": vector_weight,
            # Entity-graph knobs (pass-through so AUTO layer can detect caller overrides)
            "entity_graph": entity_graph,
            "entity_graph_depth": entity_graph_depth,
            "entity_graph_max_neighbors": entity_graph_max_neighbors,
            # Threshold overrides (pass-through so decide_branch can read them)
            "auto_top1_sharp_min": auto_top1_sharp_min,
            "auto_slope_at_3_sharp_min": auto_slope_at_3_sharp_min,
            "auto_conv_id_diversity_threshold": auto_conv_id_diversity_threshold,
            "auto_top1_low_threshold": auto_top1_low_threshold,
            # Branch value overrides (pass-through so branch_values can read them)
            "auto_temporal_k": auto_temporal_k,
            "auto_temporal_recency_bias": auto_temporal_recency_bias,
            "auto_temporal_expand_sessions": auto_temporal_expand_sessions,
            "auto_temporal_graph_depth": auto_temporal_graph_depth,
            "auto_multi_k": auto_multi_k,
            "auto_multi_expand_sessions": auto_multi_expand_sessions,
            "auto_sharp_threshold_ratio": auto_sharp_threshold_ratio,
            "auto_sharp_k_min": auto_sharp_k_min,
            "auto_sharp_k_max": auto_sharp_k_max,
            # AUTO entity_anchored branch values (pass-through for decide_branch)
            "auto_entity_graph_enabled": auto_entity_graph_enabled,
            "auto_entity_graph_depth": auto_entity_graph_depth,
            "auto_entity_graph_max_neighbors": auto_entity_graph_max_neighbors,
            "auto_entity_graph_named_entity_threshold": auto_entity_graph_named_entity_threshold,
        }

        resolved, auto_metadata = _apply_auto_layer(
            query, overshoot_candidates, _current_params, _sig_defaults
        )

        # Apply resolved values back to local variables so the rest of the
        # function (which is unchanged) uses the AUTO-adjusted parameters.
        k = resolved["k"]
        temporal_k_bump = resolved["temporal_k_bump"]
        graph_depth = resolved["graph_depth"]
        expand_sessions = resolved["expand_sessions"]
        session_cap = resolved["session_cap"]
        recency_bias = resolved["recency_bias"]
        vector_weight = resolved["vector_weight"]
        # Apply AUTO entity-graph values.
        # Precedence rule: if auto_entity_graph_enabled=False, AUTO must NOT enable entity_graph.
        # Also: if caller explicitly passed entity_graph=False (recorded in caller_overrides),
        # that beats the entity_anchored branch value.
        _eg_caller_overrides = auto_metadata.get("caller_overrides", {})
        _eg_auto_blocked = (
            not auto_entity_graph_enabled
            or ("entity_graph" in _eg_caller_overrides and not _eg_caller_overrides["entity_graph"])
        )
        if _eg_auto_blocked and auto_metadata.get("auto_branch") == "entity_anchored":
            # Suppress AUTO's entity_graph=True — use the original entity_graph value
            resolved["entity_graph"] = entity_graph
        entity_graph = resolved.get("entity_graph", entity_graph)
        entity_graph_depth = resolved.get("entity_graph_depth", entity_graph_depth)
        entity_graph_max_neighbors = resolved.get("entity_graph_max_neighbors", entity_graph_max_neighbors)

        # Populate caller-supplied capture dict if present.
        if _capture_dict is not None:
            _capture_dict.update(auto_metadata)

    # Normalize MCP empty-list sentinel → None for entity vocab overrides.
    # This happens unconditionally (covers both auto_route=True and False paths).
    _egt = entity_graph_valid_types if entity_graph_valid_types else None
    _egp = entity_graph_valid_predicates if entity_graph_valid_predicates else None

    # Read env-var override for the bump
    bump = int(os.environ.get("M3_ROUTER_TEMPORAL_K_BUMP", str(temporal_k_bump)))

    if is_temporal_query(query):
        primary = await memory_search_scored_impl(
            query, k=k + bump, user_id=user_id, scope=scope,
            type_filter=type_filter, agent_filter=agent_filter,
            search_mode=search_mode, variant=variant, as_of=as_of,
            conversation_id=conversation_id, explain=explain,
            extra_columns=extra_columns, recency_bias=recency_bias,
            vector_weight=vector_weight, adaptive_k=adaptive_k,
            elbow_sensitivity=elbow_sensitivity, adaptive_k_min=adaptive_k_min,
            adaptive_k_max=adaptive_k_max, smart_time_boost=smart_time_boost,
            smart_neighbor_sessions=smart_neighbor_sessions,
            intent_hint=intent_hint, vector_kind_strategy="default",
        )
        final_hits = await _maybe_expand_routed(
            query, primary, k=k + bump,
            graph_depth=graph_depth,
            expand_sessions=expand_sessions, session_cap=session_cap,
            entity_graph=entity_graph,
            entity_graph_depth=entity_graph_depth,
            entity_graph_max_neighbors=entity_graph_max_neighbors,
            entity_graph_valid_types=_egt,
            entity_graph_valid_predicates=_egp,
        )
    else:
        # Non-temporal path
        base_hits = await memory_search_scored_impl(
            query, k=k * 2 if fact_variant else k,
            user_id=user_id, scope=scope, type_filter=type_filter,
            agent_filter=agent_filter, search_mode=search_mode,
            variant=variant, as_of=as_of, conversation_id=conversation_id,
            explain=explain, extra_columns=extra_columns, recency_bias=recency_bias,
            vector_weight=vector_weight, adaptive_k=adaptive_k,
            elbow_sensitivity=elbow_sensitivity, adaptive_k_min=adaptive_k_min,
            adaptive_k_max=adaptive_k_max, smart_time_boost=smart_time_boost,
            smart_neighbor_sessions=smart_neighbor_sessions,
            intent_hint=intent_hint, vector_kind_strategy="max",
        )

        if not fact_variant:
            final_hits = await _maybe_expand_routed(
                query, base_hits[:k], k=k,
                graph_depth=graph_depth,
                expand_sessions=expand_sessions, session_cap=session_cap,
                entity_graph=entity_graph,
                entity_graph_depth=entity_graph_depth,
                entity_graph_max_neighbors=entity_graph_max_neighbors,
                entity_graph_valid_types=_egt,
                entity_graph_valid_predicates=_egp,
            )
        else:
            # Fuse with fact_variant hits (client-side max-fusion by memory_id, top-k)
            fact_hits = await memory_search_scored_impl(
                query, k=k * 2, user_id=user_id, scope=scope,
                type_filter=type_filter, agent_filter=agent_filter,
                search_mode=search_mode, variant=fact_variant, as_of=as_of,
                conversation_id=conversation_id, explain=explain,
                extra_columns=extra_columns, recency_bias=recency_bias,
                vector_weight=vector_weight, adaptive_k=adaptive_k,
                elbow_sensitivity=elbow_sensitivity, adaptive_k_min=adaptive_k_min,
                adaptive_k_max=adaptive_k_max, smart_time_boost=smart_time_boost,
                smart_neighbor_sessions=smart_neighbor_sessions,
                intent_hint=intent_hint, vector_kind_strategy="default",
            )

            # Both return list[tuple[score, dict]]. Dedupe by item id, keep highest score.
            best: dict = {}  # memory_id -> (score, item)
            for s, item in base_hits + fact_hits:
                mid = item.get("id") if isinstance(item, dict) else None
                if mid is None:
                    continue
                if mid not in best or s > best[mid][0]:
                    best[mid] = (s, item)
            fused = sorted(best.values(), key=lambda x: x[0], reverse=True)[:k]
            final_hits = await _maybe_expand_routed(
                query, fused, k=k,
                graph_depth=graph_depth,
                expand_sessions=expand_sessions, session_cap=session_cap,
                entity_graph=entity_graph,
                entity_graph_depth=entity_graph_depth,
                entity_graph_max_neighbors=entity_graph_max_neighbors,
                entity_graph_valid_types=_egt,
                entity_graph_valid_predicates=_egp,
            )

    # Sharp-branch post-process trim (only when AUTO routing is active and sharp branch fired)
    if auto_route and auto_metadata and auto_metadata.get("auto_branch") == "sharp":
        final_hits = _apply_sharp_trim(
            final_hits,
            threshold_ratio=resolved["auto_sharp_threshold_ratio"],
            k_min=resolved["auto_sharp_k_min"],
            k_max=resolved["auto_sharp_k_max"],
        )
        if _capture_dict is not None:
            _capture_dict["sharp_post_trim_count"] = len(final_hits)

    # Entity-anchored capture: count entity-graph neighbors added to final hits.
    if auto_route and auto_metadata and auto_metadata.get("auto_branch") == "entity_anchored":
        if _capture_dict is not None:
            eg_count = sum(
                1 for _, item in final_hits
                if isinstance(item, dict) and item.get("_expanded_via") == "entity_graph"
            )
            _capture_dict["entity_graph_neighbors_added"] = eg_count

    # Cross-encoder rerank pass (default off). Runs LAST so it sees the fully
    # expanded + sharp-trimmed result set, including entity-graph neighbors.
    # CONTRACT: rerank=False → byte-identical to pre-feature behavior.
    if rerank:
        _model = rerank_model or DEFAULT_RERANK_MODEL
        _pool = rerank_pool_k if rerank_pool_k > 0 else (3 * k)
        _final_n = len(final_hits)
        final_hits = _apply_rerank(
            final_hits,
            query,
            pool_k=_pool,
            final_k=k,
            model_name=_model,
            blend=rerank_blend,
        )
        if _capture_dict is not None:
            _capture_dict["rerank_applied"] = True
            _capture_dict["rerank_model"] = _model
            _capture_dict["rerank_pool_k"] = _pool
            _capture_dict["rerank_blend"] = rerank_blend
            _capture_dict["rerank_pre_count"] = _final_n
            _capture_dict["rerank_post_count"] = len(final_hits)

    return final_hits


async def _maybe_expand_routed(
    query: str, primary: list, k: int,
    graph_depth: int = 0,
    expand_sessions: bool = False,
    session_cap: int = 12,
    entity_graph: bool = False,
    entity_graph_depth: int = 1,
    entity_graph_max_neighbors: int = 20,
    entity_graph_valid_types: list = None,
    entity_graph_valid_predicates: list = None,
) -> list:
    """Apply optional graph, session, and entity-graph expansion to a routed retrieval result.

    All three expansions take the primary top-k hits' ids (or the query, for entity_graph)
    as seeds, fetch new rows, score them against the query, and max-fuse with the primary
    list. If all are off (the default), returns primary unchanged.
    """
    if graph_depth <= 0 and not expand_sessions and not entity_graph:
        return primary
    seed_ids = [item.get("id") for _, item in primary if isinstance(item, dict) and item.get("id")]
    if not seed_ids and not entity_graph:
        return primary

    # Build dict of new rows (memory_id -> row dict), avoiding duplicates of primary seeds.
    primary_ids: set = {item.get("id") for _, item in primary if isinstance(item, dict) and item.get("id")}
    extra_rows: dict = {}
    # Track which expansion source each extra row came from for _expanded_via tagging.
    extra_row_source: dict = {}  # memory_id -> "graph" | "session" | "entity_graph"

    if graph_depth > 0 and seed_ids:
        neighbor_ids = _graph_neighbor_ids(seed_ids, depth=int(graph_depth))
        if neighbor_ids:
            with _db() as db:
                placeholders = ",".join(["?"] * len(neighbor_ids))
                rows = db.execute(
                    f"SELECT id, type, title, content, metadata_json, conversation_id, "
                    f"valid_from, user_id FROM memory_items "
                    f"WHERE id IN ({placeholders}) AND COALESCE(is_deleted, 0) = 0",
                    list(neighbor_ids),
                ).fetchall()
                for r in rows:
                    extra_rows[r["id"]] = dict(r)
                    extra_row_source[r["id"]] = "graph"

    if expand_sessions and seed_ids:
        session_rows = _session_neighbor_ids(seed_ids, session_cap=int(session_cap))
        for rid, item in session_rows.items():
            if rid not in extra_rows:
                extra_rows[rid] = item
                extra_row_source[rid] = "session"

    if entity_graph:
        try:
            with _db() as db:
                eg_memory_ids = await _entity_graph_neighbor_ids(
                    query,
                    depth=int(entity_graph_depth),
                    max_neighbors=int(entity_graph_max_neighbors),
                    db=db,
                    valid_types=entity_graph_valid_types,
                    valid_predicates=entity_graph_valid_predicates,
                )
            new_ids = eg_memory_ids - primary_ids - set(extra_rows.keys())
            if new_ids:
                with _db() as db:
                    placeholders = ",".join(["?"] * len(new_ids))
                    eg_rows = db.execute(
                        f"SELECT id, type, title, content, metadata_json, conversation_id, "
                        f"valid_from, user_id FROM memory_items "
                        f"WHERE id IN ({placeholders}) AND COALESCE(is_deleted, 0) = 0",
                        list(new_ids),
                    ).fetchall()
                    for r in eg_rows:
                        if r["id"] not in extra_rows:
                            extra_rows[r["id"]] = dict(r)
                            extra_row_source[r["id"]] = "entity_graph"
        except Exception:  # noqa: BLE001
            pass  # entity_graph expansion is best-effort; never crash the primary path

    if not extra_rows:
        # Tag primary hits as "primary" and return unchanged.
        for _, item in primary:
            if isinstance(item, dict) and "_expanded_via" not in item:
                item["_expanded_via"] = "primary"
        return primary

    # Tag each extra row with its expansion source before scoring.
    for mid, item in extra_rows.items():
        item["_expanded_via"] = extra_row_source.get(mid, "graph")

    # Tag primary items as "primary" before fusion.
    for _, item in primary:
        if isinstance(item, dict) and "_expanded_via" not in item:
            item["_expanded_via"] = "primary"

    scored_extras = await _score_extra_rows(query, extra_rows, base_score=0.0)

    best: dict = {}
    for s, item in primary + scored_extras:
        mid = item.get("id") if isinstance(item, dict) else None
        if mid is None:
            continue
        if mid not in best or s > best[mid][0]:
            best[mid] = (s, item)
        elif s == best[mid][0] and item.get("_expanded_via", "primary") != "primary":
            # On exact score tie, prefer the non-primary tag to preserve cross-peer evidence.
            best[mid] = (s, item)
    fused = sorted(best.values(), key=lambda x: x[0], reverse=True)[:k]
    return fused


async def memory_search_impl(query, k=8, type_filter="", agent_filter="", search_mode="hybrid", include_scratchpad=False, user_id="", scope="", as_of="", explain=False, conversation_id="", recency_bias=0.0, adaptive_k=False, variant="", intent_hint="", _depth=0):
    ranked = await memory_search_scored_impl(
        query, k=k, type_filter=type_filter, agent_filter=agent_filter,
        search_mode=search_mode, user_id=user_id, scope=scope, as_of=as_of,
        conversation_id=conversation_id, explain=explain,
        recency_bias=float(recency_bias) if recency_bias else 0.0,
        adaptive_k=bool(adaptive_k),
        variant=variant,
        intent_hint=intent_hint,
        # metadata_json needed for role-boost + predecessor pull when
        # intent_hint routing is live.
        extra_columns=["metadata_json", "conversation_id"] if intent_hint else None,
    )
    if ranked is None:
        return "Search failed: FTS and semantic both unavailable."

    if not ranked:
        return "No results found."
    lines = [f"Top {len(ranked)} results:"]
    for rank, (score, item) in enumerate(ranked, 1):
        content = item.get("content") or ""
        lines.append("-" * 40)
        lines.append(f"{rank}. [{item['id']}] score={score:.4f}  type: {item.get('type', 'unknown')}  title: {item.get('title','')}")

        if explain and "_explanation" in item:
            exp = item["_explanation"]
            if "raw_hybrid" in exp:
                vw = exp.get("vector_weight", 0.7)
                lines.append(f"   Breakdown: vector={exp['vector']:.4f} (weight {vw:.2f}) + bm25={exp['bm25']:.4f} (weight {1.0-vw:.2f}) -> raw={exp['raw_hybrid']:.4f}")
                if "mmr_penalty" in exp:
                    lines.append(f"   MMR penalty: -{exp['mmr_penalty']:.4f} (max_sim_to_selected={exp['max_sim_to_selected']:.4f})")
                lines.append(f"   Importance: {exp['importance']:.4f}")
            else:
                lines.append(f"   Source: {exp.get('source', 'unknown')}")

        lines.append(f"Content:\n{content}\n")
    lines.append("-" * 40)
    return "\n".join(lines)

async def memory_suggest_impl(query: str, k: int = 5, variant: str = "__none__") -> str:
    """Returns which memories would be retrieved for a query and explains why."""
    return await memory_search_impl(query, k=k, explain=True, variant=variant)

def memory_get_impl(id):
    # Accept either a 36-char UUID (existing path) or an 8-char prefix
    # (resume-guides and conversations routinely cite memories by their
    # first 8 hex chars). Anything else is a length error — we don't try
    # to be clever about other prefix lengths because the index only
    # covers SUBSTR(id,1,8).
    ident = (id or "").strip()
    if len(ident) == 36:
        with _db() as db:
            row = db.execute("SELECT * FROM memory_items WHERE id = ?", (ident,)).fetchone()
            if not row:
                # Fall back to chroma_mirror for items pulled from remote
                mirror = db.execute("SELECT * FROM chroma_mirror WHERE id = ?", (ident,)).fetchone()
                if mirror:
                    return json.dumps(dict(mirror), indent=2, default=str)
                return "Error: not found"
        return json.dumps(dict(row), indent=2, default=str)
    if len(ident) == 8:
        with _db() as db:
            rows = db.execute(
                "SELECT * FROM memory_items WHERE SUBSTR(id,1,8) = ?",
                (ident,),
            ).fetchall()
            if not rows:
                # Fall back to chroma_mirror by prefix as well, for symmetry
                # with the full-UUID path above.
                mirror_rows = db.execute(
                    "SELECT * FROM chroma_mirror WHERE SUBSTR(id,1,8) = ?",
                    (ident,),
                ).fetchall()
                if len(mirror_rows) == 1:
                    return json.dumps(dict(mirror_rows[0]), indent=2, default=str)
                if len(mirror_rows) > 1:
                    ids = ", ".join(r["id"] for r in mirror_rows)
                    return f"Error: ambiguous prefix '{ident}': matches {ids}"
                return "Error: not found"
            if len(rows) > 1:
                ids = ", ".join(r["id"] for r in rows)
                return f"Error: ambiguous prefix '{ident}': matches {ids}"
        return json.dumps(dict(rows[0]), indent=2, default=str)
    return "Error: id must be 36-char UUID or 8-char prefix"

def memory_verify_impl(memory_id: str) -> str:
    """Verify content integrity by comparing stored hash with computed hash."""
    with _db() as db:
        row = db.execute("SELECT content, content_hash FROM memory_items WHERE id = ?", (memory_id,)).fetchone()
        if not row:
            return f"Error: memory {memory_id} not found"
        stored_hash = row["content_hash"] or ""
        computed_hash = hashlib.sha256((row["content"] or "").encode("utf-8")).hexdigest()
        if not stored_hash:
            return f"Warning: no content hash stored for {memory_id}. Computed: {computed_hash}"
        if stored_hash == computed_hash:
            return f"Integrity OK: {memory_id} (hash: {computed_hash[:16]}...)"
        return f"INTEGRITY VIOLATION: {memory_id} — stored hash {stored_hash[:16]}... != computed {computed_hash[:16]}..."

def memory_cost_report_impl() -> str:
    """Returns current session cost/usage counters."""
    lines = ["Memory Operation Costs (this session):"]
    for key, val in sorted(_COST_COUNTERS.items()):
        lines.append(f"  {key}: {val}")
    return "\n".join(lines)

async def memory_update_impl(id, content="", title="", metadata="", importance=-1.0, reembed=False, refresh_on="", refresh_reason="", conversation_id=""):
    if isinstance(metadata, dict):
        metadata = json.dumps(metadata)
    elif not isinstance(metadata, str):
        metadata = ""
    now = datetime.now(timezone.utc).isoformat()
    try:
        importance = float(importance)
    except (TypeError, ValueError):
        importance = -1.0
    with _db() as db:
        # Read old values for audit trail
        old = db.execute(
            "SELECT content, title, refresh_on, refresh_reason, conversation_id FROM memory_items WHERE id = ?",
            (id,)
        ).fetchone()
        if content:
            _record_history(id, "update", old["content"] if old else None, content, "content", db=db)
            db.execute("UPDATE memory_items SET content = ? WHERE id = ?", (content, id))
        if title:
            _record_history(id, "update", old["title"] if old else None, title, "title", db=db)
            db.execute("UPDATE memory_items SET title = ? WHERE id = ?", (title, id))
        if importance >= 0: db.execute("UPDATE memory_items SET importance = ? WHERE id = ?", (importance, id))
        if metadata: db.execute("UPDATE memory_items SET metadata_json = ? WHERE id = ?", (metadata, id))
        # Refresh lifecycle: empty string leaves unchanged, "clear" clears, anything
        # else is treated as a new ISO timestamp. Using the explicit sentinel "clear"
        # lets callers distinguish "no change" from "mark as refreshed, remove reminder".
        if refresh_on:
            new_val = None if refresh_on == "clear" else refresh_on
            _record_history(id, "update", old["refresh_on"] if old else None, new_val, "refresh_on", db=db)
            db.execute("UPDATE memory_items SET refresh_on = ? WHERE id = ?", (new_val, id))
        if refresh_reason:
            new_val = None if refresh_reason == "clear" else refresh_reason
            _record_history(id, "update", old["refresh_reason"] if old else None, new_val, "refresh_reason", db=db)
            db.execute("UPDATE memory_items SET refresh_reason = ? WHERE id = ?", (new_val, id))
        if conversation_id:
            new_val = None if conversation_id == "clear" else conversation_id
            _record_history(id, "update", old["conversation_id"] if old else None, new_val, "conversation_id", db=db)
            db.execute("UPDATE memory_items SET conversation_id = ? WHERE id = ?", (new_val, id))
        db.execute("UPDATE memory_items SET updated_at = ? WHERE id = ?", (now, id))
    if reembed and content:
        vec, m = await _embed(content)
        if vec:
            with _db() as db:
                db.execute("UPDATE memory_embeddings SET embedding = ?, embed_model = ? WHERE memory_id = ?", (_pack(vec), m, id))
    return f"Updated: {id}"

def _cosine(v1: list[float], v2: list[float]) -> float:
    """Fallback cosine similarity if numpy is unavailable."""
    from embedding_utils import cosine
    return cosine(v1, v2)

def memory_delete_impl(id, hard=False):
    """Deletes a MemoryItem (soft or hard). Implements cascade for hard delete (C5)."""
    with _db() as db:
        row = db.execute("SELECT id, content FROM memory_items WHERE id = ?", (id,)).fetchone()
        if not row:
            return f"Error: item {id} not found"
        _record_history(id, "delete", row["content"], None, "content", db=db)
        if hard:
            db.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (id,))
            db.execute("DELETE FROM memory_relationships WHERE from_id = ? OR to_id = ?", (id, id))
            db.execute("DELETE FROM chroma_sync_queue WHERE memory_id = ?", (id,))
            db.execute("DELETE FROM memory_items WHERE id = ?", (id,))
        else:
            db.execute("UPDATE memory_items SET is_deleted = 1, updated_at = ? WHERE id = ?",
                       (datetime.now(timezone.utc).isoformat(), id))
            # Drop any pending upsert in chroma_sync_queue — the row is no
            # longer eligible for sync. The tombstone enqueue (if the caller
            # uses _queue_chroma(..., 'delete') downstream) is unaffected.
            db.execute(
                "DELETE FROM chroma_sync_queue WHERE memory_id = ? AND operation = 'upsert'",
                (id,),
            )
    return f"{'Hard' if hard else 'Soft'}-deleted: {id}"

VALID_RELATIONSHIP_TYPES = {"related", "supports", "contradicts", "extends", "supersedes", "references", "message", "consolidates", "handoff", "precedes", "follows"}

def _memory_link_inner(from_id: str, to_id: str, relationship_type: str, db) -> str:
    # Verify both items exist
    for mid in (from_id, to_id):
        if not db.execute("SELECT id FROM memory_items WHERE id = ?", (mid,)).fetchone():
            return f"Error: memory {mid} not found"
    # Check for duplicate link
    existing = db.execute(
        "SELECT id FROM memory_relationships WHERE from_id = ? AND to_id = ? AND relationship_type = ?",
        (from_id, to_id, relationship_type)
    ).fetchone()
    if existing:
        return f"Link already exists: {existing['id']}"
    rid = str(uuid.uuid4())
    db.execute(
        "INSERT INTO memory_relationships (id, from_id, to_id, relationship_type, created_at) VALUES (?,?,?,?,?)",
        (rid, from_id, to_id, relationship_type, datetime.now(timezone.utc).isoformat())
    )
    return f"Linked: {from_id} --[{relationship_type}]--> {to_id} (id: {rid})"


def memory_link_impl(from_id: str, to_id: str, relationship_type: str = "related", db=None) -> str:
    """Creates a directional link between two memory items."""
    if relationship_type not in VALID_RELATIONSHIP_TYPES:
        return f"Error: invalid relationship type '{relationship_type}'. Valid: {', '.join(sorted(VALID_RELATIONSHIP_TYPES))}"

    if db is not None:
        return _memory_link_inner(from_id, to_id, relationship_type, db)

    with _db() as db:
        return _memory_link_inner(from_id, to_id, relationship_type, db)

def memory_graph_impl(memory_id: str, depth: int = 1) -> str:
    """Returns the local graph neighborhood of a memory item up to N hops."""
    depth = min(max(int(depth), 1), 3)  # Clamp to 1-3
    with _db() as db:
        # Verify item exists
        root = db.execute("SELECT id, title, type FROM memory_items WHERE id = ?", (memory_id,)).fetchone()
        if not root:
            return f"Error: memory {memory_id} not found"

        # Recursive CTE to traverse relationships up to `depth` hops
        rows = db.execute("""
            WITH RECURSIVE graph(node_id, hop) AS (
                SELECT ?, 0
                UNION ALL
                SELECT CASE WHEN mr.from_id = g.node_id THEN mr.to_id ELSE mr.from_id END, g.hop + 1
                FROM memory_relationships mr
                JOIN graph g ON (mr.from_id = g.node_id OR mr.to_id = g.node_id)
                WHERE g.hop < ?
            )
            SELECT DISTINCT mi.id, mi.title, mi.type, g.hop
            FROM graph g
            JOIN memory_items mi ON g.node_id = mi.id
            WHERE mi.is_deleted = 0
            ORDER BY g.hop, mi.type
        """, (memory_id, depth)).fetchall()

        # Also get the edges
        node_ids = [r["id"] for r in rows]
        if not node_ids:
            return f"No graph neighborhood for {memory_id}"
        placeholders = ",".join(["?"] * len(node_ids))
        edges = db.execute(
            f"SELECT from_id, to_id, relationship_type FROM memory_relationships "
            f"WHERE from_id IN ({placeholders}) OR to_id IN ({placeholders})",
            node_ids + node_ids
        ).fetchall()

    lines = [f"Graph for {root['title'] or root['id']} (type={root['type']}, depth={depth}):"]
    lines.append(f"\nNodes ({len(rows)}):")
    for r in rows:
        hop_label = "ROOT" if r["id"] == memory_id else f"hop {r['hop']}"
        lines.append(f"  [{r['id'][:8]}] {r['title'] or '(untitled)'} (type={r['type']}, {hop_label})")

    # Filter edges to only those connecting our nodes
    node_set = set(node_ids)
    relevant_edges = [e for e in edges if e["from_id"] in node_set and e["to_id"] in node_set]
    if relevant_edges:
        lines.append(f"\nEdges ({len(relevant_edges)}):")
        for e in relevant_edges:
            lines.append(f"  {e['from_id'][:8]} --[{e['relationship_type']}]--> {e['to_id'][:8]}")

    return "\n".join(lines)

def memory_handoff_impl(from_agent: str, to_agent: str, task: str,
                        context_ids: list, note: str = "",
                        task_id: str = "") -> str:
    """Creates a handoff memory for inter-agent task transfer."""
    # 0. Validate agents are registered
    if not _agent_exists(to_agent):
        return f"Error: to_agent '{to_agent}' is not registered. Call agent_register first."
    if not _agent_exists(from_agent):
        return f"Error: from_agent '{from_agent}' is not registered. Call agent_register first."

    # 1. Generate new UUID
    new_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # 2. Insert handoff memory directly via raw SQL
    with _db() as db:
        meta = {"from_agent": from_agent, "note": note}
        if task_id:
            meta["task_id"] = task_id
        metadata_json = json.dumps(meta)
        db.execute(
            "INSERT INTO memory_items (id, type, title, content, agent_id, scope, metadata_json, created_at, updated_at, is_deleted) "
            "VALUES (?, 'handoff', ?, ?, ?, 'agent', ?, ?, ?, 0)",
            (new_id, f"Handoff from {from_agent}", task, to_agent, metadata_json, now, now)
        )

    # 3. Link context items (each opens its own _db() context)
    for ctx_id in context_ids:
        try:
            memory_link_impl(new_id, ctx_id, "handoff")
        except Exception as e:
            logger.debug(f"Failed to link context {ctx_id}: {e}")

    # 4. Record history
    _record_history(new_id, "handoff_create", None, task, "content", from_agent)

    # 5. Fire-and-forget notify to to_agent
    try:
        notify_impl(to_agent, "handoff", {
            "memory_id": new_id,
            "from_agent": from_agent,
            "task": (task or "")[:200],
            "task_id": task_id or None,
        })
    except Exception as e:
        logger.warning(f"handoff notify failed for {to_agent}: {e}")

    # 6. Return status
    return f"Handoff created: {new_id} ({from_agent} -> {to_agent}, {len(context_ids)} context links)"

def memory_inbox_impl(agent_id: str, unread_only: bool = True, limit: int = 20) -> str:
    """Retrieves handoff messages for an agent, optionally filtered to unread."""
    # Build WHERE clause dynamically
    where_clause = "WHERE agent_id = ? AND type = 'handoff' AND is_deleted = 0"
    if unread_only:
        where_clause += " AND read_at IS NULL"

    # Query the inbox
    with _db() as db:
        rows = db.execute(
            f"SELECT id, title, content, metadata_json, created_at, read_at FROM memory_items "
            f"{where_clause} ORDER BY created_at DESC LIMIT ?",
            (agent_id, limit)
        ).fetchall()

    # Format result
    if not rows:
        return f"Inbox for {agent_id}: (empty)"

    lines = [f"Inbox for {agent_id} ({len(rows)} {'unread' if unread_only else 'total'}):"]
    for row in rows:
        # Parse from_agent from metadata_json
        from_agent = "?"
        try:
            meta = json.loads(row["metadata_json"] or "{}")
            from_agent = meta.get("from_agent", "?")
        except Exception:
            pass

        # Truncate task (content) to 60 chars
        task_truncated = (row["content"] or "")[:60]
        lines.append(f"  [{row['id'][:8]}] from={from_agent} task={task_truncated} created={row['created_at']}")

    return "\n".join(lines)

def memory_inbox_ack_impl(memory_id: str) -> str:
    """Marks a handoff memory as read."""
    # 1. Compute current timestamp
    now = datetime.now(timezone.utc).isoformat()

    # 2. Update read_at and updated_at
    with _db() as db:
        db.execute(
            "UPDATE memory_items SET read_at = ?, updated_at = ? WHERE id = ? AND type = 'handoff' AND is_deleted = 0",
            (now, now, memory_id)
        )

        # Verify update actually happened
        verify = db.execute(
            "SELECT id FROM memory_items WHERE id = ? AND type = 'handoff' AND is_deleted = 0 AND read_at IS NOT NULL",
            (memory_id,)
        ).fetchone()

    # 3. Check result
    if not verify:
        return f"Error: memory {memory_id} not found or not a handoff"

    # 4. Record history and return
    _record_history(memory_id, "handoff_ack", None, now, "read_at", "")
    return f"Acked: {memory_id}"

def _count_refresh_backlog(agent_id: str = "") -> int:
    """Cheap count of memories whose refresh_on has arrived. Used by lifecycle
    hooks (agent_register / agent_offline) and maintenance to surface the
    backlog without expanding the full list. Backed by the partial index
    idx_mi_refresh_on, so this is O(index-size-of-flagged-rows).
    """
    now = datetime.now(timezone.utc).isoformat()
    where = ["is_deleted = 0", "refresh_on IS NOT NULL", "refresh_on <= ?"]
    params: list = [now]
    if agent_id:
        where.append("agent_id = ?")
        params.append(agent_id)
    try:
        with _db() as db:
            row = db.execute(
                f"SELECT COUNT(*) FROM memory_items WHERE {' AND '.join(where)}",
                params,
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        # refresh_on column may not exist on an un-migrated DB — fail quiet.
        return 0

def _refresh_hint(agent_id: str = "") -> str:
    """One-line hint suitable for appending to lifecycle response strings.
    Returns empty string when there is no backlog, so callers can concatenate
    unconditionally.
    """
    n = _count_refresh_backlog(agent_id)
    if n <= 0:
        return ""
    noun = "memory" if n == 1 else "memories"
    scope = "of yours" if agent_id else "in the store"
    return f" | {n} {noun} {scope} due for refresh (see memory_refresh_queue)"

def memory_refresh_queue_impl(agent_id: str = "", limit: int = 50, include_future: bool = False) -> str:
    """Lists memories whose refresh_on timestamp has arrived (or all with refresh_on set
    if include_future=True). Read-only — actual refresh goes through memory_update.

    Surfaces memories flagged for periodic review via the refresh_on lifecycle.
    Scope to an agent with agent_id, or leave empty to see everything.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 500))

    where = ["is_deleted = 0", "refresh_on IS NOT NULL"]
    params: list = []
    if not include_future:
        where.append("refresh_on <= ?")
        params.append(now)
    if agent_id:
        where.append("agent_id = ?")
        params.append(agent_id)
    where_sql = " AND ".join(where)

    with _db() as db:
        rows = db.execute(
            f"SELECT id, type, title, refresh_on, refresh_reason, agent_id, updated_at "
            f"FROM memory_items WHERE {where_sql} ORDER BY refresh_on ASC LIMIT ?",
            (*params, limit)
        ).fetchall()

    if not rows:
        scope_label = f" for {agent_id}" if agent_id else ""
        when = "with refresh_on set" if include_future else "due for refresh"
        return f"Refresh queue{scope_label}: (empty — no memories {when})"

    scope_label = f" for {agent_id}" if agent_id else ""
    lines = [f"Refresh queue{scope_label} ({len(rows)} item{'s' if len(rows) != 1 else ''}):"]
    for row in rows:
        title = (row["title"] or "")[:60]
        reason = row["refresh_reason"] or "(no reason)"
        lines.append(
            f"  [{row['id'][:8]}] {row['type']:<12} due={row['refresh_on']} "
            f"reason={reason} title={title}"
        )
    return "\n".join(lines)

async def conversation_start_impl(title, agent_id="", model_id="", tags=""):
    cid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    metadata = json.dumps({"tags": [t.strip() for t in tags.split(",") if t.strip()]}) if tags else "{}"
    with _db() as db:
        db.execute(
            "INSERT INTO memory_items (id, type, title, agent_id, model_id, metadata_json, created_at) VALUES (?, 'conversation', ?, ?, ?, ?, ?)",
            (cid, title, agent_id, model_id, metadata, now)
        )
    return f"Conversation started: {cid}"

async def conversation_append_impl(conversation_id, role, content, agent_id="", model_id="", embed=True):
    with _db() as db:
        exists = db.execute(
            "SELECT id FROM memory_items WHERE id = ? AND type = 'conversation' AND is_deleted = 0",
            (conversation_id,)
        ).fetchone()
    if not exists:
        return f"Error: conversation {conversation_id} not found"
    mid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _db() as db:
        db.execute(
            "INSERT INTO memory_items (id, type, title, content, agent_id, model_id, created_at) VALUES (?, 'message', ?, ?, ?, ?, ?)",
            (mid, role, content, agent_id, model_id, now)
        )
        db.execute("INSERT INTO memory_relationships (id, from_id, to_id, relationship_type, created_at) VALUES (?, ?, ?, 'message', ?)",
                   (str(uuid.uuid4()), conversation_id, mid, now))
    if embed:
        vec, m = await _embed(content)
        if vec:
            with _db() as db:
                db.execute("INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim, created_at) VALUES (?,?,?,?,?,?)",
                          (str(uuid.uuid4()), mid, _pack(vec), m, len(vec), now))
    return f"Appended: {mid}"


def observation_enqueue_impl(
    conversation_id: str,
    user_id: str = "",
) -> str:
    """Phase D Mastra Observer enqueue.

    Inserts a row into observation_queue keyed on conversation_id. The
    drainer (bin/run_observer.py) pops these rows, builds the multi-turn
    JSON block from memory_items rows belonging to the conversation, calls
    the Observer SLM, and writes type='observation' rows back.

    UNIQUE on conversation_id means re-enqueue is a no-op — useful for
    idempotent close-of-conversation triggers.

    Returns "Enqueued" / "Already queued" / error string.
    """
    if not conversation_id:
        return "Error: conversation_id required"
    try:
        with _db() as db:
            db.execute(
                "INSERT OR IGNORE INTO observation_queue (conversation_id, user_id) "
                "VALUES (?, ?)",
                (conversation_id, user_id or None),
            )
            db.commit()
            row = db.execute(
                "SELECT id, attempts FROM observation_queue WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        if row:
            return f"Enqueued (queue_id={row[0]}, attempts={row[1]})"
        return "Error: enqueue failed silently"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


def reflector_enqueue_impl(
    conversation_id: str,
    user_id: str = "",
    obs_count: int | None = None,
) -> str:
    """Phase D Reflector enqueue.

    Triggered when the per-(user_id, conversation_id) observation count
    exceeds M3_REFLECTOR_THRESHOLD (default 50, env-tunable). Drained by
    bin/run_reflector.py.
    """
    if not conversation_id:
        return "Error: conversation_id required"
    try:
        with _db() as db:
            db.execute(
                "INSERT OR IGNORE INTO reflector_queue "
                "(conversation_id, user_id, obs_count_at_enqueue) VALUES (?, ?, ?)",
                (conversation_id, user_id or None, obs_count),
            )
            db.commit()
            row = db.execute(
                "SELECT id, attempts FROM reflector_queue "
                "WHERE conversation_id=? AND COALESCE(user_id,'')=COALESCE(?,'')",
                (conversation_id, user_id or None),
            ).fetchone()
        if row:
            return f"Enqueued (queue_id={row[0]}, attempts={row[1]})"
        return "Error: enqueue failed silently"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


VALID_SCOPES = {"user", "session", "agent", "org"}

async def memory_write_impl(type, content, title="", metadata="{}", agent_id="", model_id="", change_agent="", importance=0.5, source="agent", embed=True, user_id="", scope="agent", valid_from="", valid_to="", auto_classify=False, conversation_id="", refresh_on="", refresh_reason="", variant=None, embed_text=None, fact_enricher: "Callable[[str], Awaitable[list[dict]]] | None" = None, fact_enricher_variant_allowlist: "set[str] | None" = None, entity_extractor: "Callable[[str], Awaitable[dict]] | None" = None, entity_extractor_variant_allowlist: "set[str] | None" = None):
    """Internal implementation for memory_write. Contradiction detection is automatic.

    `variant` tags the item with a free-form ingestion-pipeline identifier so
    multiple variants (e.g. "baseline", "heuristic_c1c4", "llm_v1") can coexist
    and be compared. Default None = untagged.

    `embed_text` overrides the default text fed to the embedder (which is
    `content or title`). Useful when callers want to enrich the embedding with
    titles/entities without polluting the displayed content.

    `fact_enricher` is an optional async callable that extracts facts from content.
    `fact_enricher_variant_allowlist` controls which variants get enriched (default:
    None means skip all variants).
    """
    if isinstance(metadata, dict):
        metadata = json.dumps(metadata)
    elif not isinstance(metadata, str):
        metadata = "{}"
    _track_cost("write_calls")

    if auto_classify and (not type or type == "auto"):
        type = await _auto_classify(content, title)

    # Leak gate: reject `window:*` summary rows when the variant is NULL.
    # See bulk-write impl for the same gate + history (task #189, memory
    # 372f49b0). Mirrored here for the singleton path so misconfigured
    # bench callers who write items individually don't slip through.
    if (
        type == "summary"
        and isinstance(title, str)
        and title.startswith("window:")
        and not variant
    ):
        return (
            "Error: window:* summary rows require an explicit variant "
            "(rejected to prevent core-memory leak; see task #189)."
        )

    # Defense-in-depth content size check (primary validation is in memory_bridge.py)
    if content and len(content) > 50_000:
        return f"Error: content too large ({len(content)} chars, max 50000)"
    safety_err = _check_content_safety(content)
    if safety_err:
        return safety_err
    if scope not in VALID_SCOPES:
        scope = "agent"
    item_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    try:
        importance = float(importance)
    except (TypeError, ValueError):
        importance = 0.5
    agent = change_agent.strip().lower() or _infer_change_agent_util(agent_id, model_id, default=DEFAULT_CHANGE_AGENT)

    # Session-scoped memories auto-expire in 24 hours
    expires_at = None
    if scope == "session":
        from datetime import timedelta
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()

    # Opt-in ingest-time enrichment (env-gated, fail-open).
    title = await _maybe_auto_title(content or "", title)
    title = _augment_title_with_role(title, metadata)
    if _ingest_llm_enabled("M3_INGEST_AUTO_ENTITIES"):
        ents = await _maybe_auto_entities(content or "")
        if ents:
            try:
                meta_dict = json.loads(metadata) if isinstance(metadata, str) else (metadata or {})
            except json.JSONDecodeError:
                meta_dict = {}
            if isinstance(meta_dict, dict) and "entities" not in meta_dict:
                meta_dict["entities"] = ents
                metadata = json.dumps(meta_dict)

    with _db() as db:
        _vf = valid_from or now
        # Canonicalize "open-ended validity" as NULL, not "". The as_of range
        # predicate in memory_search_scored_impl historically had to allow both
        # NULL and "" because the single-write path stored "" while the bulk
        # path stored either; normalizing at write time lets future read paths
        # rely on NULL alone without carrying that compat clause forever.
        _vt = valid_to or None
        _cid = conversation_id or None
        _ron = refresh_on or None
        _rreason = refresh_reason or None
        # Same story for variant — MCP schema default is "" but search filters
        # untagged rows with `variant IS NULL`.
        _variant = variant or None
        db.execute(
            "INSERT INTO memory_items (id, type, title, content, metadata_json, agent_id, model_id, change_agent, importance, source, origin_device, user_id, scope, expires_at, created_at, valid_from, valid_to, conversation_id, refresh_on, refresh_reason, variant) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, type, title, content, metadata, agent_id, model_id, agent, importance, source, ORIGIN_DEVICE, user_id, scope, expires_at, now, _vf, _vt, _cid, _ron, _rreason, _variant)
        )
        # NOTE: chroma_sync_queue insert moved below into the `if vec:` block
        # so embed failures don't leave orphan queue rows.
        db.execute("UPDATE memory_items SET content_hash = ? WHERE id = ?",
                   (hashlib.sha256((content or "").encode("utf-8")).hexdigest(), item_id))

    vec = None
    if embed:
        _et = _augment_embed_text_with_anchors(
            embed_text or content or title, metadata
        )
        vec, m = await _embed(_et)
        if vec:
            with _db() as db:
                db.execute(
                    "INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim, created_at, content_hash) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), item_id, _pack(vec), m, len(vec), now, _content_hash(_et))
                )
                db.execute(
                    "INSERT INTO chroma_sync_queue (memory_id, operation) VALUES (?,?)",
                    (item_id, "upsert"),
                )
        else:
            logger.warning(
                f"memory_write_impl: embed failed for {item_id}; "
                f"skipping memory_embeddings + chroma_sync_queue insert"
            )

    _record_history(item_id, "create", None, content, "content", agent_id or agent)

    # Fact enrichment (Phase 4). Non-blocking: tries semaphore, enqueues on miss.
    # Always succeeds — verbatim row is already persisted before enrichment.
    try:
        with _db() as db:
            await _try_enrich_or_enqueue(item_id, content or "", fact_enricher, db, variant=variant, allowlist=fact_enricher_variant_allowlist)
    except Exception as e:
        logger.debug(f"fact enrichment dispatch failed: {e}")

    # Entity extraction (Phase 4). Non-blocking: tries semaphore, enqueues on miss.
    # fact_enriched rows are NOT extracted to prevent recursion.
    if type != "fact_enriched":
        try:
            with _db() as db:
                await _try_extract_or_enqueue(
                    item_id, content or "", entity_extractor, db,
                    variant=variant, allowlist=entity_extractor_variant_allowlist,
                )
        except Exception as e:
            logger.debug(f"entity extraction dispatch failed: {e}")

    # Contradiction detection + auto-linking (runs after embedding is stored)
    superseded_ids = []
    if vec and type not in ("conversation", "message"):
        superseded_ids, related_candidates = await _check_contradictions(item_id, content, title, vec, type, agent_id)
        # Auto-link top related (non-contradictory) memory
        if related_candidates and not superseded_ids:
            best_id, best_score = related_candidates[0]
            try:
                memory_link_impl(item_id, best_id, "related")
                logger.debug(f"Auto-linked {item_id} -> {best_id} (score={best_score:.3f})")
            except Exception:
                pass

    # Opt-in ingestion emitters. Each one is gated off by default and fails
    # open — errors are logged but never propagate to the caller. They only
    # fire for 'message' rows; other types (facts, notes, etc.) are skipped
    # since windowing/gist/event-extraction are conversation-shaped features.
    if type == "message" and _cid:
        try:
            if INGEST_EVENT_ROWS:
                await _maybe_emit_event_rows(
                    content or "", metadata, _cid, user_id, item_id
                )
            if INGEST_WINDOW_CHUNKS:
                await _maybe_emit_window_chunk(_cid, user_id)
            if INGEST_GIST_ROWS:
                await _maybe_emit_gist_row(_cid, user_id)
        except Exception as e:
            logger.debug(f"ingest emitter failed: {e}")

    result = f"Created: {item_id}"
    if superseded_ids:
        result += f" (superseded {len(superseded_ids)} conflicting memories: {', '.join(superseded_ids[:3])})"
    return result


async def memory_write_from_file_impl(
    path: str,
    type: str,
    title: str = "",
    metadata: str = "{}",
    agent_id: str = "",
    model_id: str = "",
    change_agent: str = "",
    importance: float = 0.5,
    source: str = "agent",
    embed: bool = True,
    user_id: str = "",
    scope: str = "agent",
    valid_from: str = "",
    valid_to: str = "",
    auto_classify: bool = False,
    conversation_id: str = "",
    refresh_on: str = "",
    refresh_reason: str = "",
    variant: str | None = None,
    delete_after_read: bool = True,
):
    """Write a memory whose `content` is read from a file on disk.

    Bypasses the LLM-streaming bottleneck for large memory writes: when the
    LLM authors a multi-thousand-token markdown body inline in a tool_use,
    the autoregressive decode time of streaming the JSON `input` field
    dominates the wall-clock (24-90s typical). Writing to a file with the
    Write tool is off the streaming path; the resulting tool_use here only
    needs to stream a path string + a few short metadata fields.

    `path` must be an absolute path on the host where this MCP server
    runs. The file is read once, contents become the memory `content`,
    and (by default) the file is deleted on success — keeping the temp
    directory clean and signalling that the contents are now authoritative
    in m3-memory, not on disk.

    Read errors / missing files return a string "Error: ..." mirroring
    the singleton path's contract. The underlying memory_write_impl is
    called unchanged with the read content, so all existing gates
    (content-safety, leak-gate, scope, contradiction detection,
    auto-classify, etc.) apply identically.

    Reference: bench / diagnostic data in
    `.scratch/memory_latency_diagnostic.md` — Phase K rationale.
    """
    if not path:
        return "Error: path is required"
    p = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(p):
        return f"Error: file not found: {p}"
    if not os.path.isfile(p):
        return f"Error: not a file: {p}"
    try:
        size = os.path.getsize(p)
    except OSError as e:
        return f"Error: cannot stat file: {type(e).__name__}: {e}"
    # Defense-in-depth size check — memory_write_impl will also enforce
    # 50_000-char limit on content, but we should fail fast before reading
    # a multi-megabyte file off disk.
    if size > 200_000:
        return f"Error: file too large ({size} bytes; max 200000 for memory_write_from_file)"

    try:
        with open(p, "r", encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return f"Error: cannot read file: {type(e).__name__}: {e}"

    # Delegate to the canonical singleton path. Every gate applies to the
    # disk-read content the same way it applies to inline content.
    result = await memory_write_impl(
        type=type,
        content=content,
        title=title,
        metadata=metadata,
        agent_id=agent_id,
        model_id=model_id,
        change_agent=change_agent,
        importance=importance,
        source=source,
        embed=embed,
        user_id=user_id,
        scope=scope,
        valid_from=valid_from,
        valid_to=valid_to,
        auto_classify=auto_classify,
        conversation_id=conversation_id,
        refresh_on=refresh_on,
        refresh_reason=refresh_reason,
        variant=variant,
    )

    # Only delete the source file if memory_write_impl actually wrote a
    # row (success messages start with "Created:"). On error, leave the
    # file in place so the caller can inspect it.
    if delete_after_read and isinstance(result, str) and result.startswith("Created:"):
        try:
            os.unlink(p)
        except OSError as e:
            # Non-fatal — the row landed; we just couldn't clean up the temp.
            logger.warning(f"memory_write_from_file: row written but file unlink failed: {e}")
            return result + f" (warning: could not delete source file {p}: {e})"

    return result


async def memory_write_batch_impl(items: list[dict]):
    """
    Speed Optimization: Parallelized batch memory write (Speed #1).
    Expects list of dicts with keys matching memory_write_impl args.
    """
    results = []
    # 1. First pass: Insert metadata in one transaction
    now = datetime.now(timezone.utc).isoformat()

    write_tasks = []
    for item in items:
        mid = str(uuid.uuid4())
        agent = item.get("change_agent", "").strip().lower() or _infer_change_agent_util(item.get("agent_id", ""), item.get("model_id", ""), default=DEFAULT_CHANGE_AGENT)

        with _db() as db:
            db.execute(
                "INSERT INTO memory_items (id, type, title, content, metadata_json, agent_id, model_id, change_agent, importance, source, origin_device, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (mid, item["type"], item.get("title", ""), item["content"], item.get("metadata", "{}"),
                 item.get("agent_id", ""), item.get("model_id", ""), agent, item.get("importance", 0.5),
                 item.get("source", "agent"), ORIGIN_DEVICE, now)
            )
            # NOTE: chroma_sync_queue insert moved to Phase 2 below so embed
            # failures don't leave orphan queue rows.

        if item.get("embed", True):
            # Queue for parallel embedding (gather)
            write_tasks.append((mid, item.get("content") or item.get("title")))
        results.append(mid)

    # 2. Parallelize embedding generation (Speed Optimization #1)
    # Bounded by _EMBED_SEM to prevent LM Studio overload
    async def _bounded_embed(text):
        async with _EMBED_SEM:
            return await _embed(text)

    if write_tasks:
        embed_jobs = [_bounded_embed(text) for _, text in write_tasks]
        try:
            embeddings = await asyncio.wait_for(
                asyncio.gather(*embed_jobs, return_exceptions=True),
                timeout=120.0
            )
        except asyncio.TimeoutError:
            logger.error(f"Batch embedding timed out after 120s for {len(write_tasks)} items")
            embeddings = [None] * len(write_tasks)

        with _db() as db:
            for (mid, text), result in zip(write_tasks, embeddings):
                if isinstance(result, Exception):
                    logger.warning(f"Batch embed failed for {mid}: {result}; skipping chroma_sync_queue insert")
                    continue
                if result is None:
                    logger.warning(f"Batch embed returned None for {mid}; skipping chroma_sync_queue insert")
                    continue
                vec, m = result
                if vec:
                    db.execute(
                        "INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim, created_at, content_hash) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), mid, _pack(vec), m, len(vec), now, _content_hash(text))
                    )
                    db.execute(
                        "INSERT INTO chroma_sync_queue (memory_id, operation) VALUES (?,?)",
                        (mid, "upsert"),
                    )
                else:
                    logger.warning(f"Batch embed empty vec for {mid}; skipping chroma_sync_queue insert")

    return f"Batch created: {len(results)} items"

# ── Task Orchestration: State Machine + Helper Functions ─────────────────────────

TASK_STATE_TRANSITIONS = {
    "pending":     {"in_progress", "blocked", "cancelled"},
    "in_progress": {"blocked", "completed", "failed", "cancelled"},
    "blocked":     {"in_progress", "cancelled"},
    "completed":   set(),
    "failed":      set(),
    "cancelled":   set(),
}
VALID_TASK_STATES = frozenset(TASK_STATE_TRANSITIONS.keys())
TERMINAL_TASK_STATES = frozenset({"completed", "failed", "cancelled"})
VALID_AGENT_STATUSES = frozenset({"active", "idle", "offline"})

def _validate_task_transition(prev: str, new: str):
    """Validates task state transitions. Returns None if valid, error string if invalid."""
    if new not in VALID_TASK_STATES:
        return f"Error: invalid task state '{new}'. Valid: {', '.join(sorted(VALID_TASK_STATES))}"
    if prev == new:
        return None
    allowed = TASK_STATE_TRANSITIONS.get(prev, set())
    if new not in allowed:
        return (f"Error: cannot transition task from '{prev}' to '{new}'. "
                f"Allowed from '{prev}': {sorted(allowed) or '(terminal)'}")
    return None

def _agent_exists(agent_id: str) -> bool:
    """Checks if an agent is registered in the agents table."""
    with _db() as db:
        row = db.execute("SELECT 1 FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        return row is not None

# ── Agent Registry (5 functions) ──────────────────────────────────────────────────

def agent_register_impl(agent_id: str, role: str, capabilities: list, metadata: dict) -> str:
    """Registers or updates an agent in the registry."""
    if not agent_id:
        return "Error: agent_id cannot be empty"

    now = datetime.now(timezone.utc).isoformat()
    caps_json = json.dumps(capabilities or [])
    meta_json = json.dumps(metadata or {})

    with _db() as db:
        db.execute(
            """INSERT INTO agents (agent_id, role, capabilities, metadata_json, status, last_seen, created_at)
               VALUES (?, ?, ?, ?, 'active', ?, ?)
               ON CONFLICT(agent_id) DO UPDATE SET
                 role=excluded.role,
                 capabilities=excluded.capabilities,
                 metadata_json=excluded.metadata_json,
                 status='active',
                 last_seen=excluded.last_seen""",
            (agent_id, role, caps_json, meta_json, now, now)
        )

    return f"Registered: {agent_id} (role={role}, status=active)" + _refresh_hint(agent_id)

def agent_heartbeat_impl(agent_id: str) -> str:
    """Updates agent's last_seen timestamp and status to active."""
    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        cur = db.execute(
            "UPDATE agents SET last_seen = ?, status = 'active' WHERE agent_id = ?",
            (now, agent_id)
        )
        rowcount = cur.rowcount

    if rowcount == 0:
        return f"Error: agent '{agent_id}' not registered"

    return f"Heartbeat: {agent_id} (last_seen={now})"

def agent_list_impl(status: str = "", role: str = "") -> str:
    """Lists agents, optionally filtered by status and/or role."""
    where_clauses = []
    params = []

    if status:
        where_clauses.append("status = ?")
        params.append(status)
    if role:
        where_clauses.append("role = ?")
        params.append(role)

    where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    with _db() as db:
        rows = db.execute(
            f"SELECT agent_id, role, status, last_seen FROM agents {where} ORDER BY last_seen DESC",
            params
        ).fetchall()

    if not rows:
        return "(no agents)"

    lines = [f"Agents ({len(rows)}):"]
    for row in rows:
        lines.append(f"  [{row['agent_id']}] role={row['role']} status={row['status']} last_seen={row['last_seen']}")

    return "\n".join(lines)

def agent_get_impl(agent_id: str) -> str:
    """Retrieves detailed information about a single agent."""
    with _db() as db:
        row = db.execute(
            "SELECT * FROM agents WHERE agent_id = ?",
            (agent_id,)
        ).fetchone()

    if not row:
        return f"Error: agent '{agent_id}' not found"

    caps = json.loads(row["capabilities"] or "[]")
    meta = json.loads(row["metadata_json"] or "{}")

    lines = [
        f"Agent: {row['agent_id']}",
        f"  Role: {row['role']}",
        f"  Status: {row['status']}",
        f"  Capabilities: {caps}",
        f"  Metadata: {meta}",
        f"  Last Seen: {row['last_seen']}",
        f"  Created At: {row['created_at'] if 'created_at' in row.keys() else 'N/A'}",
    ]

    return "\n".join(lines)

def agent_offline_impl(agent_id: str) -> str:
    """Marks an agent as offline."""
    with _db() as db:
        cur = db.execute(
            "UPDATE agents SET status = 'offline' WHERE agent_id = ?",
            (agent_id,)
        )
        rowcount = cur.rowcount

    if rowcount == 0:
        return f"Error: agent '{agent_id}' not found"

    return f"Agent {agent_id} marked offline" + _refresh_hint(agent_id)

# ── Notifications (4 functions) ───────────────────────────────────────────────────

def notify_impl(agent_id: str, kind: str, payload: dict = None) -> str:
    """Sends a notification to an agent."""
    now = datetime.now(timezone.utc).isoformat()
    payload_json = json.dumps(payload or {})

    with _db() as db:
        db.execute(
            "INSERT INTO notifications (agent_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (agent_id, kind, payload_json, now)
        )
        # Get the ID of the newly inserted row
        new_id = db.execute(
            "SELECT last_insert_rowid() as id"
        ).fetchone()["id"]

    return f"Notified {agent_id}: {kind} (id={new_id})"

def notifications_poll_impl(agent_id: str, unread_only: bool = True, limit: int = 20) -> str:
    """Retrieves notifications for an agent."""
    where_clause = "WHERE agent_id = ?"
    params = [agent_id]

    if unread_only:
        where_clause += " AND read_at IS NULL"

    with _db() as db:
        rows = db.execute(
            f"SELECT id, kind, payload_json, created_at, read_at FROM notifications {where_clause} ORDER BY created_at DESC LIMIT ?",
            params + [limit]
        ).fetchall()

    if not rows:
        return f"Notifications for {agent_id}: (empty)"

    read_type = "unread" if unread_only else "total"
    lines = [f"Notifications for {agent_id} ({len(rows)} {read_type}):"]
    for row in rows:
        lines.append(f"  [{row['id']}] kind={row['kind']} payload={row['payload_json']} created={row['created_at']}")

    return "\n".join(lines)

def notifications_ack_impl(notification_id: int) -> str:
    """Marks a notification as read."""
    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        cur = db.execute(
            "UPDATE notifications SET read_at = ? WHERE id = ? AND read_at IS NULL",
            (now, notification_id)
        )
        rowcount = cur.rowcount

    if rowcount == 0:
        return f"Error: notification {notification_id} not found or already acked"

    return f"Acked notification {notification_id}"

def notifications_ack_all_impl(agent_id: str) -> str:
    """Marks all unread notifications for an agent as read."""
    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        cur = db.execute(
            "UPDATE notifications SET read_at = ? WHERE agent_id = ? AND read_at IS NULL",
            (now, agent_id)
        )
        rowcount = cur.rowcount

    return f"Acked {rowcount} notifications for {agent_id}"

# ── Tasks (7 functions) ───────────────────────────────────────────────────────────

def task_create_impl(title: str, created_by: str, description: str = "", owner_agent: str = "", parent_task_id: str = "", metadata: dict = None) -> str:
    """Creates a new task."""
    if not title:
        return "Error: title cannot be empty"
    if not created_by:
        return "Error: created_by cannot be empty"

    task_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        db.execute(
            """INSERT INTO tasks (id, title, description, state, created_by, owner_agent, parent_task_id, metadata_json, created_at, updated_at)
               VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)""",
            (task_id, title, description, created_by, owner_agent or None, parent_task_id or None, json.dumps(metadata or {}), now, now)
        )

    return f"Task created: {task_id}"

def task_assign_impl(task_id: str, owner_agent: str) -> str:
    """Assigns a task to an agent and transitions state to in_progress."""
    with _db() as db:
        row = db.execute(
            "SELECT state, created_by FROM tasks WHERE id = ? AND deleted_at IS NULL",
            (task_id,)
        ).fetchone()

    if not row:
        return f"Error: task '{task_id}' not found"

    prev_state = row["state"]
    err = _validate_task_transition(prev_state, "in_progress")
    if err:
        return err

    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        db.execute(
            "UPDATE tasks SET owner_agent = ?, state = 'in_progress', updated_at = ? WHERE id = ?",
            (owner_agent, now, task_id)
        )

    _record_history(task_id, "task_state", prev_state, "in_progress", "state", owner_agent)

    # Fire-and-forget notification
    try:
        notify_impl(owner_agent, "task_assigned", {"task_id": task_id})
    except Exception as e:
        logger.warning(f"task_assigned notify failed for {owner_agent}: {e}")

    return f"Task {task_id} assigned to {owner_agent} (state=in_progress)"

def task_update_impl(task_id: str, state: str = "", description: str = "", metadata: dict = None, actor: str = "") -> str:
    """Updates a task's state, description, and/or metadata."""
    with _db() as db:
        row = db.execute(
            "SELECT state, description, metadata_json, created_by FROM tasks WHERE id = ? AND deleted_at IS NULL",
            (task_id,)
        ).fetchone()

    if not row:
        return f"Error: task '{task_id}' not found"

    prev_state = row["state"]
    new_state = state if state else prev_state

    if state:
        err = _validate_task_transition(prev_state, new_state)
        if err:
            return err

    now = datetime.now(timezone.utc).isoformat()
    updates = ["updated_at = ?"]
    params = [now]

    if state:
        updates.append("state = ?")
        params.append(new_state)

    if description:
        updates.append("description = ?")
        params.append(description)

    if metadata is not None:
        updates.append("metadata_json = ?")
        params.append(json.dumps(metadata))

    if new_state in TERMINAL_TASK_STATES:
        updates.append("completed_at = ?")
        params.append(now)

    params.append(task_id)

    with _db() as db:
        db.execute(
            f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?",
            params
        )

    if state and prev_state != new_state:
        _record_history(task_id, "task_state", prev_state, new_state, "state", actor or "system")

        # Fire-and-forget notification if completed
        if new_state == "completed":
            try:
                notify_impl(row["created_by"], "task_completed", {"task_id": task_id})
            except Exception as e:
                logger.warning(f"task_completed notify failed for {row['created_by']}: {e}")

        return f"Task {task_id} updated: state={new_state}"
    else:
        return f"Task {task_id} updated"

def task_set_result_impl(task_id: str, result_memory_id: str) -> str:
    """Sets the result memory for a task (without changing state)."""
    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        cur = db.execute(
            "UPDATE tasks SET result_memory_id = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (result_memory_id, now, task_id)
        )
        rowcount = cur.rowcount

    if rowcount == 0:
        return f"Error: task '{task_id}' not found"

    return f"Task {task_id} result={result_memory_id}"

def task_get_impl(task_id: str, include_deleted: bool = False) -> str:
    """Retrieves detailed information about a task."""
    sql = "SELECT * FROM tasks WHERE id = ?"
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    with _db() as db:
        row = db.execute(sql, (task_id,)).fetchone()

    if not row:
        return f"Error: task '{task_id}' not found"

    lines = [
        f"Task: {row['id']}",
        f"  Title: {row['title']}",
        f"  Description: {row['description']}",
        f"  State: {row['state']}",
        f"  Created By: {row['created_by']}",
        f"  Owner: {row['owner_agent'] or '(unassigned)'}",
        f"  Parent Task: {row['parent_task_id'] or '(none)'}",
        f"  Result Memory: {row['result_memory_id'] or '(none)'}",
        f"  Created At: {row['created_at']}",
        f"  Updated At: {row['updated_at']}",
        f"  Completed At: {row['completed_at'] or '(not completed)'}",
        f"  Deleted At: {row['deleted_at'] or '(not deleted)'}",
    ]

    return "\n".join(lines)

def task_delete_impl(task_id: str, hard: bool = False, actor: str = "") -> str:
    """Delete a task.

    Soft-delete (default): sets `deleted_at` so pg_sync propagates the
    tombstone to the warehouse and peers on the next run. The row stays
    in local SQLite and is filtered out of reads.

    Hard-delete: only allowed once the row is already tombstoned. Removes
    the row from local SQLite. Note that sync is UPSERT-only, so a hard
    delete on one peer does NOT remove the row on other peers — they
    converge via the soft-delete tombstone.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _db() as db:
        row = db.execute(
            "SELECT state, deleted_at FROM tasks WHERE id = ?",
            (task_id,)
        ).fetchone()

        if not row:
            return f"Error: task '{task_id}' not found"

        if hard:
            if row["deleted_at"] is None:
                return (
                    f"Error: task '{task_id}' must be soft-deleted before hard-delete. "
                    "Call task_delete with hard=False first."
                )
            db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            _record_history(task_id, "task_deleted", row["state"], "hard_deleted", "deleted_at", actor or "system")
            return f"Task {task_id} hard-deleted"

        if row["deleted_at"] is not None:
            return f"Task {task_id} already soft-deleted at {row['deleted_at']}"

        db.execute(
            "UPDATE tasks SET deleted_at = ?, updated_at = ? WHERE id = ?",
            (now, now, task_id)
        )

    _record_history(task_id, "task_deleted", row["state"], "soft_deleted", "deleted_at", actor or "system")
    return f"Task {task_id} soft-deleted (tombstone will sync on next pg_sync run)"

def task_list_impl(owner_agent: str = "", state: str = "", parent_task_id: str = "", limit: int = 20, include_deleted: bool = False) -> str:
    """Lists tasks, optionally filtered by owner, state, and/or parent."""
    where_clauses = []
    params = []

    if not include_deleted:
        where_clauses.append("deleted_at IS NULL")
    if owner_agent:
        where_clauses.append("owner_agent = ?")
        params.append(owner_agent)
    if state:
        where_clauses.append("state = ?")
        params.append(state)
    if parent_task_id:
        where_clauses.append("parent_task_id = ?")
        params.append(parent_task_id)

    where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    with _db() as db:
        rows = db.execute(
            f"SELECT id, title, state, owner_agent FROM tasks {where} ORDER BY updated_at DESC LIMIT ?",
            params + [limit]
        ).fetchall()

    if not rows:
        return "Tasks: (empty)"

    lines = [f"Tasks ({len(rows)}):"]
    for row in rows:
        lines.append(f"  [{row['id'][:8]}] {row['title']} state={row['state']} owner={row['owner_agent']}")

    return "\n".join(lines)

def task_tree_impl(root_task_id: str, max_depth: int = 10) -> str:
    """Displays a task and its subtasks in a tree structure. Tombstoned tasks are hidden."""
    max_depth = max(1, min(max_depth, 20))

    with _db() as db:
        row = db.execute(
            "SELECT id, title, state, owner_agent FROM tasks WHERE id = ? AND deleted_at IS NULL",
            (root_task_id,)
        ).fetchone()

        if not row:
            return f"Error: task '{root_task_id}' not found"

        rows = db.execute(
            """WITH RECURSIVE subtree(id, title, state, owner_agent, parent_task_id, depth) AS (
                SELECT id, title, state, owner_agent, parent_task_id, 0
                  FROM tasks WHERE id = ? AND deleted_at IS NULL
                UNION ALL
                SELECT t.id, t.title, t.state, t.owner_agent, t.parent_task_id, s.depth + 1
                  FROM tasks t JOIN subtree s ON t.parent_task_id = s.id
                 WHERE s.depth + 1 <= ? AND t.deleted_at IS NULL
            )
            SELECT * FROM subtree ORDER BY depth, id""",
            (root_task_id, max_depth)
        ).fetchall()

    if not rows:
        return f"Error: task '{root_task_id}' not found"

    lines = [f"Task tree from {root_task_id[:8]} (max_depth={max_depth}):"]
    for row in rows:
        indent = "  " * row["depth"]
        owner_str = row["owner_agent"] or "-"
        lines.append(f"{indent}[{row['id'][:8]}] {row['title']} ({row['state']}, owner={owner_str})")

    return "\n".join(lines)


# ── Fact enrichment queue drain (Phase 5) ────────────────────────────────────
def _select_pending_fact_enrichment(db, limit: int | None = None, allowed_variants: list[str] | None = None) -> list[tuple[str, str]]:
    """Returns [(memory_id, content), ...] eligible for enrichment.

    Eligibility: type != fact_enriched, variant IS NULL (or in allowed_variants),
    no existing fact_enriched child via references edge, attempts < max_attempts.

    When allowed_variants is provided, loosen the variant filter from strict NULL
    to (variant IS NULL OR variant IN (...)).
    """
    # Build the variant clause
    if allowed_variants:
        variant_clause = f"AND (mi.variant IS NULL OR mi.variant IN ({','.join(['?'] * len(allowed_variants))}))"
        variant_params = list(allowed_variants)
    else:
        variant_clause = "AND mi.variant IS NULL"
        variant_params = []

    sql = f"""
    WITH eligible AS (
        SELECT mi.id, mi.content
        FROM memory_items mi
        WHERE mi.type != 'fact_enriched'
          AND COALESCE(mi.is_deleted, 0) = 0
          {variant_clause}
          AND NOT EXISTS (
              SELECT 1 FROM memory_relationships mr
              JOIN memory_items child ON child.id = mr.from_id
              WHERE mr.to_id = mi.id
                AND mr.relationship_type = 'references'
                AND child.type = 'fact_enriched'
          )
    ),
    queued AS (
        SELECT mi.id, mi.content, q.attempts
        FROM fact_enrichment_queue q
        JOIN memory_items mi ON mi.id = q.memory_id
        WHERE q.attempts < ?
    )
    SELECT id, content FROM queued
    UNION
    SELECT id, content FROM eligible
    WHERE id NOT IN (SELECT memory_id FROM fact_enrichment_queue)
    ORDER BY id
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    params = variant_params + [FACT_ENRICH_MAX_ATTEMPTS]
    return list(db.execute(sql, params).fetchall())


async def enrich_pending_impl(dry_run: bool = True, limit: int = 0, allowed_variants: list[str] | None = None) -> dict:
    """Enrich pending memory items. Dry-run reports count + ETA; execute drains queue.

    Returns:
    - dry_run=True: {"count": N, "est_wall_clock_seconds": F, "sample_ids": [...]}
    - dry_run=False: {"processed": N, "succeeded": N, "failed": N, "errors_summary": str}
    """
    with _db() as db:
        pending = _select_pending_fact_enrichment(db, limit=limit, allowed_variants=allowed_variants)

    if not pending:
        if dry_run:
            return {"count": 0, "est_wall_clock_seconds": 0.0, "sample_ids": []}
        else:
            return {"processed": 0, "succeeded": 0, "failed": 0, "errors_summary": "No pending items"}

    if dry_run:
        # Dry run: report count + ETA estimate (2.0 sec/item conservative default)
        est_secs = len(pending) * 2.0
        sample_ids = [mid for mid, _ in pending[:3]]
        return {
            "count": len(pending),
            "est_wall_clock_seconds": est_secs,
            "sample_ids": sample_ids,
        }

    # Execute: drain the queue using the semaphore
    # For execution, we'd need to have a fact_enricher available. Since this is the
    # core implementation and the enricher is passed at write time, we can't execute
    # here without the enricher. This function is typically called as an MCP tool
    # with an enricher injected. For now, return a placeholder indicating execution mode.
    # In Wave 3 (MCP tool), the caller will provide the enricher.
    return {
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
        "errors_summary": "Execution requires enricher (Wave 3 MCP tool)",
    }


# ── Entity extraction queue drain (Phase 5) ──────────────────────────────────
def _select_pending_entity_extraction(
    db,
    limit: int | None = None,
    allowed_variants: list[str] | None = None,
) -> list[tuple[str, str, str | None]]:
    """Returns [(memory_id, content, valid_from), ...] eligible for entity extraction.

    Eligibility:
      - type != 'fact_enriched'  (don't extract from derived rows)
      - COALESCE(is_deleted, 0) = 0  (NB: is_deleted, NOT deleted_at — avoids
        the deleted_at-vs-is_deleted confusion that burned us in fact_enriched)
      - variant IS NULL (or in allowed_variants when provided)
      - id NOT IN (SELECT memory_id FROM memory_item_entities)  — not already extracted
      - id NOT IN (SELECT memory_id FROM entity_extraction_queue
                   WHERE attempts >= ENTITY_EXTRACT_MAX_ATTEMPTS)  — poisoned-item guard

    valid_from is included so callers can inherit bitemporal validity from the source
    memory when creating entities and relationships during extraction.
    """
    if allowed_variants:
        variant_clause = (
            f"AND (mi.variant IS NULL OR mi.variant IN "
            f"({','.join(['?'] * len(allowed_variants))}))"
        )
        variant_params = list(allowed_variants)
    else:
        variant_clause = "AND mi.variant IS NULL"
        variant_params = []

    sql = f"""
    WITH eligible AS (
        SELECT mi.id, mi.content, mi.valid_from
        FROM memory_items mi
        WHERE mi.type != 'fact_enriched'
          AND COALESCE(mi.is_deleted, 0) = 0
          {variant_clause}
          AND mi.id NOT IN (SELECT DISTINCT memory_id FROM memory_item_entities)
    ),
    queued AS (
        SELECT mi.id, mi.content, mi.valid_from, q.attempts
        FROM entity_extraction_queue q
        JOIN memory_items mi ON mi.id = q.memory_id
        WHERE q.attempts < ?
    )
    SELECT id, content, valid_from FROM queued
    UNION
    SELECT id, content, valid_from FROM eligible
    WHERE id NOT IN (SELECT memory_id FROM entity_extraction_queue)
    ORDER BY id
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    params = variant_params + [ENTITY_EXTRACT_MAX_ATTEMPTS]
    return list(db.execute(sql, params).fetchall())


async def extract_pending_impl(
    dry_run: bool = True,
    limit: int = 0,
    allowed_variants: list[str] | None = None,
    *,
    valid_types: frozenset | None = None,       # None = use VALID_ENTITY_TYPES
    valid_predicates: frozenset | None = None,  # None = use VALID_ENTITY_PREDICATES
) -> dict:
    """Entity extraction queue drain. Mirrors enrich_pending_impl shape.

    Returns:
    - dry_run=True: {"count": N, "est_wall_clock_seconds": F, "sample_ids": [...]}
    - dry_run=False: {"processed": N, "succeeded": N, "failed": N,
                      "errors_summary": str, "entities_created": N,
                      "relationships_created": N}

    ETA estimate uses 3.0 sec/item (higher than fact_enriched's 2.0 because
    entity resolution adds DB lookups on top of SLM call).

    valid_types / valid_predicates are forwarded to _run_entity_extractor.
    None means use the module-level VALID_ENTITY_TYPES / VALID_ENTITY_PREDICATES constants.
    Bench harnesses can pass custom frozensets; production callers pass None (default behavior).

    Execute path: placeholder until Wave 3 injects entity_extractor via MCP tool.
    """
    with _db() as db:
        pending = _select_pending_entity_extraction(
            db,
            limit=limit if limit else None,
            allowed_variants=allowed_variants,
        )

    if not pending:
        if dry_run:
            return {"count": 0, "est_wall_clock_seconds": 0.0, "sample_ids": []}
        else:
            return {
                "processed": 0,
                "succeeded": 0,
                "failed": 0,
                "errors_summary": "No pending items",
                "entities_created": 0,
                "relationships_created": 0,
            }

    if dry_run:
        # Dry run: report count + ETA estimate (3.0 sec/item)
        est_secs = len(pending) * 3.0
        sample_ids = [row[0] for row in pending[:3]]
        return {
            "count": len(pending),
            "est_wall_clock_seconds": est_secs,
            "sample_ids": sample_ids,
        }

    # Execute: placeholder until Wave 3 wires entity_extractor injection.
    # The entity_extractor is provided at write-time through memory_write_impl;
    # a drain path that calls the extractor requires it to be injected by the
    # MCP tool layer (same pattern as enrich_pending_impl).
    # valid_types/valid_predicates are accepted here now so callers can pass them
    # when Wave 3 wires the real execution path.
    return {
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
        "errors_summary": "Execution requires entity_extractor (Wave 3 MCP tool)",
        "entities_created": 0,
        "relationships_created": 0,
    }

# ── Entity extractor health (Phase E1) ───────────────────────────────────────
def entity_extractor_health() -> dict:
    """Read-only diagnostic for the entity extraction pipeline.

    Returns a dict with 6 keys:
      queue_depth          — COUNT(*) from entity_extraction_queue where
                             attempts < ENTITY_EXTRACT_MAX_ATTEMPTS (eligible to retry)
      poisoned             — COUNT(*) where attempts >= ENTITY_EXTRACT_MAX_ATTEMPTS
                             (excluded from eligible set; kept for diagnostic visibility)
      last_extracted_at    — ISO-8601 string of the most recent entities.created_at,
                             or None if the entities table is empty
      entities_total       — total rows in entities table
      relationships_total  — total rows in entity_relationships table
      memory_item_entities_total — total rows in memory_item_entities table
    """
    with _db() as db:
        q_depth = db.execute(
            "SELECT COUNT(*) FROM entity_extraction_queue WHERE attempts < ?",
            (ENTITY_EXTRACT_MAX_ATTEMPTS,),
        ).fetchone()[0]

        poisoned = db.execute(
            "SELECT COUNT(*) FROM entity_extraction_queue WHERE attempts >= ?",
            (ENTITY_EXTRACT_MAX_ATTEMPTS,),
        ).fetchone()[0]

        last_row = db.execute(
            "SELECT MAX(created_at) AS last_at FROM entities"
        ).fetchone()
        last_extracted_at: str | None = last_row["last_at"] if last_row else None

        entities_total = db.execute(
            "SELECT COUNT(*) FROM entities"
        ).fetchone()[0]

        relationships_total = db.execute(
            "SELECT COUNT(*) FROM entity_relationships"
        ).fetchone()[0]

        mie_total = db.execute(
            "SELECT COUNT(*) FROM memory_item_entities"
        ).fetchone()[0]

    return {
        "queue_depth": q_depth,
        "poisoned": poisoned,
        "last_extracted_at": last_extracted_at,
        "entities_total": entities_total,
        "relationships_total": relationships_total,
        "memory_item_entities_total": mie_total,
    }


# ── Entity search and retrieval (Phase 7) ─────────────────────────────────────
def entity_search_impl(
    query: str = "",
    entity_type: str = "",
    limit: int = 10,
    with_neighbors: bool = False,
) -> list[dict]:
    """Search the entities table by canonical_name and optionally by entity_type.

    Returns:
        List of dicts: [{entity_id, canonical_name, entity_type, attributes_json, neighbor_count}]
        neighbor_count only computed if with_neighbors=True.
    """
    with _db() as db:
        # Build the WHERE clause
        where_parts = []
        params = []

        if query:
            # LIKE %query% on canonical_name (case-insensitive)
            where_parts.append("canonical_name LIKE ?")
            params.append(f"%{query}%")

        if entity_type:
            where_parts.append("entity_type = ?")
            params.append(entity_type)

        where_clause = " AND ".join(where_parts) if where_parts else "1=1"

        sql = f"""
        SELECT id, canonical_name, entity_type, attributes_json
        FROM entities
        WHERE {where_clause}
        ORDER BY canonical_name
        LIMIT ?
        """
        params.append(limit)

        rows = list(db.execute(sql, params).fetchall())

        result = []
        for entity_id, canonical_name, entity_type_val, attributes_json in rows:
            neighbor_count = 0
            if with_neighbors:
                # Count relationships where this entity is from_entity OR to_entity
                neighbor_sql = """
                SELECT COUNT(DISTINCT id)
                FROM entity_relationships
                WHERE from_entity = ? OR to_entity = ?
                """
                neighbor_count = db.execute(
                    neighbor_sql, (entity_id, entity_id)
                ).fetchone()[0]

            result.append({
                "entity_id": entity_id,
                "canonical_name": canonical_name,
                "entity_type": entity_type_val,
                "attributes_json": attributes_json or "{}",
                "neighbor_count": neighbor_count,
            })

        return result


def entity_get_impl(entity_id: str, depth: int = 1) -> dict:
    """Load single entity with neighborhood.

    Returns:
        {
            entity: {entity_id, canonical_name, entity_type, attributes_json, created_at},
            predecessors: [{from_entity_id, from_canonical_name, predicate, confidence}],
            successors: [{to_entity_id, to_canonical_name, predicate, confidence}],
            linked_memories: [{memory_id, title, type}]
        }

    Note: depth is accepted but unused beyond depth=1 (multi-hop is future work).
    """
    with _db() as db:
        # Load the entity itself
        entity_sql = """
        SELECT id, canonical_name, entity_type, attributes_json, created_at
        FROM entities
        WHERE id = ?
        """
        entity_row = db.execute(entity_sql, (entity_id,)).fetchone()

        if not entity_row:
            return {
                "entity": None,
                "predecessors": [],
                "successors": [],
                "linked_memories": [],
            }

        entity_id_val, canonical_name, entity_type, attributes_json, created_at = entity_row
        entity = {
            "entity_id": entity_id_val,
            "canonical_name": canonical_name,
            "entity_type": entity_type,
            "attributes_json": attributes_json or "{}",
            "created_at": created_at,
        }

        # Load predecessors (relationships where to_entity = this entity)
        predecessors_sql = """
        SELECT er.from_entity, e.canonical_name, er.predicate, er.confidence
        FROM entity_relationships er
        JOIN entities e ON e.id = er.from_entity
        WHERE er.to_entity = ?
        """
        predecessors = [
            {
                "from_entity_id": row[0],
                "from_canonical_name": row[1],
                "predicate": row[2],
                "confidence": row[3],
            }
            for row in db.execute(predecessors_sql, (entity_id,)).fetchall()
        ]

        # Load successors (relationships where from_entity = this entity)
        successors_sql = """
        SELECT er.to_entity, e.canonical_name, er.predicate, er.confidence
        FROM entity_relationships er
        JOIN entities e ON e.id = er.to_entity
        WHERE er.from_entity = ?
        """
        successors = [
            {
                "to_entity_id": row[0],
                "to_canonical_name": row[1],
                "predicate": row[2],
                "confidence": row[3],
            }
            for row in db.execute(successors_sql, (entity_id,)).fetchall()
        ]

        # Load linked memories
        memories_sql = """
        SELECT DISTINCT mi.id, mi.title, mi.type
        FROM memory_item_entities mie
        JOIN memory_items mi ON mi.id = mie.memory_id
        WHERE mie.entity_id = ?
        """
        linked_memories = [
            {
                "memory_id": row[0],
                "title": row[1] or "",
                "type": row[2],
            }
            for row in db.execute(memories_sql, (entity_id,)).fetchall()
        ]

        return {
            "entity": entity,
            "predecessors": predecessors,
            "successors": successors,
            "linked_memories": linked_memories,
        }
