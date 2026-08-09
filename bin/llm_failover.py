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
from m3_sdk import getenv_compat

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


def is_ollama_url(url: str) -> bool:
    """True when ``url`` points at an Ollama server (its fixed default port 11434).

    Ollama's OpenAI-compatible endpoint honors ``reasoning_effort: "none"`` to turn
    OFF a reasoning model's thinking (verified live) — the answer then lands in
    ``content`` instead of the reasoning channel, so fact extraction actually
    parses. ``"none"`` is non-standard for OpenAI/LM Studio (they 400 on it), so
    callers send it only when this returns True."""
    try:
        from urllib.parse import urlparse
        return urlparse(url).port == 11434
    except Exception:  # noqa: BLE001
        return False


def suppresses_thinking_via_effort(url: str) -> bool:
    """True for local runtimes verified to honor ``reasoning_effort="none"`` to turn
    a reasoning model's thinking OFF: LM Studio (:1234) and Ollama (:11434). Both
    return 200 and empty the reasoning channel (so the answer lands in ``content``
    and fact extraction parses). Real OpenAI cloud 400s on ``"none"`` (only
    minimal/low/medium/high are valid), and other local servers (vLLM, …) are
    unverified — so callers send ``reasoning_effort="none"`` ONLY when this is True."""
    return is_lmstudio_url(url) or is_ollama_url(url)


def apply_thinking_suppression(payload: dict, url: str) -> dict:
    """Add ``reasoning_effort="none"`` to ``payload`` when ``url`` is a local
    runtime verified to honor it. Returns the same dict, for chaining.

    The one-liner every OpenAI-chat caller needs, so the guard has ONE symbol to
    check and callers stop hand-rolling the same try/except. Without it, a
    reasoning model spends its budget in the ``reasoning`` channel and returns
    ``finish_reason="length"`` with an EMPTY ``content`` — the caller reads "" as
    "the model said nothing" and the feature silently degrades to a no-op that
    looks like a clean result (measured 2026-08-09: the citation-drift judge
    abstained on 100% of syntheses and reported "0 findings").

    Never raises and never sends the key to a cloud endpoint: ``"none"`` is
    non-standard and 400s on real OpenAI. Pure dict manipulation — no I/O, no
    platform or database coupling.
    """
    try:
        if suppresses_thinking_via_effort(url):
            payload["reasoning_effort"] = "none"
    except Exception:  # noqa: BLE001 — a suppression hint must never break a call
        pass
    return payload


def is_lmstudio_url(url: str) -> bool:
    """True when ``url`` points at an LM Studio server (its default port 1234).

    LM Studio is the one local server where the Anthropic ``/v1/messages`` wire
    format buys something — prompt caching for a large reused system prompt. Every
    other local OpenAI-compatible server (Ollama, vLLM, llama.cpp, LocalAI) is
    better served by the universal ``/v1/chat/completions`` path: some don't
    implement ``/v1/messages`` at all, and those that do (e.g. current Ollama) gain
    no caching from it. Callers use this to keep Anthropic only where it pays off."""
    try:
        from urllib.parse import urlparse
        return urlparse(url).port == 1234
    except Exception:  # noqa: BLE001
        return False


def _flag(name: str, default: bool) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes")


_endpoints_csv = getenv_compat("M3_LLM_ENDPOINTS_CSV", "LLM_ENDPOINTS_CSV", "").strip()
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

# Sync discovery cache, keyed by ENDPOINT (not process-global): sync callers
# resolve their own endpoint first, so a model picked for another endpoint
# would be the wrong answer. See discover_model_sync.
_SYNC_MODEL_CACHE: dict = {}


def clear_failover_caches() -> None:
    """Forget all cached endpoints. Call after a persistent failure
    so the next discovery attempt probes the network."""
    global _LLM_ENDPOINT_CACHE, _SMALL_LLM_ENDPOINT_CACHE, _EMBED_ENDPOINT_CACHE
    global _EMBED_NEG_CACHE_TS
    _SYNC_MODEL_CACHE.clear()
    _LLM_ENDPOINT_CACHE = None
    _SMALL_LLM_ENDPOINT_CACHE = None
    _EMBED_ENDPOINT_CACHE = None
    _EMBED_NEG_CACHE_TS = 0.0


