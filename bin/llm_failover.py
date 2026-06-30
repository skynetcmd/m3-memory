"""
LLM Failover Module

Cross-machine failover strategy for selecting LLM and embedding models.
Tries endpoints in order: LM Studio (local + remote), then Ollama.
Used by custom_tool_bridge.py and memory_bridge.py.
"""

import logging
import os
import re
import time
from typing import Optional

import httpx

logger = logging.getLogger("llm_failover")

# Failover order. We only probe endpoints the user actually opts into — a probe
# to an unconfigured provider is not free on every platform (a connect to a
# non-listening localhost port can block up to the full connect timeout rather
# than failing in <1ms, e.g. on Windows), and that cost is paid on every
# discovery for a provider the user may not run.
#
# Two built-in local endpoints, each independently toggleable so neither
# single-provider group pays for the other's probe:
#   - LM Studio (:1234) — ON by default (most common). Disable with
#     M3_ENABLE_LMSTUDIO_FAILOVER=0 (e.g. Ollama-only users).
#   - Ollama (:11434)   — OFF by default. Enable with
#     M3_ENABLE_OLLAMA_FAILOVER=1.
# Running your own server (llama.cpp, vLLM, LocalAI, a remote box, …)? Point at it
# with M3_LLM_URL — a single OpenAI-compatible /v1 base URL, tried FIRST. Setting it
# also turns OFF the LM Studio default probe (you've told us your endpoint, so we
# don't also probe :1234) unless you explicitly re-enable it. Example:
#   M3_LLM_URL="http://localhost:8080/v1"        # llama-server
#   M3_LLM_URL="http://gpu-box.local:8000/v1"    # remote vLLM
#
# Or take full control (overrides M3_LLM_URL and both toggles) with LLM_ENDPOINTS_CSV
# — the path for an ordered multi-endpoint failover / multi-machine LAN, e.g.:
#   LLM_ENDPOINTS_CSV="http://localhost:8080/v1,http://gpu-box.local:8000/v1"
_LMSTUDIO_ENDPOINT = "http://localhost:1234/v1"
_OLLAMA_ENDPOINT = "http://localhost:11434/v1"


def _flag(name: str, default: bool) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes")


_endpoints_csv = os.environ.get("LLM_ENDPOINTS_CSV", "").strip()
_custom_url = os.environ.get("M3_LLM_URL", "").strip()
if _endpoints_csv:
    # Explicit ordered list — full control, overrides everything below.
    LLM_ENDPOINTS = [ep.strip() for ep in _endpoints_csv.split(",") if ep.strip()]
else:
    LLM_ENDPOINTS = []
    if _custom_url:
        LLM_ENDPOINTS.append(_custom_url)
    # A custom URL implies "this is my server" — don't auto-probe LM Studio unless
    # the user explicitly opted in. With no custom URL, LM Studio stays on by default.
    if _flag("M3_ENABLE_LMSTUDIO_FAILOVER", default=not _custom_url):
        if _LMSTUDIO_ENDPOINT not in LLM_ENDPOINTS:
            LLM_ENDPOINTS.append(_LMSTUDIO_ENDPOINT)
    if _flag("M3_ENABLE_OLLAMA_FAILOVER", False):
        if _OLLAMA_ENDPOINT not in LLM_ENDPOINTS:
            LLM_ENDPOINTS.append(_OLLAMA_ENDPOINT)

# Model filtering patterns
EMBED_EXCLUSIONS = ("embed", "nomic", "jina", "bge", "minilm", "e5")
LLM_EXCLUSIONS = ()  # nothing excluded for LLM selection — embedding models filtered

# Timeouts
# Connect timeout is deliberately short. On Linux a refused loopback connection
# returns in <1ms, but on Windows a connect to a non-listening localhost port can
# block up to the full timeout — so keep this small to bound the cost of probing
# any absent endpoint. Mainly caps remote LAN endpoints set via LLM_ENDPOINTS_CSV.
# Override with M3_LLM_CONNECT_TIMEOUT (seconds) for slow LAN links.
try:
    CONNECT_TIMEOUT = float(os.environ.get("M3_LLM_CONNECT_TIMEOUT", "0.3"))
