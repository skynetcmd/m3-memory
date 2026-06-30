"""Embedding pipeline — the headline of the memory_core modularization.

Phase 3 of the migration. Holds the cascade (_embed, _embed_many), the
in-process Rust embedder lazy-init, HTTP-client singleton, sliding-window
chunking + dense-content recovery, anchor augmentation, backend stats,
content_hash, and the canonical-name cache. Re-exported through the
legacy memory_core shim.

Subtle dependency note: `_embed` and `_embed_many` resolve the active
M3Context via `_ctx()` rather than importing memory_core's `ctx`
singleton, mirroring db.py's pattern (avoids the circular import).
`_track_cost` is lazy-imported from memory_core inside each call because
the telemetry counter (`_COST_COUNTERS`) is owned by memory_core and
will move only when telemetry gets its own module.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import threading
from functools import lru_cache as _lru_cache
from threading import Lock as _ThreadLock

import httpx as _httpx
from embedding_utils import unpack as _unpack
from llm_failover import get_best_embed
from m3_sdk import M3Context, resolve_db_path

from . import config
from .db import _db
from .util import sha256_hex as _sha256_hex

logger = logging.getLogger("memory.embed")


# ──────────────────────────────────────────────────────────────────────────────
# Typed exceptions — for log-line clarity, not for callers
# ──────────────────────────────────────────────────────────────────────────────
# The `_embed` and `_embed_many` cascades catch broadly (`except Exception`)
# and either fall through to the next tier or return None/EMBED_MODEL on
# total failure. That contract is unchanged by these classes — callers
# still get `(None, model)` on failure.
#
# What changes: log lines now carry a specific exception type
# (`EmbeddedBackendError`, `EmbedFallbackError`, `EmbedPrimaryError`,
# `EmbedSemaphoreTimeout`) instead of a generic `Exception`. Grep-ability
# improves materially — "what kind of failure is filling the warning
# stream" was previously a manual `e.__cause__` chain inspection.
#
# These classes are public via the memory_core shim re-export (the
# `from memory.embed import ...` block in memory_core.py picks up
# anything not prefixed with _underscore that the module exports).
# Downstream code is free to `except EmbeddedBackendError` if it
# eventually wants tier-specific reactions — but no existing code does,
# and the cascade itself keeps catching `Exception` to preserve
# fall-through behavior.
class EmbedError(Exception):
    """Base class for all embed-pipeline errors. Internal: callers see
    `(None, model)` on the public surface, not these exceptions."""


class EmbeddedBackendError(EmbedError):
    """In-process llama.cpp embedder failed (GGUF load, kernel error,
    OOM, dim mismatch). Cascade falls through to CPU HTTP fallback."""


class EmbedFallbackError(EmbedError):
    """CPU HTTP fallback (M3_EMBED_FALLBACK_URL) failed — connection
    refused, timeout, malformed JSON, dim mismatch. Cascade falls
    through to primary HTTP."""


class EmbedPrimaryError(EmbedError):
    """Primary HTTP embedder (LM Studio / llama-server / Ollama via
    llm_failover) failed after all retry attempts. Cascade returns
    `(None, model)`."""


class EmbedSemaphoreTimeout(EmbedError):
    """`_EMBED_SEM.acquire()` timed out after 30 s. Indicates the
    process is saturated by in-flight embed calls, not a backend
    health issue. Cascade returns `(None, EMBED_MODEL)`."""


# ──────────────────────────────────────────────────────────────────────────────
# Per-backend circuit breakers (audit item L)
# ──────────────────────────────────────────────────────────────────────────────
# Three breakers, one per cascade tier. After `threshold` consecutive
# failures a breaker opens and the cascade skips that tier entirely until
# `reset_after_secs` elapse, at which point one half-open probe is allowed.
#
# Rationale: prior to this change, every embed call to a dead backend paid
# its full timeout (~30s CPU fallback; up to 6s+ on the primary's 3-retry
# loop with exponential backoff). A 100-call burst against a dead llama-
# server would burn ~3000s of wall-clock with no useful work. The breaker
# bounds total wasted time to roughly `threshold * timeout` before all
# subsequent calls in the window short-circuit to the next tier.
#
# When `m3_core_rs` is None or a threshold is 0, the breaker is None and
# the call sites fall through to the pre-breaker "try every call" behavior.
# This preserves the Python-only fallback path for ops who disable Rust.
def _maybe_make_breaker(threshold: int, reset_after_secs: float):
    """Construct a Rust CircuitBreaker if Rust is available and threshold > 0."""
    if config.m3_core_rs is None or threshold <= 0:
        return None
    return config.m3_core_rs.CircuitBreaker(
        threshold=int(threshold),
        reset_after_secs=float(reset_after_secs),
    )


_EMBEDDED_BREAKER = _maybe_make_breaker(
    config.EMBED_BREAKER_EMBEDDED_THRESHOLD,
    config.EMBED_BREAKER_EMBEDDED_RESET_SECS,
)
_CPU_FALLBACK_BREAKER = _maybe_make_breaker(
    config.EMBED_BREAKER_CPU_FALLBACK_THRESHOLD,
    config.EMBED_BREAKER_CPU_FALLBACK_RESET_SECS,
)
_PRIMARY_BREAKER = _maybe_make_breaker(
    config.EMBED_BREAKER_PRIMARY_THRESHOLD,
    config.EMBED_BREAKER_PRIMARY_RESET_SECS,
)
_CLOUD_BREAKER = _maybe_make_breaker(
    config.EMBED_BREAKER_CLOUD_THRESHOLD,
    config.EMBED_BREAKER_CLOUD_RESET_SECS,
)


def get_embed_breaker_state() -> dict:
    """Return current state of all four embed breakers.

    Useful for diagnostics and surfacing via `embedder_status_impl`.
    Returns `{embedded, cpu_fallback, primary, cloud}` each mapped to a state
    string (`"closed"` / `"open"` / `"half_open"`) or `"disabled"` when
    the breaker isn't constructed (Rust unavailable or threshold=0).
    """
    return {
        "embedded": _EMBEDDED_BREAKER.state() if _EMBEDDED_BREAKER else "disabled",
        "cpu_fallback": _CPU_FALLBACK_BREAKER.state() if _CPU_FALLBACK_BREAKER else "disabled",
        "primary": _PRIMARY_BREAKER.state() if _PRIMARY_BREAKER else "disabled",
        "cloud": _CLOUD_BREAKER.state() if _CLOUD_BREAKER else "disabled",
    }


def reset_embed_breakers() -> dict:
    """Force-close all breakers (test/debug helper). Returns prior state."""
    prior = get_embed_breaker_state()
    for breaker in (_EMBEDDED_BREAKER, _CPU_FALLBACK_BREAKER, _PRIMARY_BREAKER, _CLOUD_BREAKER):
        if breaker is not None:
            breaker.record_success()
    return prior


def _ctx() -> M3Context:
    """Resolve the active M3Context lazily (mirrors memory_core._current_ctx)."""
    return M3Context.for_db(resolve_db_path(None))


def _track_cost_lazy(operation: str, tokens_est: int = 0) -> None:
    """Bump the telemetry counter via memory_core's module-level dict.

    Lazy import avoids circular at load time; Python caches the import.
    Best-effort: never blocks the embed path on a telemetry failure.
    """
    try:
        import memory_core as _mc
        _mc._COST_COUNTERS[operation] = _mc._COST_COUNTERS.get(operation, 0) + 1
        if tokens_est:
            _mc._COST_COUNTERS["embed_tokens_est"] += tokens_est
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# In-process Rust embedder (M3_EMBED_GGUF, or auto-detected when unset)
# ──────────────────────────────────────────────────────────────────────────────
_EMBED_GGUF_PATH: str | None = (os.environ.get("M3_EMBED_GGUF") or "").strip() or None
_EMBED_GGUF_MODEL_TAG: str = (
    (os.environ.get("M3_EMBED_GGUF_MODEL_TAG") or "").strip()
    or "bge-m3-GGUF-Q4_K_M.gguf"
)
# When M3_EMBED_GGUF is unset, search the canonical model dirs for a bge-m3 GGUF
# so tier-1 (the ~10-85x faster in-process embedder) activates automatically.
# Opt out with M3_EMBED_GGUF_AUTODETECT=0. The walk is depth- and time-bounded so
# a pathological models directory can never stall cold start.
_EMBED_GGUF_AUTODETECT: bool = os.environ.get("M3_EMBED_GGUF_AUTODETECT", "1") != "0"
_EMBED_GGUF_WALK_BUDGET_S: float = float(os.environ.get("M3_EMBED_GGUF_WALK_BUDGET", "2.0"))
_embedded_embedder = None
_embedded_embed_checked = False


# ── Embedder identity gate ─────────────────────────────────────────────────────
# A vector is only acceptable if it came from the configured ("proper") embedder.
# These resolve config LIVE (so set_embed_override / env changes are honoured) and
# are model-agnostic. _validate_identity is the single gate every tier calls.

def _proper_embed_dim() -> int:
    return int(config.EMBED_DIM)


def _compatible_model_names() -> frozenset[str]:
    """The embed_model tags that map to the proper embed space. A tier whose tag
    is in this set is accepted; anything else is a foreign embedder. Includes the
    configured name, the tier-1 GGUF tag, the tier-2 fallback tag, the space tag,
    any runtime model override, and operator-supplied extras."""
    names = {
        config.EMBED_MODEL,
        config.EMBED_SPACE_TAG,
        config.EMBED_FALLBACK_MODEL_TAG,
        _EMBED_GGUF_MODEL_TAG,
        config._EMBED_MODEL_OVERRIDE or config.EMBED_MODEL,
        *config.EMBED_COMPATIBLE_MODELS,
    }
    return frozenset(n for n in names if n)


# Log a given identity-rejection reason at most once per source label, so a
# misconfigured tier is visible without flooding the log (the storm we just fixed).
_IDENTITY_WARNED: set[str] = set()


def _validate_identity(vecs, attached_model: str, source_label: str) -> bool:
    """True iff `vecs` (a single vector or a list) is acceptable for the store:
    correct dimension, a compatible model tag, and (if required) unit-length.
    A failure means this tier did not produce a PROPER vector — the caller must
    cascade to the next tier (or defer), NEVER store the vector. Never raises."""
    try:
        sample = vecs[0] if (vecs and isinstance(vecs[0], (list, tuple))) else vecs
        if not sample:
            return False
        dim = _proper_embed_dim()
        if len(sample) != dim:
            _identity_warn(source_label, f"dim {len(sample)} != {dim}")
            return False
        if attached_model not in _compatible_model_names():
            _identity_warn(source_label, f"foreign model {attached_model!r}")
            return False
        # Finite-ness is a hard invariant, independent of the unit-norm policy: a
        # NaN/inf component is never a valid embedding (it poisons every cosine
        # distance) and NaN slips past the norm tolerance check (NaN compares
        # False to everything), so reject non-finite vectors unconditionally.
        if not _sample_is_finite(vecs):
            _identity_warn(source_label, "non-finite vector components")
            return False
        if config.EMBED_REQUIRE_UNIT_NORM and not _sample_is_unit(vecs):
            _identity_warn(source_label, "vectors not unit-normalized")
            return False
        return True
    except Exception:
        return False  # never let the gate itself break the cascade


def _identity_warn(source_label: str, reason: str) -> None:
    key = f"{source_label}:{reason.split(' ')[0]}"
    if key not in _IDENTITY_WARNED:
        _IDENTITY_WARNED.add(key)
        logger.warning("Embed identity rejected from %s: %s — cascading/deferring "
                       "rather than storing a non-proper vector.", source_label, reason)


def _accept_bulk(out, miss_indices, vecs, model: str, label: str) -> list[int]:
    """Assign each proper vector into `out` at its original index; return the
    LOCAL indices (into miss_indices/vecs) whose vector was missing or failed the
    embedder-identity gate, so the caller can narrow the cascade to just those.
    Validates the batch once (cheap, sampled) before accepting any vector — a
    foreign-identity batch is rejected wholesale so no bad vector is stored."""
    real = [v for v in vecs if v is not None]
    if real and not _validate_identity(real, model, label):
        return list(range(len(vecs)))  # whole batch is not proper -> all miss
    still: list[int] = []
    for j, (idx, vec) in enumerate(zip(miss_indices, vecs)):
        if vec is not None:
            out[idx] = (vec, model)
        else:
            still.append(j)
    return still


def _identity_samples(vecs) -> list:
    """The first/middle/last vectors of a bulk batch, or [vecs] for a single
    vector — the sample set shared by the finite-ness and unit-norm checks."""
    if vecs and isinstance(vecs[0], (list, tuple)):
        n = len(vecs)
        idxs = {0, n // 2, n - 1}  # first, middle, last
        return [vecs[i] for i in idxs]
    return [vecs]


def _sample_is_finite(vecs) -> bool:
    """All components of the sampled vector(s) are finite (no NaN/inf). Cheap:
    samples first/middle/last like the norm check. Runs regardless of the
    unit-norm policy — a non-finite component is never a valid embedding."""
    for v in _identity_samples(vecs):
        if not v:
            return False
        if not all(math.isfinite(x) for x in v):
            return False
    return True


def _sample_is_unit(vecs) -> bool:
    """Cheap L2-norm check on a SAMPLE (not every vector in a bulk batch)."""
    samples = _identity_samples(vecs)
    tol = config.EMBED_NORM_TOL
    for v in samples:
        if not v:
            return False
        norm = math.sqrt(sum(x * x for x in v))
        # A non-finite norm (NaN/inf) must be rejected explicitly: NaN compares
        # False to everything, so `abs(nan - 1.0) > tol` is False and a NaN
        # vector would otherwise slip through and poison every cosine distance.
        if not math.isfinite(norm) or abs(norm - 1.0) > tol:
            return False
    return True


def discover_bge_m3_gguf(budget_s: float = _EMBED_GGUF_WALK_BUDGET_S) -> str | None:
    """Probe the canonical model directories for a bge-m3 GGUF and return its
    path, or None. Bounded: at most `budget_s` wall-clock and depth ~4 per dir,
    first match wins. Cross-platform (LM Studio dirs differ per OS; Path.home()
    resolves the home dir). This is the runtime mirror of the setup wizard's
    discovery — keep the two in sync (the wizard imports this helper)."""
    import time
    from pathlib import Path

    home = Path.home()
    candidate_dirs = [
        home / ".lmstudio" / "models",
        home / "Library" / "Application Support" / "LM Studio" / "models",
        home / ".cache" / "lm-studio" / "models",   # Linux LM Studio default (XDG)
        home / ".cache" / "m3" / "models",
        home / ".m3-memory" / "_assets" / "embedder",
        home / "models",
    ]
    deadline = time.monotonic() + max(0.1, budget_s)
    for d in candidate_dirs:
        try:
            if not d.is_dir():
                continue
            base_depth = len(d.parts)
            for path in d.rglob("*.gguf"):
                if time.monotonic() > deadline:
                    logger.debug("bge-m3 GGUF auto-detect: walk budget exceeded")
                    return None
                # Bound depth to ~4 below the candidate dir (LM Studio's
                # org/model/file layout is depth 3; allow one extra).
                if len(path.parts) - base_depth > 4:
                    continue
                name = path.name.lower()
                if "bge-m3" in name or "bge_m3" in name:
                    return str(path)
        except OSError:
            continue
    return None


def _get_embedded_embedder():
    """Return the in-process EmbeddedEmbedder, or None if unavailable/unsafe."""
    global _embedded_embedder, _embedded_embed_checked, _EMBED_GGUF_PATH
    if _embedded_embed_checked:
        return _embedded_embedder
    _embedded_embed_checked = True
    if config.m3_core_rs is None:
        return None
    # Resolve the GGUF path: explicit env wins; otherwise auto-detect (on by
    # default). A failed detect leaves tier-1 off and the cascade falls to HTTP.
    if _EMBED_GGUF_PATH is None and _EMBED_GGUF_AUTODETECT:
        found = discover_bge_m3_gguf()
        if found:
            _EMBED_GGUF_PATH = found
            logger.info("bge-m3 GGUF auto-detected for tier-1: %s", found)
    if _EMBED_GGUF_PATH is None:
        return None
    if not hasattr(config.m3_core_rs, "EmbeddedEmbedder"):
        logger.warning(
            "M3_EMBED_GGUF set but m3_core_rs lacks EmbeddedEmbedder "
            "(wheel built without --features embedded) — using HTTP"
        )
        return None
    try:
        emb = config.m3_core_rs.EmbeddedEmbedder(_EMBED_GGUF_PATH)
        dim = emb.embedding_dim()
        if dim != config.EMBED_DIM:
            logger.error(
                "M3_EMBED_GGUF dimension %d != EMBED_DIM %d — embedded "
                "embedder disabled, using HTTP", dim, config.EMBED_DIM
            )
            return None
        logger.info(
            "embedded llama.cpp embedder active (%s, dim=%d)",
            _EMBED_GGUF_PATH, dim,
        )
        _embedded_embedder = emb
        return emb
    except Exception as e:
        logger.error("embedded embedder init failed (%s) — using HTTP", e)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Sliding-window chunking + dense-content recovery
# ──────────────────────────────────────────────────────────────────────────────
MAX_CHARS_PER_CHUNK = int(os.environ.get("M3_EMBED_CHUNK_MAX_CHARS", 28000))
MIN_OVERLAP_CHARS = int(os.environ.get("M3_EMBED_CHUNK_OVERLAP_CHARS", 8000))
STRIDE_CHARS = MAX_CHARS_PER_CHUNK - MIN_OVERLAP_CHARS


def _chunk_for_sliding_window(text: str) -> list[tuple[str, int]]:
    """Split text into overlapping windows for embedding."""
    n = len(text or "")
    if n <= MAX_CHARS_PER_CHUNK:
        return [(text or "", 0)]
    out: list[tuple[str, int]] = []
    idx = 0
    start = 0
    while True:
        end = start + MAX_CHARS_PER_CHUNK
        if end >= n:
            out.append((text[start:n], idx))
            return out
        out.append((text[start:end], idx))
        idx += 1
        start += STRIDE_CHARS


DENSE_TARGET_TOKENS = 7000
DENSE_TOKEN_OVERLAP = 500
DENSE_MIN_SUB_CHARS = 2000
_DENSE_ERR_RE = re.compile(r"(\d+)\s*tokens\s*>\s*n_ctx")

# Per-call embed-failure sentinels (returned by _post_once, NOT shared state).
# Distinct objects so a chunk's outcome can't leak into a concurrent chunk's.
_EMBED_TRANSIENT = object()   # retryable: timeout / 5xx / 413 / 429
_EMBED_PERMANENT = object()   # non-retryable 4xx: don't retry or bisect


def _order_embeddings(data: list[dict], n_inputs: int) -> list[list[float]] | None:
    """Return embeddings in INPUT order, or None if the response can't be safely
    aligned. An OpenAI-style embeddings response carries a per-item `index`; we
    sort by it. But a server that OMITS index (every item defaults to 0) would
    pass a naive len-check while the vectors are in arbitrary order — storing a
    semantically-WRONG vector under a memory id with no error. Require `index` to
    be a complete permutation of range(n_inputs) before trusting order; reject
    (treat as failure) otherwise."""
    if len(data) != n_inputs:
        return None
    seen = [d.get("index") for d in data]
    if any(ix is None for ix in seen) or sorted(seen) != list(range(n_inputs)):
        return None  # missing / duplicate / out-of-range index -> not alignable
    ordered = sorted(data, key=lambda d: d["index"])
    return [d["embedding"] for d in ordered]


def _subdivide_dense_chunk(text: str, observed_tokens: int) -> list[str]:
    """Re-split a chunk that overflowed the bge-m3 token ceiling."""
    if observed_tokens <= 0 or not text:
        return [text]
    chars_per_token = len(text) / observed_tokens
    sub_chars = int(DENSE_TARGET_TOKENS * chars_per_token * 0.90)
    sub_chars = max(sub_chars, DENSE_MIN_SUB_CHARS)
    if sub_chars >= len(text):
        return [text]
    overlap_chars = int(DENSE_TOKEN_OVERLAP * chars_per_token)
    stride = max(sub_chars - overlap_chars, sub_chars // 2)
    out: list[str] = []
    start = 0
    n = len(text)
    while True:
        end = start + sub_chars
        if end >= n:
            out.append(text[start:n])
            return out
        out.append(text[start:end])
        start += stride


def _mean_pool(vecs: list[list[float]]) -> list[float] | None:
    """Average several sub-chunk vectors into one (standard long-doc embedding),
    then L2-NORMALIZE the result. bge-m3 vectors are unit-length and the store /
    cosine paths assume that invariant — mean-pooling alone yields a sub-unit
    vector (norm < 1), so it MUST be renormalized or it is incomparable to every
    other vector in the store. Returns None if there's nothing to pool."""
    if not vecs:
        return None
    if len(vecs) == 1:
        return vecs[0]  # already a normalized model output
    dim = len(vecs[0])
    acc = [0.0] * dim
    n = 0
    for v in vecs:
        if len(v) != dim:  # defensive: skip a malformed sub-vector
            continue
        for k in range(dim):
            acc[k] += v[k]
        n += 1
    if n == 0:
        return None
    norm = math.sqrt(sum(x * x for x in acc))
    if norm == 0.0:
        return [0.0] * dim  # degenerate (opposing vectors); avoid /0
    return [x / norm for x in acc]  # mean then L2-normalize (the /n cancels)


