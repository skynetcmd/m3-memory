"""
LLM Failover Module

Cross-machine failover strategy for selecting LLM and embedding models.
Tries endpoints in order: LM Studio (local + remote), then Ollama.
Used by custom_tool_bridge.py and memory_bridge.py.
"""

import os
import httpx
import logging
import re
from typing import Optional

logger = logging.getLogger("llm_failover")

# Failover order — LM Studio (OpenAI-compat on :1234), Ollama (:11434 via /v1 compat)
# Default: single-machine setup (localhost only).
# For multi-machine LAN failover, set LLM_ENDPOINTS_CSV env var, e.g.:
#   LLM_ENDPOINTS_CSV="http://localhost:1234/v1,http://laptop.local:1234/v1,http://desktop.local:1234/v1"
_DEFAULT_LLM_ENDPOINTS = [
    "http://localhost:1234/v1",
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
CONNECT_TIMEOUT = 3.0    # fail fast on unreachable hosts
READ_TIMEOUT = 10.0      # for model list fetches only


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

    Args:
        client: httpx.AsyncClient for making requests
        token: Bearer token for API authentication

    Returns:
        Tuple of (base_url, model_id) or None if no usable models found
    """
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
        return (endpoint, best_model)

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
    The floor avoids picking tiny/broken models (e.g. unparseable 0.0) and keeps
    enrichment quality usable. Models with unparseable size (0.0) are skipped.

    Intended for cheap ingest-time enrichment: auto-titling, entity tagging,
    session gists. Callers should be prepared for None if no endpoint is reachable.

    Args:
        client: httpx.AsyncClient for making requests
        token: Bearer token for API authentication
        min_size_b: Minimum parsed model size in billions (default 0.5)

    Returns:
        Tuple of (base_url, model_id) or None if no usable models found
    """
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
        return (endpoint, smallest)

    return None


async def get_best_embed(client: httpx.AsyncClient, token: str) -> Optional[tuple[str, str]]:
    """
    Find an embedding model across endpoints, with fallback to any available model.

    Iterates LLM_ENDPOINTS in failover order. Prefers models matching EMBED_EXCLUSIONS.
    Falls back to first endpoint with any usable model if no embed model found.

    Args:
        client: httpx.AsyncClient for making requests
        token: Bearer token for API authentication

    Returns:
        Tuple of (base_url, model_id) or None if no models found anywhere
    """
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
            return (endpoint, embed_models[0])

        # Record fallback if this endpoint has any usable model
        if other_models and fallback_result is None:
            fallback_result = (endpoint, other_models[0])

    return fallback_result
