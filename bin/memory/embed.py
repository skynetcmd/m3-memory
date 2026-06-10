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
# In-process Rust embedder (opt-in via M3_EMBED_GGUF)
# ──────────────────────────────────────────────────────────────────────────────
_EMBED_GGUF_PATH: str | None = (os.environ.get("M3_EMBED_GGUF") or "").strip() or None
_EMBED_GGUF_MODEL_TAG: str = (
    (os.environ.get("M3_EMBED_GGUF_MODEL_TAG") or "").strip()
    or "bge-m3-GGUF-Q4_K_M.gguf"
)
_embedded_embedder = None
_embedded_embed_checked = False


def _get_embedded_embedder():
    """Return the in-process EmbeddedEmbedder, or None if unavailable/unsafe."""
    global _embedded_embedder, _embedded_embed_checked
    if _embedded_embed_checked:
        return _embedded_embedder
    _embedded_embed_checked = True
    if config.m3_core_rs is None or _EMBED_GGUF_PATH is None:
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
# Per-call + bulk semaphores, dim-validation flag
# ──────────────────────────────────────────────────────────────────────────────
_EMBED_SEM = asyncio.Semaphore(4)
_EMBED_DIM_VALIDATED = False

EMBED_BULK_CHUNK = int(os.environ.get("EMBED_BULK_CHUNK", "1024"))
EMBED_BULK_CONCURRENCY = int(os.environ.get("EMBED_BULK_CONCURRENCY", "4"))
_EMBED_BULK_SEM = asyncio.Semaphore(EMBED_BULK_CONCURRENCY)


# ──────────────────────────────────────────────────────────────────────────────
# The cascade itself
# ──────────────────────────────────────────────────────────────────────────────
async def _embed(text: str) -> tuple[list[float] | None, str]:
    global _EMBED_DIM_VALIDATED
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
            if not _EMBED_DIM_VALIDATED:
                if len(vec) != config.EMBED_DIM:
                    logger.error(
                        f"Embedded embedding dim {len(vec)} != EMBED_DIM {config.EMBED_DIM}"
                    )
                _EMBED_DIM_VALIDATED = True
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
            if not _EMBED_DIM_VALIDATED:
                if len(emb) != config.EMBED_DIM:
                    logger.error(
                        f"CPU fallback embedding dim {len(emb)} != EMBED_DIM {config.EMBED_DIM}"
                    )
                _EMBED_DIM_VALIDATED = True
            if _CPU_FALLBACK_BREAKER is not None:
                _CPU_FALLBACK_BREAKER.record_success()
            _record_embed_backend("cpu-http-fallback", 1)
            return emb, _EMBED_GGUF_MODEL_TAG
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

                    if not _EMBED_DIM_VALIDATED:
                        if len(emb) != config.EMBED_DIM:
                            logger.error(
                                f"Embedding dimension mismatch: got {len(emb)}, "
                                f"expected EMBED_DIM={config.EMBED_DIM}. Update EMBED_DIM env var."
                            )
                        _EMBED_DIM_VALIDATED = True

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

                    if len(emb) != config.EMBED_DIM:
                        logger.error(f"Cloud Enclave embedding dim {len(emb)} != EMBED_DIM {config.EMBED_DIM}")

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
        ordered = sorted(data, key=lambda d: d.get("index", 0))
        vecs = [d["embedding"] for d in ordered]

        if len(vecs) == len(cloud_texts):
            _cloud_served = 0
            for idx, vec in zip(cloud_indices, vecs):
                if vec is not None:
                    if len(vec) != config.EMBED_DIM:
                        logger.error(f"Cloud Enclave embedding dim {len(vec)} != EMBED_DIM {config.EMBED_DIM}")
                    out[idx] = (vec, config.EMBED_MODEL)
                    _cloud_served += 1

            if _cloud_served:
                if _CLOUD_BREAKER is not None:
                    _CLOUD_BREAKER.record_success()
                _record_embed_backend("cloud-enclave", _cloud_served)
        else:
            logger.error(f"Cloud enclave returned {len(vecs)} vectors for {len(cloud_texts)} inputs")
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
            for idx, vec in zip(miss_indices, vecs):
                out[idx] = (vec, _EMBED_GGUF_MODEL_TAG)
            _record_embed_backend(_embedded_label(), len(miss_texts))
            return out  # type: ignore[return-value]
        except Exception as e:
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
        ordered = sorted(data, key=lambda d: d.get("index", 0))
        vecs = [d["embedding"] for d in ordered]
        if len(vecs) != len(miss_texts):
            raise RuntimeError(
                f"CPU fallback returned {len(vecs)} vectors for {len(miss_texts)} inputs"
            )
        for idx, vec in zip(miss_indices, vecs):
            out[idx] = (vec, _EMBED_GGUF_MODEL_TAG)
        _record_embed_backend("cpu-http-fallback", len(miss_texts))
        return out  # type: ignore[return-value]
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

    _last_embed_err: dict[str, str] = {"msg": ""}

    async def _post_once(chunk_texts: list[str]) -> list[list[float] | None] | None:
        try:
            resp = await client.post(
                f"{base_url}/embeddings",
                json={"model": model, "input": chunk_texts},
                headers={"Authorization": f"Bearer {token}"},
                timeout=_httpx.Timeout(config.CHROMA_CONNECT_T, read=config.EMBED_TIMEOUT_READ * 4),
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
        async with _EMBED_BULK_SEM:
            for attempt in range(3):
                result = await _post_once(chunk_texts)
                if result is not None:
                    return result
                if attempt < 2:
                    await asyncio.sleep(2 * (2 ** attempt))

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

    chunks = [
        miss_texts[i : i + EMBED_BULK_CHUNK]
        for i in range(0, len(miss_texts), EMBED_BULK_CHUNK)
    ]
    chunk_results = await asyncio.gather(*(_post_chunk(c) for c in chunks))

    global _EMBED_DIM_VALIDATED
    flat: list[list[float] | None] = []
    for cr in chunk_results:
        flat.extend(cr)
    _primary_served = 0
    for local_i, vec in enumerate(flat):
        if vec is not None and not _EMBED_DIM_VALIDATED:
            if len(vec) != config.EMBED_DIM:
                logger.error(
                    f"Embedding dimension mismatch: got {len(vec)}, expected {config.EMBED_DIM}"
                )
            _EMBED_DIM_VALIDATED = True
        out[miss_indices[local_i]] = (vec, model)
        if vec is not None:
            _primary_served += 1
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