async def _embedded_bulk_with_subdivide(
    embedded, texts: list[str]
) -> list[list[float] | None]:
    """Embed each text via the in-process (tier-1) embedder, subdividing any row
    that overflows n_ctx and mean-pooling its sub-chunk vectors. Returns one
    vector per input (None for a row tier 1 still can't embed). Keeps an
    oversized row IN-PROCESS instead of cascading the whole batch to HTTP."""
    def _embed_list(items: list[str]) -> list[list[float]]:
        return embedded.embed(items)

    results: list[list[float] | None] = []
    for text in texts:
        try:
            vecs = await asyncio.to_thread(_embed_list, [text])
            results.append(vecs[0])
            continue
        except Exception as e:
            m = _DENSE_ERR_RE.search(str(e))
            if not m:
                results.append(None)
                continue
            observed = int(m.group(1))
        # Overflow: split into sub-chunks, embed each, mean-pool.
        try:
            subs = _subdivide_dense_chunk(text, observed)
            sub_vecs = await asyncio.to_thread(_embed_list, subs)
            results.append(_mean_pool([v for v in sub_vecs if v is not None]))
        except Exception as e2:
            logger.warning("Subdivide-embed failed for oversized row: %s", e2)
            results.append(None)
    return results


# ──────────────────────────────────────────────────────────────────────────────
# Anchor augmentation (passage-side only — see EMBED_INPUT_RECIPE.md)
# ──────────────────────────────────────────────────────────────────────────────
def _augment_embed_text_with_anchors(embed_text: str, metadata: str | dict | None) -> str:
    """Prepend `[anchor1, anchor2]` to text from metadata['temporal_anchors']."""
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