def clear_embed_cache() -> None:
    """Legacy helper for forgetting only the embed cache."""
    global _EMBED_ENDPOINT_CACHE, _EMBED_NEG_CACHE_TS
    _EMBED_ENDPOINT_CACHE = None
    _EMBED_NEG_CACHE_TS = 0.0


def _auth_headers(token: str | None) -> dict:
    """Authorization header, OMITTED ENTIRELY when there is no token.

    A junk or empty bearer is NOT equivalent to no bearer:

      * `Bearer ` (empty token) is an ILLEGAL HTTP header value — httpx raises
        LocalProtocolError before the request leaves the process. The callers
        below wrap discovery in a broad `except Exception`, so that surfaced as
        "endpoint unusable" and get_best_llm returned None against a perfectly
        healthy server. Observed 2026-07-26 against a live Ollama: every
        tokenless endpoint was silently undiscoverable.
      * A placeholder like "not-needed" earns a 401 from an auth-enabled
        LM Studio, which answers a MISSING header differently from a WRONG one.

    Mirrors files_memory.config.llm_auth_headers. One helper, three callers, so
    the three cannot drift apart again.
    """
    tok = (token or "").strip()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _default_lm_token() -> "str | None":
    """The default LM token when a caller passes none — resolved through the
    SHARED, headless-safe resolver (env -> keyring -> keychain -> vault, with the
    LM_STUDIO_API_KEY alias), NOT os.environ alone. Headless launchers (launchd/
    systemd/task) don't inherit the shell's LM_API_TOKEN (§3), so an env-only
    default 401s against an auth-enabled LM Studio in every background context —
    the exact failure mode that motivated m3_sdk.get_secret."""
    try:
        from auth_utils import get_api_key  # lazy: auth_utils vault reads route via M3Context
        return get_api_key("LM_API_TOKEN")
    except Exception:  # noqa: BLE001 — resolver failure -> env fallback
        return os.environ.get("LM_API_TOKEN")


