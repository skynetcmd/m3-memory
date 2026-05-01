"""
LLM Failover Module

Cross-machine failover strategy for selecting LLM and embedding models.
Tries endpoints in order: LM Studio (local + remote), then Ollama.
Used by custom_tool_bridge.py and memory_bridge.py.
"""

import logging
import os
import re
from typing import Optional

import httpx

logger = logging.getLogger("llm_failover")

# Failover order — LM Studio (OpenAI-compat on :1234), Ollama (:11434 via /v1 compat).
# Both localhost endpoints are probed in order; unreachable ones are skipped
# gracefully by get_best_llm / get_best_embed (they continue on ConnectError).
# This keeps Ollama-only users working out of the box — the prior LM-Studio-only
# default required LLM_ENDPOINTS_CSV to be set by hand before Ollama could be
# reached (see plan memory 776d3729 task #6).
#
# For multi-machine LAN failover, set LLM_ENDPOINTS_CSV env var, e.g.:
#   LLM_ENDPOINTS_CSV="http://localhost:1234/v1,http://laptop.local:1234/v1,http://desktop.local:1234/v1"
_DEFAULT_LLM_ENDPOINTS = [
    "http://localhost:1234/v1",
    "http://localhost:11434/v1",
]

_endpoints_csv = os.environ.get("LLM_ENDPOINTS_CSV", "").strip()
if _endpoints_csv:
    LLM_ENDPOINTS = [ep.strip() for ep in _endpoints_csv.split(",") if ep.strip()]
else:
    LLM_ENDPOINTS = _DEFAULT_LLM_ENDPOINTS

# Model filtering patterns
EMBED_EXCLUSIONS = ("embed", "nomic", "jina", "bge", "minilm", "e5")
LLM_EXCLUSIONS = ()  # nothing excluded for LLM selection — embedding models filtered

# Timeouts
# Connect timeout is deliberately short: the default endpoint list now includes
# both LM Studio (:1234) and Ollama (:11434). Single-provider users pay the
# connect timeout once per probe for the absent one, so fast-fail is important.
# On a loopback connection refused comes back in <1ms anyway; this mainly
# caps remote LAN endpoints set via LLM_ENDPOINTS_CSV.
CONNECT_TIMEOUT = 1.0
READ_TIMEOUT = 10.0      # for model list fetches only

# Process-global caches for discovery. Discovered once on first call; reused
# for all subsequent calls in the same process. Reset by process exit or by
# calling clear_failover_caches().
# Avoids a GET /v1/models roundtrip before every LLM/embed call, which
# otherwise dominates per-call wall time and generates excessive log noise.
_LLM_ENDPOINT_CACHE: Optional[tuple[str, str]] = None
_SMALL_LLM_ENDPOINT_CACHE: Optional[tuple[str, str]] = None
_EMBED_ENDPOINT_CACHE: Optional[tuple[str, str]] = None


def clear_failover_caches() -> None:
    """Forget all cached endpoints. Call after a persistent failure
    so the next discovery attempt probes the network."""
    global _LLM_ENDPOINT_CACHE, _SMALL_LLM_ENDPOINT_CACHE, _EMBED_ENDPOINT_CACHE
    _LLM_ENDPOINT_CACHE = None
    _SMALL_LLM_ENDPOINT_CACHE = None
    _EMBED_ENDPOINT_CACHE = None


def clear_embed_cache() -> None:
    """Legacy helper for forgetting only the embed cache."""
    global _EMBED_ENDPOINT_CACHE
    _EMBED_ENDPOINT_CACHE = None


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

    Iterates LLM_ENDPOINTS in failover order. Prefers models matching EMBED_EXCLUSIONS.
    Falls back to first endpoint with any usable model if no embed model found.

    Result is cached process-globally after first successful discovery. Reset
    via clear_embed_cache() on persistent failure.

    Args:
        client: httpx.AsyncClient for making requests
        token: Bearer token for API authentication

    Returns:
        Tuple of (base_url, model_id) or None if no models found anywhere
    """
    global _EMBED_ENDPOINT_CACHE
    if _EMBED_ENDPOINT_CACHE is not None:
        return _EMBED_ENDPOINT_CACHE

    fallback_result = None

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

        # Prefer embedding model from this endpoint
        if embed_models:
            _EMBED_ENDPOINT_CACHE = (endpoint, embed_models[0])
            return _EMBED_ENDPOINT_CACHE

        # Record fallback if this endpoint has any usable model
        if other_models and fallback_result is None:
            fallback_result = (endpoint, other_models[0])

    if fallback_result is not None:
        _EMBED_ENDPOINT_CACHE = fallback_result
    return fallback_result