@_lru_cache(maxsize=512)
def _content_hash(content: str) -> str:
    """sha256 of (content or "") UTF-8 bytes, lru-cached at 512 entries.

    Called once per embed (line 322 below) and N times per chatlog write
    pass; sees frequent repeats during bulk re-embed and chatlog drain.
    Cache key is the raw content string — modest memory footprint for
    typical memory bodies (under a few KB each). 512 entries is enough
    to absorb repeats within a single chatlog drain without unbounded
    growth.
    """
    return _sha256_hex((content or "").encode("utf-8"))


# ──────────────────────────────────────────────────────────────────────────────
# HTTP-client singleton
# ──────────────────────────────────────────────────────────────────────────────
_EMBED_HTTP_MAX_CONNS = int(os.environ.get("M3_EMBED_HTTP_MAX_CONNS", "32"))
_EMBED_HTTP_MAX_KEEPALIVE = int(os.environ.get("M3_EMBED_HTTP_MAX_KEEPALIVE", "16"))
_EMBED_HTTP_KEEPALIVE_EXPIRY = float(
    os.environ.get("M3_EMBED_HTTP_KEEPALIVE_EXPIRY", "60.0")
)

_EMBED_CLIENT: _httpx.AsyncClient | None = None
_EMBED_CLIENT_LOOP_ID: int | None = None
_EMBED_CLIENT_LOCK = threading.Lock()
_shared_embed_client: _httpx.AsyncClient | None = None  # legacy alias