except ValueError:
    CONNECT_TIMEOUT = 0.3
READ_TIMEOUT = 10.0      # for model list fetches only

# Process-global caches for discovery. Discovered once on first call; reused
# for all subsequent calls in the same process. Reset by process exit or by
# calling clear_failover_caches().
# Avoids a GET /v1/models roundtrip before every LLM/embed call, which
# otherwise dominates per-call wall time and generates excessive log noise.
_LLM_ENDPOINT_CACHE: Optional[tuple[str, str]] = None
_SMALL_LLM_ENDPOINT_CACHE: Optional[tuple[str, str]] = None
_EMBED_ENDPOINT_CACHE: Optional[tuple[str, str]] = None
# Negative-result cache for embed discovery. Without this, a host with no
# embedding model loaded would re-run the GET /v1/models probe of EVERY endpoint
# on EVERY embed call (the positive cache only short-circuits a SUCCESS) — its
# own per-call request storm. Remember a "no embed endpoint" result for
# _EMBED_NEG_TTL seconds, then re-probe (so it recovers when a model is loaded).
_EMBED_NEG_CACHE_TS: float = 0.0
_EMBED_NEG_TTL: float = float(os.environ.get("M3_EMBED_DISCOVERY_NEG_TTL", "60"))


def clear_failover_caches() -> None:
    """Forget all cached endpoints. Call after a persistent failure
    so the next discovery attempt probes the network."""
    global _LLM_ENDPOINT_CACHE, _SMALL_LLM_ENDPOINT_CACHE, _EMBED_ENDPOINT_CACHE
    global _EMBED_NEG_CACHE_TS
    _LLM_ENDPOINT_CACHE = None
    _SMALL_LLM_ENDPOINT_CACHE = None
    _EMBED_ENDPOINT_CACHE = None
    _EMBED_NEG_CACHE_TS = 0.0


def clear_embed_cache() -> None:
    """Legacy helper for forgetting only the embed cache."""
    global _EMBED_ENDPOINT_CACHE, _EMBED_NEG_CACHE_TS
    _EMBED_ENDPOINT_CACHE = None
    _EMBED_NEG_CACHE_TS = 0.0


def parse_model_size(model_id: str) -> float:
    """
    Extract model size from model identifier.

    Supports patterns: 70b, 32b, 8b, 1.5b, 0.5b, 235a22b (MoE — uses active params)

    Args:
        model_id: Model identifier string

    Returns:
        Float size in billions (e.g., 70b → 70.0, 500m → 0.5), or 0.0 if unparseable
    """
    model_id_lower = model_id.lower()

    # MoE pattern first: NNNaNNb (e.g. 235a22b) — use total params (first number)
    moe_match = re.search(r'(\d+)a\d+b', model_id_lower)
    if moe_match:
        return float(moe_match.group(1))

    # Standard pattern: digits.digits + (b|m) or digits + b|m
    # Examples: 70b, 1.5b, 500m
    match = re.search(r'(\d+(?:\.\d+)?)\s*([bm])', model_id_lower)

    if not match:
        return 0.0

    size_val = float(match.group(1))
    unit = match.group(2)

    # Convert to billions
    if unit == 'm':
        return size_val / 1000.0  # 500m → 0.5b
    else:  # unit == 'b'
        return size_val