def discover_model_sync(
    endpoint: str,
    token: Optional[str] = None,
    *,
    timeout: float = 5.0,
) -> Optional[str]:
    """Pick a model on ``endpoint`` by asking it, SYNCHRONOUSLY. No name needed.

    The sync sibling of :func:`get_best_llm`, for callers that are not async and
    cannot drive an httpx.AsyncClient — e.g. the files-store summarizer and
    extractor, which run a serial per-file loop.

    Exists so "which model?" has ONE answer per process shape rather than a
    hardcoded literal per call site. Before this, several callers defaulted to a
    specific model NAME (``qwen3-4b-instruct``, ``qwen/qwen3-coder-next``, …),
    which meant a box without that exact model got a 404 from the server rather
    than a graceful fall-through. The name should be an OVERRIDE, never the
    fallback.

    Differs from get_best_llm deliberately:
      * takes ONE endpoint the caller already resolved — it does NOT walk
        LLM_ENDPOINTS. The callers here have their own endpoint precedence
        (M3_FILES_SUMMARY_URL > M3_LMSTUDIO_URL > …) and picking a model for a
        DIFFERENT endpoint than the one they will POST to would be a bug.
      * caches per-endpoint rather than process-globally, for the same reason.

    ``token`` defaults to ``LM_API_TOKEN`` from the environment. When there is
    no token the Authorization header is OMITTED ENTIRELY rather than sent with
    a placeholder — mirroring files_memory.config.llm_auth_headers. A junk
    bearer token is not equivalent to no token: an auth-enabled LM Studio
    answers a missing header differently from a wrong one, and sending
    "not-needed" earns a 401 (observed 2026-07-26 against the live server).

    Returns the largest usable model id by :func:`parse_model_size`, or None if
    the endpoint is unreachable / lists nothing usable. None means "caller
    decides" — it must NOT be turned back into a hardcoded name.

    NOT FOR EMBEDDERS. This picks a GENERATIVE model and filters embedders out
    (EMBED_EXCLUSIONS). The store's embedder is deliberately a FIXED name
    (memory.config.EMBED_MODEL, default text-embedding-bge-m3): vectors from
    different models are not semantically comparable, so discovering an
    embedder at runtime would silently corrupt the vector space. Naming the
    model IS the correctness requirement there — the opposite of here.
    """
    cached = _SYNC_MODEL_CACHE.get(endpoint)
    if cached is not None:
        return cached
    try:
        import httpx

        tok = token if token is not None else _default_lm_token()
        headers = _auth_headers(tok)
        r = httpx.get(
            f"{endpoint.rstrip('/')}/models",
            headers=headers,
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001 — discovery is best-effort by contract
        logger.warning(f"[llm_failover] sync discovery {endpoint}: {type(e).__name__}: {e}")
        return None
    best = _pick_best_model(data)
    if best is None:
        logger.warning(f"[llm_failover] sync discovery {endpoint}: no usable models")
        return None
    _SYNC_MODEL_CACHE[endpoint] = best
    logger.info(f"[llm_failover] sync discovery {endpoint} -> {best}")
    return best


def _pick_best_model(data: dict) -> Optional[str]:
    """From a /models response, return the largest usable GENERATIVE model id, or
    None. Embedders (EMBED_EXCLUSIONS) and LLM_EXCLUSIONS are filtered out and the
    winner is the largest by :func:`parse_model_size`. Shared by
    discover_model_sync and discover_model_async so the two cannot drift (§10a).

    Both OpenAI-shaped servers and Ollama return {"data": [...]}; Ollama's native
    (non-/v1) listing uses {"models": [...]}. Accept either rather than asserting a
    shape we have not verified against every server."""
    models = data.get("data", data.get("models", [])) or []
    usable = []
    for m in models:
        mid = (m.get("id") or m.get("model", "")) if isinstance(m, dict) else str(m)
        if not mid:
            continue
        if any(x in mid.lower() for x in EMBED_EXCLUSIONS):
            continue
        if LLM_EXCLUSIONS and any(x in mid.lower() for x in LLM_EXCLUSIONS):
            continue
        usable.append(mid)
    if not usable:
        return None
    return max(usable, key=parse_model_size)


async def discover_model_async(
    client: "httpx.AsyncClient",
    endpoint: str,
    token: Optional[str] = None,
    *,
    timeout: float = 5.0,
    fresh: bool = False,
) -> Optional[str]:
    """Async sibling of :func:`discover_model_sync` — pick the largest usable
    model on ``endpoint`` by asking it, over the caller's AsyncClient so it does
    NOT block the event loop. Same filter/pick contract (shared _pick_best_model)
    and same token/auth handling (default LM_API_TOKEN, header omitted when
    absent).

    ``fresh=True`` bypasses the per-endpoint cache and re-queries every call — for
    a long-running loop that must follow a model swapped mid-run (e.g. the entity
    extractor's per-pass resolution). ``fresh=False`` (default) caches per-endpoint
    like the sync version. Returns None when unreachable / nothing usable —
    "caller decides"; never turn None back into a hardcoded name."""
    if not fresh:
        cached = _SYNC_MODEL_CACHE.get(endpoint)
        if cached is not None:
            return cached
    try:
        tok = token if token is not None else _default_lm_token()
        headers = _auth_headers(tok)
        r = await client.get(f"{endpoint.rstrip('/')}/models", headers=headers, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001 — discovery is best-effort by contract
        logger.warning(f"[llm_failover] async discovery {endpoint}: {type(e).__name__}: {e}")
        return None
    best = _pick_best_model(data)
    if best is None:
        logger.warning(f"[llm_failover] async discovery {endpoint}: no usable models")
        return None
    if not fresh:
        _SYNC_MODEL_CACHE[endpoint] = best
    logger.info(f"[llm_failover] async discovery {endpoint} -> {best}")
    return best


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
                headers=_auth_headers(token),
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

        # VERIFIED 2026-07-26 against live Ollama 0.31.2: its /v1/models does
        # return {"data": [...]} with an "id" field, same as OpenAI/LM Studio.
        # The "models" fallback covers Ollama's NATIVE /api/tags shape, which we
        # do not call — kept as cheap insurance for a non-/v1 endpoint.
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
                headers=_auth_headers(token),
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
                headers=_auth_headers(token),
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