def _get_embed_client() -> _httpx.AsyncClient:
    """Return a process-wide, pool-tuned httpx.AsyncClient for embed traffic."""
    global _EMBED_CLIENT, _EMBED_CLIENT_LOOP_ID, _shared_embed_client
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_id = None
    if (
        _EMBED_CLIENT is None
        or _EMBED_CLIENT.is_closed
        or loop_id != _EMBED_CLIENT_LOOP_ID
    ):
        with _EMBED_CLIENT_LOCK:
            if (
                _EMBED_CLIENT is None
                or _EMBED_CLIENT.is_closed
                or loop_id != _EMBED_CLIENT_LOOP_ID
            ):
                limits = _httpx.Limits(
                    max_connections=_EMBED_HTTP_MAX_CONNS,
                    max_keepalive_connections=_EMBED_HTTP_MAX_KEEPALIVE,
                    keepalive_expiry=_EMBED_HTTP_KEEPALIVE_EXPIRY,
                )
                timeout = _httpx.Timeout(
                    connect=config.CHROMA_CONNECT_T,
                    read=config.EMBED_TIMEOUT_READ,
                    write=10.0,
                    pool=5.0,
                )
                _EMBED_CLIENT = _httpx.AsyncClient(
                    limits=limits, timeout=timeout, http2=False,
                )
                _EMBED_CLIENT_LOOP_ID = loop_id
                _shared_embed_client = _EMBED_CLIENT
                logger.debug(
                    f"Initialized embed httpx.AsyncClient "
                    f"(max_conns={_EMBED_HTTP_MAX_CONNS}, "
                    f"keepalive={_EMBED_HTTP_MAX_KEEPALIVE}, "
                    f"expiry={_EMBED_HTTP_KEEPALIVE_EXPIRY}s, http/1.1)"
                )
    return _EMBED_CLIENT  # type: ignore[return-value]


_EMBED_FALLBACK_URL: str = (
    os.environ.get("M3_EMBED_FALLBACK_URL") or "http://127.0.0.1:8082"
).rstrip("/")


# ──────────────────────────────────────────────────────────────────────────────
# Backend stats (thread-safe)
# ──────────────────────────────────────────────────────────────────────────────
_EMBED_BACKEND_STATS: dict[str, int] = {}
_EMBED_BACKEND_STATS_LOCK = _ThreadLock()


def _record_embed_backend(label: str, call_count: int = 1) -> None:
    """Increment the served-call counter for one embed-path label."""
    with _EMBED_BACKEND_STATS_LOCK:
        _EMBED_BACKEND_STATS[label] = _EMBED_BACKEND_STATS.get(label, 0) + call_count


def get_embed_backend_stats() -> dict[str, int]:
    """Snapshot of which embed paths have served calls in this process."""
    with _EMBED_BACKEND_STATS_LOCK:
        return dict(_EMBED_BACKEND_STATS)


def reset_embed_backend_stats() -> None:
    """Clear the served-call stats dict — useful between benchmark phases."""
    with _EMBED_BACKEND_STATS_LOCK:
        _EMBED_BACKEND_STATS.clear()


def _embedded_label() -> str:
    """Return the in-process backend-label string for stats."""
    try:
        import m3_core_rs as _m3
        bk = getattr(_m3, "embed_backend_label", lambda: "cpu")()
    except Exception:
        bk = "cpu"
    return f"{bk}-inprocess"


def set_embed_override(url: str | None, model: str | None = None) -> None:
    """Set or clear the embedder endpoint override at runtime."""
    new_url = (url or "").strip() or None
    new_model = (model or "").strip() or None
    config._EMBED_URL_OVERRIDE = new_url
    config._EMBED_MODEL_OVERRIDE = new_model
    try:
        from llm_failover import clear_embed_cache as _cec
        _cec()
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Per-call + bulk semaphores
# ──────────────────────────────────────────────────────────────────────────────
_EMBED_SEM = asyncio.Semaphore(4)

EMBED_BULK_CHUNK = int(os.environ.get("EMBED_BULK_CHUNK", "1024"))
EMBED_BULK_CONCURRENCY = int(os.environ.get("EMBED_BULK_CONCURRENCY", "4"))
_EMBED_BULK_SEM = asyncio.Semaphore(EMBED_BULK_CONCURRENCY)