async def get_best_llm(client: httpx.AsyncClient, token: str) -> Optional[tuple[str, str]]:
    """
    Find the largest available LLM model across endpoints.

    Iterates LLM_ENDPOINTS in failover order. Filters out embedding models.
    Returns first endpoint with a usable model, selecting the largest by parse_model_size().
    Result is cached process-globally.

    Args:
        client: httpx.AsyncClient for making requests
        token: Bearer token for API authentication

    Returns:
        Tuple of (base_url, model_id) or None if no usable models found
    """
    global _LLM_ENDPOINT_CACHE
    if _LLM_ENDPOINT_CACHE is not None:
        return _LLM_ENDPOINT_CACHE

    for endpoint in LLM_ENDPOINTS:
        try:
            response = await client.get(
                f"{endpoint}/models",
                headers={"Authorization": f"Bearer {token}"},
                timeout=httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT),
            )
            response.raise_for_status()

        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning(f"[llm_failover] {endpoint}: {type(e).__name__}")
            continue
        except httpx.HTTPStatusError as e:
            logger.warning(f"[llm_failover] {endpoint}: HTTPStatusError {e.response.status_code}")
            continue
        except Exception as e:
            logger.warning(f"[llm_failover] {endpoint}: {type(e).__name__}: {e}")
            continue

        # Parse response — handle both OpenAI and Ollama formats
        try:
            data = response.json()
        except Exception as e:
            logger.warning(f"[llm_failover] {endpoint}: Failed to parse JSON: {e}")
            continue

        # Both OpenAI and Ollama /v1/models return {"data": [...]}
        models = data.get("data", data.get("models", []))

        if not models:
            continue

        # Filter out embedding models and LLM exclusions
        usable_models = []
        for model in models:
            model_id = model.get("id") or model.get("model", "")

            # Skip if matches embedding exclusions
            if any(excl in model_id.lower() for excl in EMBED_EXCLUSIONS):
                continue

            # Skip if matches LLM exclusions
            if LLM_EXCLUSIONS and any(excl in model_id.lower() for excl in LLM_EXCLUSIONS):
                continue

            usable_models.append(model_id)

        if not usable_models:
            continue

        # Find largest model by size
        best_model = max(usable_models, key=lambda m: parse_model_size(m))
        _LLM_ENDPOINT_CACHE = (endpoint, best_model)
        return _LLM_ENDPOINT_CACHE

    return None


async def get_smallest_llm(
    client: httpx.AsyncClient,
    token: str,
    min_size_b: float = 0.5,
) -> Optional[tuple[str, str]]:
    """
    Find the smallest available LLM model across endpoints, subject to a size floor.

    Mirrors `get_best_llm` but selects the smallest model by `parse_model_size`,
    ignoring models whose parsed size is below ``min_size_b`` (default 0.5B).
    Result is cached process-globally.

    Args:
        client: httpx.AsyncClient for making requests
        token: Bearer token for API authentication
        min_size_b: Minimum parsed model size in billions (default 0.5)

    Returns:
        Tuple of (base_url, model_id) or None if no usable models found
    """
    global _SMALL_LLM_ENDPOINT_CACHE
    if _SMALL_LLM_ENDPOINT_CACHE is not None:
        return _SMALL_LLM_ENDPOINT_CACHE

    for endpoint in LLM_ENDPOINTS:
        try:
            response = await client.get(
                f"{endpoint}/models",
                headers={"Authorization": f"Bearer {token}"},
                timeout=httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT),
            )
            response.raise_for_status()

        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning(f"[llm_failover] {endpoint}: {type(e).__name__}")
            continue
        except httpx.HTTPStatusError as e:
            logger.warning(f"[llm_failover] {endpoint}: HTTPStatusError {e.response.status_code}")
            continue
        except Exception as e:
            logger.warning(f"[llm_failover] {endpoint}: {type(e).__name__}: {e}")
            continue

        try:
            data = response.json()
        except Exception as e:
            logger.warning(f"[llm_failover] {endpoint}: Failed to parse JSON: {e}")
            continue

        models = data.get("data", data.get("models", []))
        if not models:
            continue

        sized_models: list[tuple[float, str]] = []
        for model in models:
            model_id = model.get("id") or model.get("model", "")
            if any(excl in model_id.lower() for excl in EMBED_EXCLUSIONS):
                continue
            if LLM_EXCLUSIONS and any(excl in model_id.lower() for excl in LLM_EXCLUSIONS):
                continue
            size = parse_model_size(model_id)
            if size >= min_size_b:
                sized_models.append((size, model_id))

        if not sized_models:
            continue

        smallest = min(sized_models, key=lambda t: t[0])[1]
        _SMALL_LLM_ENDPOINT_CACHE = (endpoint, smallest)
        return _SMALL_LLM_ENDPOINT_CACHE

    return None