# ──────────────────────────────────────────────────────────────────────────────
# The cascade itself
# ──────────────────────────────────────────────────────────────────────────────
async def _embed(text: str) -> tuple[list[float] | None, str]:
    c_hash = _content_hash(text)
    embedded = _get_embedded_embedder()
    cache_model = _EMBED_GGUF_MODEL_TAG if embedded is not None else config.EMBED_MODEL
    try:
        with _db() as db:
            cached = db.execute(
                "SELECT embedding, embed_model FROM memory_embeddings "
                "WHERE content_hash = ? AND embed_model = ? LIMIT 1",
                (c_hash, cache_model),
            ).fetchone()
            if cached:
                return _unpack(cached["embedding"]), cached["embed_model"]
    except Exception as e:
        logger.debug(f"Embedding cache lookup failed: {e}")

    # Tier 1: in-process Rust embedder. Gated by _EMBEDDED_BREAKER so a
    # storm of CUDA/OOM failures doesn't cost ~ms each call indefinitely.
    if embedded is not None and (
        _EMBEDDED_BREAKER is None or _EMBEDDED_BREAKER.allow_request()
    ):
        try:
            _track_cost_lazy("embed_calls", len(text.split()) * 2)
            vec = await asyncio.to_thread(lambda: embedded.embed([text])[0])
            if not _validate_identity(vec, _EMBED_GGUF_MODEL_TAG, "tier1-embedded"):
                raise EmbeddedBackendError("output failed embedder-identity check")
            if _EMBEDDED_BREAKER is not None:
                _EMBEDDED_BREAKER.record_success()
            _record_embed_backend(_embedded_label(), 1)
            return vec, _EMBED_GGUF_MODEL_TAG
        except Exception as e:
            # Wrap as EmbeddedBackendError purely for log-line clarity;
            # cascade still falls through (we don't re-raise).
            # Annotated with the common base so the other except blocks below
            # can rebind it to sibling EmbedError subclasses.
            wrapped: EmbedError = EmbeddedBackendError(str(e))
            wrapped.__cause__ = e
            if _EMBEDDED_BREAKER is not None:
                _EMBEDDED_BREAKER.record_failure()
            logger.warning(
                f"{type(wrapped).__name__}: {wrapped} — falling back to CPU HTTP"
            )

    # Tier 2: local CPU HTTP fallback — the m3-embed-server service at
    # _EMBED_FALLBACK_URL (default http://127.0.0.1:8082). This service is
    # always-on (Windows service / systemd unit) and serves BGE-M3 over HTTP.
    # It is INDEPENDENT of tier 1's in-proc GGUF, so we always try it as
    # long as the breaker allows — formerly this was gated on
    # _EMBED_GGUF_PATH which caused MCP-server cold cascades (no env var
    # set) to skip 8082 entirely and fall straight to Ollama.
    #
    # Storm risk: a dead server eats the full read timeout per call. Breaker
    # bounds that to `threshold` strikes before short-circuiting to primary.
    if (_CPU_FALLBACK_BREAKER is None or _CPU_FALLBACK_BREAKER.allow_request()):
        try:
            client = _get_embed_client()
            resp = await client.post(
                f"{_EMBED_FALLBACK_URL}/embedding",
                json={"input": [text]},
                timeout=_httpx.Timeout(config.CHROMA_CONNECT_T, read=config.EMBED_TIMEOUT_READ),
            )
            resp.raise_for_status()
            payload = resp.json()
            emb = payload["data"][0]["embedding"]
            # The CPU HTTP service carries the proper-identity fallback tag, NOT
            # the tier-1 GGUF filename (a historical mis-tag).
            if not _validate_identity(emb, config.EMBED_FALLBACK_MODEL_TAG, "tier2-cpu-http"):
                raise EmbedFallbackError("output failed embedder-identity check")
            if _CPU_FALLBACK_BREAKER is not None:
                _CPU_FALLBACK_BREAKER.record_success()
            _record_embed_backend("cpu-http-fallback", 1)
            return emb, config.EMBED_FALLBACK_MODEL_TAG
        except Exception as e:
            wrapped = EmbedFallbackError(f"{_EMBED_FALLBACK_URL}: {e}")
            wrapped.__cause__ = e
            if _CPU_FALLBACK_BREAKER is not None:
                _CPU_FALLBACK_BREAKER.record_failure()
            logger.warning(
                f"{type(wrapped).__name__}: {wrapped} — using primary HTTP"
            )

    # Tier 3: primary HTTP via llm_failover. Three-attempt internal retry
    # with exponential backoff (2s, 4s). The breaker gates the WHOLE tier,
    # not each retry — one tick per _embed call. When open, short-circuit
    # to the final "return None" without attempting any of the 3 retries
    # (saving up to ~6s wall-clock per call during a primary outage).
    if _PRIMARY_BREAKER is not None and not _PRIMARY_BREAKER.allow_request():
        logger.warning(
            "EmbedPrimaryError: primary breaker open — short-circuiting (state=open)"
        )
        return None, config.EMBED_MODEL

    try:
        await asyncio.wait_for(_EMBED_SEM.acquire(), timeout=30.0)
    except asyncio.TimeoutError as e:
        wrapped = EmbedSemaphoreTimeout("30s")
        wrapped.__cause__ = e
        logger.error(
            f"{type(wrapped).__name__}: {wrapped} — process saturated by in-flight embed calls"
        )
        return None, config.EMBED_MODEL

    model = config.EMBED_MODEL
    try:
        try:
            _track_cost_lazy("embed_calls", len(text.split()) * 2)
            token = _ctx().get_secret("LM_API_TOKEN") or "lm-studio"
            client = _get_embed_client()
            if config._EMBED_URL_OVERRIDE:
                base_url = config._EMBED_URL_OVERRIDE.rstrip("/")
                model = config._EMBED_MODEL_OVERRIDE or "bge-m3-GGUF-Q4_K_M.gguf"
            else:
                result = await get_best_embed(client, token)
                if not result:
                    raise RuntimeError("No primary embedding backend returned by get_best_embed")
                base_url, model = result

            last_exc = None
            for attempt in range(3):
                try:
                    resp = await client.post(
                        f"{base_url}/embeddings",
                        json={"model": model, "input": text},
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=_httpx.Timeout(config.CHROMA_CONNECT_T, read=config.EMBED_TIMEOUT_READ),
                    )
                    resp.raise_for_status()
                    emb = resp.json()["data"][0]["embedding"]

                    if not _validate_identity(emb, model, "tier3-primary"):
                        raise RuntimeError(
                            f"primary embed output failed identity (model={model!r})")

                    if _PRIMARY_BREAKER is not None:
                        _PRIMARY_BREAKER.record_success()
                    _record_embed_backend("http-primary", 1)
                    return emb, model
                except Exception as e:
                    last_exc = e
                    if attempt < 2:
                        wait = 2 * (2 ** attempt)
                        logger.warning(
                            f"Embedding attempt {attempt + 1} failed: {e}. Retrying in {wait}s..."
                        )
                        await asyncio.sleep(wait)

            # All 3 attempts exhausted — tick the breaker once for the whole
            # call, then wrap the last exception for log clarity.
            if _PRIMARY_BREAKER is not None:
                _PRIMARY_BREAKER.record_failure()
            wrapped = EmbedPrimaryError(f"{base_url}: {last_exc}")
            wrapped.__cause__ = last_exc
            logger.error(
                f"{type(wrapped).__name__}: {wrapped} after 3 attempts"
            )
            from llm_failover import clear_embed_cache
            clear_embed_cache()
        except Exception as e:
            if _PRIMARY_BREAKER is not None:
                _PRIMARY_BREAKER.record_failure()
            wrapped = EmbedPrimaryError(str(e))
            wrapped.__cause__ = e
            logger.warning(
                f"Primary HTTP fallback exception: {wrapped} — attempting Tier 4 fallback if allowed"
            )
            from llm_failover import clear_embed_cache
            clear_embed_cache()

        # Fall through to Tier 4 Cloud Enclave if enabled
        if config.M3_ALLOW_CLOUD_FALLBACK and config.M3_CLOUD_ENCLAVE_URL:
            if _CLOUD_BREAKER is None or _CLOUD_BREAKER.allow_request():
                try:
                    # 1. PII Redaction Gate
                    from chatlog_redaction import scrub
                    redact_cfg = {
                        "enabled": True,
                        "patterns": ["api_keys", "bearer_tokens", "jwt", "aws_keys", "github_tokens", "pii"],
                        "custom_regex": [],
                        "redact_pii": True,
                    }
                    scrubbed_text, match_count, groups_fired = scrub(text, redact_cfg)
                    if match_count > 0:
                        logger.info(
                            f"PII Redaction Gate: redacted {match_count} items from "
                            f"text before cloud transmission (groups: {groups_fired})"
                        )

                    # 2. Keyring credentials resolution
                    token = None
                    if config.M3_CLOUD_AUTH_TOKEN_KEYRING:
                        try:
                            if ":" in config.M3_CLOUD_AUTH_TOKEN_KEYRING:
                                service, username = config.M3_CLOUD_AUTH_TOKEN_KEYRING.split(":", 1)
                            else:
                                service = "m3_memory"
                                username = config.M3_CLOUD_AUTH_TOKEN_KEYRING
                            from auth_utils import safe_keyring_get_password
                            token = safe_keyring_get_password(service, username)
                        except Exception as keyring_err:
                            logger.warning(f"Keyring lookup for cloud token failed: {keyring_err}")

                    if not token:
                        token = os.environ.get("M3_CLOUD_AUTH_TOKEN")

                    # 3. HTTP Call to Cloud Enclave
                    client = _get_embed_client()
                    headers = {}
                    if token:
                        headers["Authorization"] = f"Bearer {token}"

                    url = config.M3_CLOUD_ENCLAVE_URL.rstrip("/")
                    post_url = url if url.endswith("/embeddings") or url.endswith("/embedding") else f"{url}/embeddings"

                    resp = await client.post(
                        post_url,
                        json={"model": config.EMBED_MODEL, "input": scrubbed_text},
                        headers=headers,
                        timeout=_httpx.Timeout(config.CHROMA_CONNECT_T, read=config.EMBED_TIMEOUT_READ * 2),
                    )
                    resp.raise_for_status()
                    emb = resp.json()["data"][0]["embedding"]

                    if not _validate_identity(emb, config.EMBED_MODEL, "tier4-cloud"):
                        raise RuntimeError("cloud enclave output failed identity check")

                    if _CLOUD_BREAKER is not None:
                        _CLOUD_BREAKER.record_success()
                    _record_embed_backend("cloud-enclave", 1)
                    return emb, config.EMBED_MODEL
                except Exception as cloud_err:
                    if _CLOUD_BREAKER is not None:
                        _CLOUD_BREAKER.record_failure()
                    logger.error(f"Tier 4 Cloud Enclave failed: {cloud_err}. Routing payload back to local fallback.")

        return None, model
    finally:
        _EMBED_SEM.release()


async def _embed_many_cloud_fallback(
    out: list[tuple[list[float] | None, str]],
    miss_indices: list[int],
    texts: list[str],
) -> None:
    """Invokes Tier 4 Cloud Enclave for any items in miss_indices that failed to embed."""
    if not (config.M3_ALLOW_CLOUD_FALLBACK and config.M3_CLOUD_ENCLAVE_URL):
        return

    cloud_indices = [idx for idx in miss_indices if out[idx] is None or out[idx][0] is None]
    if not cloud_indices:
        return

    if _CLOUD_BREAKER is not None and not _CLOUD_BREAKER.allow_request():
        logger.warning("Cloud Enclave breaker is open. Skipping Tier 4 fallback.")
        return

    try:
        # 1. PII Redaction Gate
        from chatlog_redaction import scrub
        redact_cfg = {
            "enabled": True,
            "patterns": ["api_keys", "bearer_tokens", "jwt", "aws_keys", "github_tokens", "pii"],
            "custom_regex": [],
            "redact_pii": True,
        }

        cloud_texts = []
        for idx in cloud_indices:
            scrubbed, _, _ = scrub(texts[idx], redact_cfg)
            cloud_texts.append(scrubbed)

        # 2. Keyring credentials resolution
        token = None
        if config.M3_CLOUD_AUTH_TOKEN_KEYRING:
            try:
                if ":" in config.M3_CLOUD_AUTH_TOKEN_KEYRING:
                    service, username = config.M3_CLOUD_AUTH_TOKEN_KEYRING.split(":", 1)
                else:
                    service = "m3_memory"
                    username = config.M3_CLOUD_AUTH_TOKEN_KEYRING
                from auth_utils import safe_keyring_get_password
                token = safe_keyring_get_password(service, username)
            except Exception as keyring_err:
                logger.warning(f"Keyring lookup for cloud token failed: {keyring_err}")

        if not token:
            token = os.environ.get("M3_CLOUD_AUTH_TOKEN")

        # 3. HTTP Call to Cloud Enclave (Batched)
        client = _get_embed_client()
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        url = config.M3_CLOUD_ENCLAVE_URL.rstrip("/")
        post_url = url if url.endswith("/embeddings") or url.endswith("/embedding") else f"{url}/embeddings"

        resp = await client.post(
            post_url,
            json={"model": config.EMBED_MODEL, "input": cloud_texts},
            headers=headers,
            timeout=_httpx.Timeout(config.CHROMA_CONNECT_T, read=config.EMBED_TIMEOUT_READ * 4),
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        vecs = _order_embeddings(data, len(cloud_texts))

        real = [v for v in vecs if v is not None] if vecs is not None else []
        if vecs is not None and (not real or _validate_identity(
                real, config.EMBED_MODEL, "tier4-cloud-bulk")):
            _cloud_served = 0
            for idx, vec in zip(cloud_indices, vecs):
                if vec is not None:
                    out[idx] = (vec, config.EMBED_MODEL)
                    _cloud_served += 1

            if _cloud_served:
                if _CLOUD_BREAKER is not None:
                    _CLOUD_BREAKER.record_success()
                _record_embed_backend("cloud-enclave", _cloud_served)
        else:
            logger.error("Cloud enclave response for %d inputs could not be "
                         "aligned (bad/missing index or count) — not stored.",
                         len(cloud_texts))
            if _CLOUD_BREAKER is not None:
                _CLOUD_BREAKER.record_failure()
    except Exception as e:
        if _CLOUD_BREAKER is not None:
            _CLOUD_BREAKER.record_failure()
        logger.error(f"Tier 4 Cloud Enclave bulk failed: {e}. Routing payloads back to local fallback.")


async def _embed_many(texts: list[str]) -> list[tuple[list[float] | None, str]]:
    """Batched embed path; honors the content-hash cache."""
    if not texts:
        return []

    out: list[tuple[list[float] | None, str] | None] = [None] * len(texts)

    embedded = _get_embedded_embedder()
    cache_model = _EMBED_GGUF_MODEL_TAG if embedded is not None else config.EMBED_MODEL

    hashes = [_content_hash(t) for t in texts]
    uniq_hashes = list(set(hashes))
    cached_vecs: dict[str, tuple[list[float], str]] = {}
    # Audit item A: cache lookup is single-query batched within each chunk
    # (one SQL round-trip, not per-row). Chunked at 500 to stay safely under
    # SQLite's SQLITE_MAX_VARIABLE_NUMBER (32766 on modern builds, 999 on
    # older). A 50k-item bulk write would otherwise hit the cap on the
    # IN(...) clause. Same chunk size memory_delete_bulk uses (commit
    # 249b4b2). With 1000 unique hashes we typically do 2 round-trips
    # instead of one; the wall-time difference is dominated by network/disk
    # round-trip latency (~3-5ms each on local SQLite), not the IN-clause
    # parse cost, so this is effectively free relative to a 5-20s embed
    # batch.
    _CACHE_LOOKUP_CHUNK = 500
    try:
        with _db() as db:
            for start in range(0, len(uniq_hashes), _CACHE_LOOKUP_CHUNK):
                chunk = uniq_hashes[start : start + _CACHE_LOOKUP_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                rows = db.execute(
                    f"SELECT content_hash, embedding, embed_model FROM memory_embeddings "
                    f"WHERE embed_model = ? AND content_hash IN ({placeholders})",
                    (cache_model, *chunk),
                ).fetchall()
                for r in rows:
                    cached_vecs[r["content_hash"]] = (_unpack(r["embedding"]), r["embed_model"])
    except Exception as e:
        logger.debug(f"Bulk embed cache lookup failed: {e}")

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

    if embedded is not None:
        try:
            _track_cost_lazy("embed_calls", sum(len(t.split()) * 2 for t in miss_texts))
            vecs = await asyncio.to_thread(lambda: embedded.embed(miss_texts))
            # Identity gate (previously this bulk path had NO dim/model check): a
            # row whose vector isn't proper stays in the miss set and cascades.
            kept_local = _accept_bulk(out, miss_indices, vecs,
                                      _EMBED_GGUF_MODEL_TAG, "tier1-embedded-bulk")
            if not kept_local:
                _record_embed_backend(_embedded_label(), len(miss_texts))
                return out  # type: ignore[return-value]
            miss_indices = [miss_indices[j] for j in kept_local]
            miss_texts = [miss_texts[j] for j in kept_local]
        except Exception as e:
            # An n_ctx overflow ("N tokens > n_ctx") fails the WHOLE batch even
            # though only one row is too long. Don't cascade the whole batch to
            # HTTP for that — subdivide the oversized row(s) in-process (tier 1
            # handles them), and only the rows tier 1 genuinely can't do fall
            # through. This keeps a single oversized row from cascading to the
            # HTTP tiers (and, when those are down, fanning out into a 4xx storm).
            if _DENSE_ERR_RE.search(str(e)):
                resolved = await _embedded_bulk_with_subdivide(embedded, miss_texts)
                still_missing_local = []
                for j, (idx, vec) in enumerate(zip(miss_indices, resolved)):
                    if vec is not None:
                        out[idx] = (vec, _EMBED_GGUF_MODEL_TAG)
                    else:
                        still_missing_local.append(j)
                if not still_missing_local:
                    _record_embed_backend(_embedded_label(), len(miss_texts))
                    return out  # type: ignore[return-value]
                # Narrow the cascade to ONLY the rows tier 1 couldn't embed.
                miss_indices = [miss_indices[j] for j in still_missing_local]
                miss_texts = [miss_texts[j] for j in still_missing_local]
                logger.warning(
                    "Embedded bulk: %d oversized row(s) embedded via subdivide; "
                    "%d still unresolved — falling back to CPU HTTP for those.",
                    len(resolved) - len(still_missing_local), len(still_missing_local))
            else:
                logger.warning(f"Embedded bulk embed failed ({e}) — falling back to CPU HTTP")

    # Tier 2 (bulk): same architecture as single-_embed — always try the
    # always-on 8082 service regardless of tier-1 GGUF configuration.
    try:
        _track_cost_lazy("embed_calls", sum(len(t.split()) * 2 for t in miss_texts))
        client = _get_embed_client()
        resp = await client.post(
            f"{_EMBED_FALLBACK_URL}/embedding",
            json={"input": miss_texts},
            timeout=_httpx.Timeout(config.CHROMA_CONNECT_T, read=config.EMBED_TIMEOUT_READ * 4),
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        vecs = _order_embeddings(data, len(miss_texts))
        if vecs is None:
            raise RuntimeError(
                f"CPU fallback response for {len(miss_texts)} inputs could not be "
                f"aligned (bad/missing index or count)"
            )
        # Retag to the proper-identity fallback tag (not the tier-1 GGUF name)
        # and gate: rows failing identity stay in the miss set and cascade.
        kept_local = _accept_bulk(out, miss_indices, vecs,
                                  config.EMBED_FALLBACK_MODEL_TAG, "tier2-cpu-http-bulk")
        if not kept_local:
            _record_embed_backend("cpu-http-fallback", len(miss_texts))
            return out  # type: ignore[return-value]
        miss_indices = [miss_indices[j] for j in kept_local]
        miss_texts = [miss_texts[j] for j in kept_local]
    except Exception as e:
        logger.warning(
            f"CPU HTTP fallback ({_EMBED_FALLBACK_URL}) bulk failed ({e}) — using primary HTTP"
        )

    _track_cost_lazy("embed_calls", sum(len(t.split()) * 2 for t in miss_texts))
    token = _ctx().get_secret("LM_API_TOKEN") or "lm-studio"
    client = _get_embed_client()
    if config._EMBED_URL_OVERRIDE:
        base_url = config._EMBED_URL_OVERRIDE.rstrip("/")
        model = config._EMBED_MODEL_OVERRIDE or "bge-m3-GGUF-Q4_K_M.gguf"
    else:
        result = await get_best_embed(client, token)
        if not result:
            for i in miss_indices:
                out[i] = (None, config.EMBED_MODEL)
            await _embed_many_cloud_fallback(out, miss_indices, texts)
            return out  # type: ignore[return-value]
        base_url, model = result

    # _post_once returns a PER-CALL result — never shared mutable state. Each
    # concurrent chunk decides its own fate; a permanent 4xx in one chunk must
    # NOT cause a concurrent chunk's transient failure to be dropped as permanent
    # (that would silently lose embeddable rows). Sentinels:
    #   list[...]              -> success (the vectors)
    #   _EMBED_TRANSIENT       -> retryable (timeout / 5xx / 413 / 429)
    #   (_EMBED_PERMANENT,msg) -> non-retryable 4xx; don't retry or bisect

    async def _post_once(chunk_texts: list[str]):
        try:
            resp = await client.post(
                f"{base_url}/embeddings",
                json={"model": model, "input": chunk_texts},
                headers={"Authorization": f"Bearer {token}"},
                timeout=_httpx.Timeout(config.CHROMA_CONNECT_T, read=config.EMBED_TIMEOUT_READ * 4),
            )
            resp.raise_for_status()
            data = resp.json()["data"]
            ordered = _order_embeddings(data, len(chunk_texts))
            if ordered is None:
                # Response can't be aligned to inputs — treat as transient (a
                # smaller batch via bisect may return a clean index), never
                # store mis-aligned vectors.
                return _EMBED_TRANSIENT
            return ordered
        except _httpx.HTTPStatusError as e:
            code = e.response.status_code
            msg = f"HTTP {code}: {e.response.text[:300]}"
            # 4xx = the request is wrong and won't succeed on retry — EXCEPT 413
            # (payload too large) and 429 (rate limit), where a smaller batch /
            # a wait genuinely helps. Everything else is permanent.
            if 400 <= code < 500 and code not in (413, 429):
                return (_EMBED_PERMANENT, msg)
            return _EMBED_TRANSIENT
        except Exception:
            return _EMBED_TRANSIENT

    async def _post_chunk(chunk_texts: list[str]) -> list[list[float] | None]:
        async with _EMBED_BULK_SEM:
            for attempt in range(3):
                result = await _post_once(chunk_texts)
                if isinstance(result, list):
                    return result
                # Permanent (non-retryable 4xx): stop immediately — no backoff
                # retries, no bisect. Drop this chunk; the next sweep can retry
                # once the underlying cause (e.g. wrong embed endpoint) is fixed.
                if isinstance(result, tuple) and result[0] is _EMBED_PERMANENT:
                    logger.warning(
                        "Bulk embed: permanent failure (%s) — dropping %d input(s) "
                        "without retry/bisect.", result[1], len(chunk_texts))
                    return [None] * len(chunk_texts)
                if attempt < 2:
                    await asyncio.sleep(2 * (2 ** attempt))

        if len(chunk_texts) == 1:
            logger.warning(
                "Bulk embed: dropping single input of len=%d after 3 transient "
                "attempts.", len(chunk_texts[0]))
            return [None]
        # Only transient/size failures reach here — bisecting can help (smaller
        # batch, or isolate one oversized item). Permanent failures returned above.
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

    chunks = [
        miss_texts[i : i + EMBED_BULK_CHUNK]
        for i in range(0, len(miss_texts), EMBED_BULK_CHUNK)
    ]
    chunk_results = await asyncio.gather(*(_post_chunk(c) for c in chunks))

    flat: list[list[float] | None] = []
    for cr in chunk_results:
        flat.extend(cr)
    # Identity gate: a primary-HTTP vector that isn't proper is set to None so it
    # stays a miss and is handed to the cloud fallback (never stored as-is).
    real = [v for v in flat if v is not None]
    primary_ok = (not real) or _validate_identity(real, model, "tier3-primary-bulk")
    _primary_served = 0
    for local_i, vec in enumerate(flat):
        if vec is not None and primary_ok:
            out[miss_indices[local_i]] = (vec, model)
            _primary_served += 1
        else:
            out[miss_indices[local_i]] = (None, model)
    if _primary_served:
        _record_embed_backend("http-primary", _primary_served)

    await _embed_many_cloud_fallback(out, miss_indices, texts)
    return out  # type: ignore[return-value]


# ──────────────────────────────────────────────────────────────────────────────
# Entity-name embedding cache (used by entity resolution)
# ──────────────────────────────────────────────────────────────────────────────
_ENTITY_NAME_EMBED_CACHE: dict[str, list[float]] = {}
ENTITY_NAME_EMBED_CACHE_MAX = int(os.environ.get("ENTITY_NAME_EMBED_CACHE_MAX", "50000"))


async def _embed_canonical_cached(canonical_name: str) -> list[float] | None:
    """Embed a canonical_name via the cache. Misses fall through to _embed."""
    cached = _ENTITY_NAME_EMBED_CACHE.get(canonical_name)
    if cached is not None:
        return cached
    vec, _ = await _embed(canonical_name)
    if vec is None:
        return None
    if len(_ENTITY_NAME_EMBED_CACHE) >= ENTITY_NAME_EMBED_CACHE_MAX:
        _ENTITY_NAME_EMBED_CACHE.clear()
    _ENTITY_NAME_EMBED_CACHE[canonical_name] = vec
    return vec


async def prime_entity_name_cache(names: list[str]) -> int:
    """Pre-warm the canonical-name embed cache for a batch of names in ONE
    batched embed call. Resolution then hits the warm cache via
    _embed_canonical_cached instead of embedding each name individually.

    The single-item embed kernel is GPU-starved (~15-200ms/name depending on
    device contention); batching many names through _embed_many amortizes that
    to ~1ms/name (measured ~13x on this GPU). A bulk extractor that knows all of
    a batch's entity names up front should call this before the resolve loop.

    Only embeds names not already cached. Returns the number newly cached.
    Best-effort: a failed embed for one name just leaves it uncached (the resolve
    path will embed it individually later)."""
    todo = [n for n in dict.fromkeys(names) if n and n not in _ENTITY_NAME_EMBED_CACHE]
    if not todo:
        return 0
    results = await _embed_many(todo)
    newly = 0
    for name, (vec, _model) in zip(todo, results):
        if vec is None:
            continue
        if len(_ENTITY_NAME_EMBED_CACHE) >= ENTITY_NAME_EMBED_CACHE_MAX:
            _ENTITY_NAME_EMBED_CACHE.clear()
        _ENTITY_NAME_EMBED_CACHE[name] = vec
        newly += 1
    return newly


# ──────────────────────────────────────────────────────────────────────────────
# Status probe
# ──────────────────────────────────────────────────────────────────────────────
async def embedder_status_impl() -> dict:
    """Returns the status of the local sovereign embedder server.

    Probes the URL configured by M3_EMBED_FALLBACK_URL (default
    http://127.0.0.1:8082). This is the always-on CPU BGE-M3 service
    shipped as the `m3-embed-server` Windows/Unix service. Returns a
    structured dict with health, metrics, and any error.

    Use `memory_doctor` for a broader cascade health check (tier 1 GGUF +
    tier 2 service + DB integrity + roundtrip smoke).
    """
    import http.client
    from urllib.parse import urlparse

    url = os.environ.get("M3_EMBED_FALLBACK_URL") or "http://127.0.0.1:8082"
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8082

    res: dict = {
        "status": "offline",
        "url": url,
        "host": host,
        "port": port,
        "health": None,
        "model": None,
        "metrics": None,
        "error": None,
    }

    try:
        conn = http.client.HTTPConnection(host, port, timeout=2)
        # /health is a fast liveness probe
        conn.request("GET", "/health")
        resp = conn.getresponse()
        body = resp.read().decode(errors="replace").strip()
        conn.close()
        res["health"] = body
        if resp.status != 200 or body != "OK":
            res["status"] = f"unhealthy-{resp.status}"
            return res

        # /metrics returns {in_flight, model, p50_ms, p99_ms, queue_depth}
        conn = http.client.HTTPConnection(host, port, timeout=2)
        conn.request("GET", "/metrics")
        mresp = conn.getresponse()
        if mresp.status == 200:
            try:
                metrics = json.loads(mresp.read().decode())
                res["metrics"] = metrics
                res["model"] = metrics.get("model")
            except json.JSONDecodeError:
                pass
        conn.close()

        res["status"] = "online"
    except (ConnectionRefusedError, OSError, http.client.HTTPException) as e:
        res["error"] = f"{type(e).__name__}: {e}"
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"

    return res