async def get_best_embed(client: httpx.AsyncClient, token: str) -> Optional[tuple[str, str]]:
    """
    Find an embedding model across endpoints, with fallback to any available model.

    Iterates LLM_ENDPOINTS in failover order, returning the first endpoint that
    advertises an EMBEDDING model (prefers BGE-M3). Returns None if no endpoint
    serves an embedding model — a chat-only endpoint is NOT a valid fallback
    (POSTing /embeddings to it 400s), so the caller takes its own embed fallback.

    Result is cached process-globally after first successful discovery. Reset
    via clear_embed_cache() on persistent failure.

    Args:
        client: httpx.AsyncClient for making requests
        token: Bearer token for API authentication

    Returns:
        Tuple of (base_url, model_id) or None if no models found anywhere
    """
    global _EMBED_ENDPOINT_CACHE, _EMBED_NEG_CACHE_TS
    if _EMBED_ENDPOINT_CACHE is not None:
        return _EMBED_ENDPOINT_CACHE
    # Negative cache: a recent "no embed endpoint" result short-circuits the
    # all-endpoints /models probe so a host without an embedding model doesn't
    # re-probe on every single embed call.
    if _EMBED_NEG_CACHE_TS and (time.time() - _EMBED_NEG_CACHE_TS) < _EMBED_NEG_TTL:
        return None

    for endpoint in LLM_ENDPOINTS:
        try:
            response = await client.get(
                f"{endpoint}/models",
                headers={"Authorization": f"Bearer {token}"},
                timeout=httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT),
            )
            response.raise_for_status()

        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning(f"[llm_failover] {endpoint}: {type(e).__name__}")
            continue
        except httpx.HTTPStatusError as e:
            logger.warning(f"[llm_failover] {endpoint}: HTTPStatusError {e.response.status_code}")
            continue
        except Exception as e:
            logger.warning(f"[llm_failover] {endpoint}: {type(e).__name__}: {e}")
            continue

        # Parse response
        try:
            data = response.json()
        except Exception as e:
            logger.warning(f"[llm_failover] {endpoint}: Failed to parse JSON: {e}")
            continue

        models = data.get("data", data.get("models", []))

        if not models:
            continue

        # Separate embedding and LLM models
        embed_models = []
        other_models = []

        for model in models:
            model_id = model.get("id") or model.get("model", "")

            # Check if it's an embedding model
            is_embed = any(excl in model_id.lower() for excl in EMBED_EXCLUSIONS)

            if is_embed:
                embed_models.append(model_id)
            else:
                other_models.append(model_id)

        # Prefer embedding model from this endpoint. When the endpoint
        # advertises several embed models, pick BGE-M3 — it is m3-memory's
        # canonical embedder; a blind `[0]` could otherwise select a retired
        # model (e.g. qwen3-embedding), producing vectors that are
        # semantically incomparable to the rest of the store.
        if embed_models:
            embed_models.sort(key=lambda m: 0 if "bge-m3" in m.lower() else 1)
            _EMBED_ENDPOINT_CACHE = (endpoint, embed_models[0])
            return _EMBED_ENDPOINT_CACHE

    # No endpoint advertised an EMBEDDING model. Do NOT fall back to a chat-only
    # endpoint: POSTing /embeddings to a chat model returns 400 on every call,
    # and the caller would retry it into a storm. Return None so the caller takes
    # its real fallback (CPU embed server / cloud / graceful skip) instead of
    # hammering an endpoint that structurally cannot embed — fail cleanly rather
    # than returning a known-doomed target. Stamp the negative cache so we don't
    # re-probe every call for the next _EMBED_NEG_TTL seconds.
    _EMBED_NEG_CACHE_TS = time.time()
    logger.warning(
        "[llm_failover] no embedding model found across endpoints — returning "
        "None so the caller uses its embed fallback (negative-cached %.0fs).",
        _EMBED_NEG_TTL)
    return None
