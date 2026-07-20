"""Health-panel data collector for the m3 dashboard.

Gathers, in ONE backend-agnostic place, the same signals `m3 doctor` reports —
so the dashboard's System Health view and the CLI doctor stay in agreement
(one source of truth, DESIGN_PHILOSOPHIES §3). Everything is best-effort: a
probe that can't run yields a degraded/None field, never an exception, so a
single unhealthy subsystem can't blank the whole panel.

Returns plain dicts (JSON-friendly) so the caller renders HTML; this module
holds NO presentation. Backend identity/counts go through the storage-backend
seam (active_backend / dialect), so a future backend (MariaDB, …) is picked up
with no change here.
"""
from __future__ import annotations

import os
import re
from typing import Any


def _fmt_dual_time(value: "object") -> str:
    """'LOCAL (ZULU)' timestamp — mirrors sections._fmt_dual_time (house convention)."""
    import datetime as _dt

    if value is None or value == "":
        return "—"
    dt = None
    try:
        if isinstance(value, _dt.datetime):
            dt = value
        elif isinstance(value, (int, float)):
            dt = _dt.datetime.fromtimestamp(float(value))
        else:
            dt = _dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, OSError, OverflowError):
        return str(value)
    if dt is None:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    local = dt.astimezone()
    raw_tz = local.strftime("%Z")
    if raw_tz and " " in raw_tz:
        tzname = "".join(w[0] for w in raw_tz.split() if w).upper()
    else:
        tzname = raw_tz or local.strftime("%z")
    utc = dt.astimezone(_dt.timezone.utc)
    return (f"{local.strftime('%Y-%m-%d %H:%M:%S')} {tzname} "
            f"({utc.strftime('%Y-%m-%dT%H:%M:%SZ')})")


def _verdict(inference: "dict | None" = None, pipeline: "dict | None" = None) -> dict:
    """Overall health verdict + the SPECIFIC reasons it isn't healthy.

    Returns {verdict, label, tone, headline, reasons}. ``verdict`` is the raw
    status_summary value (healthy/degraded/broken) kept for the contract; ``label``
    is the USER-FACING word chosen from the actual cause — deliberately NOT
    "DEGRADED" for a mere performance/throttle state ("degraded" wrongly implies
    data-integrity loss). Mapping:
      * genuinely not installed / broken → "NEEDS SETUP" (tone=bad)
      * inference backend down/empty WHILE a pipeline is backlogged → "INFERENCE
        BACKEND DOWN" (tone=bad — real work is stuck, not just slow)
      * load-throttled (governor CPU/RAM/GPU over threshold) → "THROTTLED (<res>)"
      * slower embedder tier only → "REDUCED PERFORMANCE"
      * otherwise healthy → "HEALTHY"
    ``tone`` ∈ {ok, warn, bad} drives the color; a throttle/perf state is warn
    (amber), never bad (red) — nothing is wrong with the data. An inference-backend
    stall IS bad (red): the LLM the loop needs is unreachable, so a backlog cannot
    drain until the user acts — that is worth alarming on, unlike a slow tier.
    ``inference``/``pipeline`` are the already-collected blocks (passed in to avoid
    re-probing); when omitted the inference-stall check is skipped.
    """
    try:
        from m3_memory.installer import status_summary
        s = status_summary()
    except Exception as e:  # noqa: BLE001
        return {"verdict": "unknown", "label": "UNKNOWN", "tone": "warn",
                "headline": f"status unavailable: {e}", "reasons": []}

    verdict = s.get("verdict", "unknown")
    reasons: list[str] = []

    # Broken/uninstalled is the only genuinely-bad state.
    if verdict == "broken" or not s.get("installed", True):
        reasons.append("m3 payload is not installed — run `m3 setup`.")
        return {"verdict": verdict, "label": "NEEDS SETUP", "tone": "bad",
                "headline": s.get("headline", ""), "reasons": reasons}

    # PRIMARY "why": live load-throttle from the governor. When the governor is
    # pacing background work because a resource is over threshold, THAT is the
    # honest reason for any slowness — name the pinned resource(s) and their %.
    throttled_res: list[str] = []
    try:
        from m3_sdk import resolve_db_path

        from dashboard.queue_stats import collect_governor
        gov = collect_governor(resolve_db_path(None))
        if gov.get("available") and str(gov.get("mode", "")).upper() in ("THROTTLED", "HALTED"):
            init = float(gov.get("initial_threshold", 80) or 80)
            pinned: list[str] = []
            for res, key in (("GPU", "gpu"), ("CPU", "cpu"), ("RAM", "ram")):
                try:
                    val = float(gov.get(key, 0) or 0)
                except (TypeError, ValueError):
                    continue
                if val >= init:
                    throttled_res.append(res)
                    pinned.append(f"{res} {val:.0f}%")
            if not throttled_res:  # throttled but no single resource pinned
                throttled_res.append("load")
            detail = f" ({', '.join(pinned)})" if pinned else ""
            reasons.append(
                f"Background work is being paced by the governor because "
                f"{'/'.join(throttled_res)} load is high{detail}. Interactive use "
                "is unaffected; queued work simply drains more slowly until load eases.")
    except Exception:  # noqa: BLE001 — governor telemetry is optional
        pass

    # Embedder note ONLY when it actually matters: a real embedding BACKLOG (many
    # rows still unembedded). The tier being "pure-Python" is NOT itself a problem
    # when embedding is caught up, and adding the native tier is NOT the fix for a
    # load throttle — so we do not surface the tier as a reason/remedy by default.
    embedder = str(s.get("embedder", ""))
    unembedded = _unembedded_count()
    if embedder.startswith("pure-Python") and unembedded > 200:
        reasons.append(
            f"{unembedded:,} items are still awaiting embedding and the current "
            "embedder is the pure-Python (HTTP) tier — the backlog will clear, "
            "just slowly. The native tier (`m3 embedder install-gpu`) speeds it up.")

    if s.get("chatlog") == "unreadable":
        reasons.append("Chatlog DB is unreadable — capture may be failing; "
                       "check `m3 chatlog status`.")

    # Inference-backend stall: the cognitive loop / entity extraction / enrichment
    # need an LLM at the configured endpoint. If that backend is DOWN or has NO
    # model loaded WHILE a pipeline is genuinely backlogged, queued work cannot
    # drain until the user acts — a real, actionable problem (red), not mere
    # slowness. A dead backend with no backlog is not worth alarming on (nothing is
    # stuck), so we gate on an actual backlog.
    inference_stall = False
    failover_active = False
    inf_status = (inference or {}).get("status")
    if inf_status in ("down", "no_model", "auth_failed", "unknown"):
        backlogged = False
        for pl in (pipeline or {}).get("pipelines", []):
            try:
                if int(pl.get("queue_len", 0) or 0) > 0 and "drained" not in str(pl.get("eta_human", "")).lower():
                    backlogged = True
                    break
            except (TypeError, ValueError):
                continue
        if backlogged:
            remedy = inference.get("remedy", "")
            if inf_status == "down":
                inference_stall = True  # red — definitely stuck
                why = "unreachable"
            elif inf_status == "no_model":
                inference_stall = True  # red — definitely stuck
                why = "up but has no model loaded"
            elif inf_status == "auth_failed":
                inference_stall = True  # red — m3's own calls are being rejected too
                why = "rejecting m3's credentials"
            else:  # unknown — model state unverifiable; warn, don't alarm red
                why = "reachable but its model state could not be verified"
            reasons.insert(0, f"Inference backend (LLM/SLM) is {why} — background "
                           f"pipelines are backlogged and cannot drain. {remedy}")
    elif inf_status == "failover_active":
        # Working, but the PREFERRED endpoint(s) failed and m3 fell through to a
        # secondary. Always worth a warn (even with no backlog) — the operator
        # should know the primary is down before the secondary also fails.
        failover_active = True
        reasons.insert(0, (inference or {}).get(
            "remedy", "LLM failover is active — a preferred endpoint is down."))

    # Choose the least-alarming accurate label from the real cause.
    if inference_stall:
        label = "INFERENCE BACKEND DOWN"
        tone = "bad"
    elif failover_active:
        label = "LLM FAILOVER ACTIVE"
        tone = "warn"
    elif throttled_res:
        label = f"THROTTLED ({'/'.join(throttled_res)})"
        tone = "warn"
    elif reasons:
        # A non-throttle reason survived (e.g. an embedding backlog) — reduced
        # throughput, not broken data.
        label = "REDUCED PERFORMANCE"
        tone = "warn"
    else:
        # No live problem worth flagging (a caught-up pure-Python tier is fine).
        label = "HEALTHY"
        tone = "ok"

    # status_summary's headline LEADS with the raw verdict word ("DEGRADED · …");
    # the pill already shows the (friendlier) label, so strip that leading token
    # to avoid re-introducing "DEGRADED" beside a "THROTTLED" pill. Keep the facts.
    headline = str(s.get("headline", ""))
    for raw in ("HEALTHY", "DEGRADED", "BROKEN"):
        if headline.upper().startswith(raw):
            headline = headline[len(raw):].lstrip(" ·-—").strip()
            break
    return {"verdict": verdict, "label": label, "tone": tone,
            "headline": headline, "reasons": reasons}


# Human-facing backend names (tall-man / correct casing). The seam uses lowercase
# identifiers; map them for display. Unknown backends fall back to a title-cased
# form so a future engine still reads sensibly.
_BACKEND_DISPLAY = {"sqlite": "SQLite", "postgres": "PostgreSQL",
                    "postgresql": "PostgreSQL", "mariadb": "MariaDB", "mysql": "MySQL"}


def _backend_display(name: str) -> str:
    return _BACKEND_DISPLAY.get((name or "").lower(), (name or "unknown").title())


def _unembedded_count() -> int:
    """Count live memory_items lacking an embedding (the real 'is embedding
    behind?' signal). Best-effort, read-only; returns 0 on any error. Backend-
    blind via the active backend's connection."""
    try:
        from memory.db import _db
        with _db() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM memory_items mi WHERE COALESCE(mi.is_deleted,0)=0 "
                "AND NOT EXISTS (SELECT 1 FROM memory_embeddings me WHERE me.memory_id=mi.id)"
            ).fetchone()
            return int(row[0]) if row else 0
    except Exception:  # noqa: BLE001
        return 0


def _backend_label_for_endpoint(url: str) -> str:
    """Human name for an LLM endpoint URL, provider-agnostic. m3 talks to several
    OpenAI/Anthropic-compatible local servers; name the well-known ones and fall
    back to host:port for anything custom/remote (M3_LLM_URL, LAN vLLM, …)."""
    u = (url or "").lower()
    if ":1234" in u:
        return "LM Studio"
    if ":11434" in u:
        return "Ollama"
    # Strip scheme + trailing /v1 for a compact "host:port" custom label.
    host = re.sub(r"^https?://", "", url or "").rstrip("/")
    host = re.sub(r"/v1$", "", host)
    return host or "custom endpoint"


def _llm_token() -> str:
    """The SAME token m3 itself sends to the local LLM, so the health probe sees
    exactly what the real call path sees (auth mismatch = false 401s otherwise).
    Mirrors memory_core / custom_tool_bridge: `ctx.get_secret("LM_API_TOKEN")`
    (→ auth_utils.get_api_key: env → keyring → macOS Keychain → encrypted vault)
    with LM Studio's conventional "lm-studio" placeholder as the fallback."""
    try:
        from auth_utils import get_api_key
        return get_api_key("LM_API_TOKEN") or "lm-studio"
    except Exception:  # noqa: BLE001 — never let key resolution break the panel
        # Last-ditch: honor the raw env var directly, else the LM Studio default.
        return (os.environ.get("LM_API_TOKEN", "").strip() or "lm-studio")


_CLOUD_HOST_HINTS = ("anthropic.com", "googleapis.com", "openai.com", "x.ai",
                     "mistral.ai", "cohere.", "groq.com", "together.", "openrouter.")


def _is_loopback_url(url: str) -> bool:
    """True if the endpoint is on THIS machine (loopback). Mirrors m3's own
    _is_local_llm_url discriminator (m3_cognitive_loop)."""
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")


def _is_cloud_url(url: str) -> bool:
    """True for a hosted/frontier endpoint (Anthropic, Gemini, OpenAI, xAI, …) —
    an https, non-loopback host, or a known cloud host. Cloud gets a PING check
    only (reachable + key present); NEVER a billed completion smoke, and none of
    the local 'is a model loaded' signals apply (cloud is always warm)."""
    u = (url or "").lower()
    if any(h in u for h in _CLOUD_HOST_HINTS):
        return True
    # https + not loopback → treat as remote/cloud (LAN https is rare; safe to ping).
    return u.startswith("https://") and not _is_loopback_url(url)


def _cloud_backend_label(url: str) -> str:
    u = (url or "").lower()
    if "anthropic.com" in u:
        return "Anthropic"
    if "googleapis.com" in u:
        return "Google Gemini"
    if "openai.com" in u:
        return "OpenAI"
    if "x.ai" in u:
        return "xAI"
    if "mistral.ai" in u:
        return "Mistral"
    if "groq.com" in u:
        return "Groq"
    if "openrouter." in u:
        return "OpenRouter"
    from urllib.parse import urlparse
    return (urlparse(url).hostname or "cloud endpoint")


def _probe_cloud_endpoint(endpoint: str, api_key_service: str, connect_timeout: float,
                          read_timeout: float) -> dict:
    """PING-ONLY health for a cloud/frontier endpoint. Verifies the API key
    RESOLVES and the API is REACHABLE via a free, non-generating GET /models — NO
    completion, so zero token cost and no rate-limit burn. For cloud the only local
    failure modes are missing key + unreachable; the model itself is always 'loaded'.
    Auth style follows the wire: Anthropic uses x-api-key, others Bearer."""
    import httpx

    out = {"url": endpoint, "backend": _cloud_backend_label(endpoint), "cloud": True,
           "reachable": False, "model_loaded": False, "model_id": "",
           "queryable": False, "loaded_confirmed": False, "detail": "",
           "api_key_service": api_key_service}
    # Resolve the key the SAME way m3 does for this profile (its api_key_service),
    # falling back to LM_API_TOKEN only if the profile didn't name one.
    key = ""
    try:
        from auth_utils import get_api_key
        if api_key_service:
            key = get_api_key(api_key_service) or ""
        if not key:
            key = get_api_key("LM_API_TOKEN") or ""
    except Exception:  # noqa: BLE001
        key = os.environ.get(api_key_service or "", "").strip()
    if not key:
        out["detail"] = f"no API key ({api_key_service or 'unset'}) resolved"
        out["auth_missing"] = True
        return out

    is_anthropic = "anthropic.com" in endpoint.lower()
    # /models base: strip a trailing chat path to reach the provider root's /models.
    base = re.sub(r"/(v1|v1beta)(/openai)?/(chat/completions|messages).*$", r"/\1\2", endpoint)
    base = base.rstrip("/")
    models_url = f"{base}/models"
    headers = ({"x-api-key": key, "anthropic-version": "2023-06-01"} if is_anthropic
               else {"Authorization": f"Bearer {key}"})
    try:
        r = httpx.get(models_url, headers=headers,
                      timeout=httpx.Timeout(connect_timeout, read=read_timeout))
    except Exception as e:  # noqa: BLE001 — network down / DNS / TLS
        out["detail"] = f"unreachable ({type(e).__name__})"
        return out
    out["reachable"] = True
    if r.status_code in (401, 403):
        out["detail"] = f"HTTP {r.status_code} — API key rejected (check {api_key_service})"
        out["auth_rejected"] = True
        return out
    if r.status_code >= 400:
        # Some providers gate /models; a 404/405 still proves reachable+authed
        # enough for a ping (we didn't get 401). Treat as reachable, model assumed.
        out["queryable"] = False
        out["model_loaded"] = True
        out["loaded_confirmed"] = True
        out["detail"] = f"reachable (key accepted); /models returned HTTP {r.status_code}"
        return out
    out["queryable"] = True
    out["model_loaded"] = True
    out["loaded_confirmed"] = True
    out["detail"] = "reachable + key accepted (ping OK, no inference)"
    return out


def _probe_llm_endpoint(endpoint: str, connect_timeout: float, read_timeout: float,
                        api_key_service: str = "") -> dict:
    """Probe one LLM endpoint (sync, read-only). Never raises. For a CLOUD endpoint
    (Anthropic/Gemini/OpenAI/xAI/…) delegates to a ping-only check (reachable + key
    present, no billed completion). For a LOCAL endpoint, uses cache-safe readiness
    signals (LM Studio state / Ollama /api/ps / llama.cpp /health / else /v1/models
    listing). Authenticates with m3's OWN token so a working backend is never
    mis-reported."""
    import httpx
    from llm_failover import EMBED_EXCLUSIONS

    # Cloud/frontier: ping only — no cache-safe 'loaded' signals apply, and a
    # completion would cost money + rate limit. Route to the ping probe.
    if _is_cloud_url(endpoint):
        return _probe_cloud_endpoint(endpoint, api_key_service, connect_timeout, read_timeout)

    out = {"url": endpoint, "backend": _backend_label_for_endpoint(endpoint),
           "reachable": False, "model_loaded": False, "model_id": "",
           "queryable": False, "loaded_confirmed": False, "cloud": False, "detail": ""}
    token = _llm_token()

    # (1) CACHE-SAFE preferred path for LM Studio: native /api/v0/models returns
    # per-model state=="loaded" — authoritative "ready to serve" with NO inference
    # and no cache disturbance. Only LM Studio serves this; a 404/timeout just
    # falls through to the standard /v1/models listing below.
    base = re.sub(r"/v1/?$", "", endpoint)  # strip the OpenAI /v1 suffix
    try:
        rv0 = httpx.get(f"{base}/api/v0/models",
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=httpx.Timeout(connect_timeout, read=read_timeout))
        if rv0.status_code == 200:
            out["reachable"] = True
            out["queryable"] = True
            for m in rv0.json().get("data", []):
                mid = m.get("id", "")
                if any(x in mid.lower() for x in EMBED_EXCLUSIONS):
                    continue
                if m.get("state") == "loaded":
                    out["model_loaded"] = True
                    out["loaded_confirmed"] = True  # state-verified, cache-safe
                    out["model_id"] = mid
                    break
            if out["model_loaded"]:
                out["detail"] = "loaded (state-verified, no inference)"
                return out
            # Reachable + queryable but nothing in state=loaded → definite no_model.
            out["detail"] = "no chat model in state=loaded"
            return out
    except Exception:  # noqa: BLE001 — not LM Studio, or native REST unavailable
        pass

    # (1b) CACHE-SAFE for Ollama: native /api/ps lists models CURRENTLY LOADED in
    # memory (not /api/tags, which is on-disk installed) — zero inference. A
    # non-empty ps → ready to serve now. Empty ps on a reachable Ollama means the
    # model is merely COLD (Ollama lazy-loads on first request), NOT down — so we
    # fall through to /v1/models to confirm it's installed rather than cry "down".
    if ":11434" in endpoint or "ollama" in endpoint.lower():
        try:
            rps = httpx.get(f"{base}/api/ps",
                            headers={"Authorization": f"Bearer {token}"},
                            timeout=httpx.Timeout(connect_timeout, read=read_timeout))
            if rps.status_code == 200:
                out["reachable"] = True
                out["queryable"] = True
                for m in rps.json().get("models", []):
                    mid = m.get("name") or m.get("model", "")
                    if any(x in mid.lower() for x in EMBED_EXCLUSIONS):
                        continue
                    out["model_loaded"] = True
                    out["loaded_confirmed"] = True  # in-memory, cache-safe
                    out["model_id"] = mid
                    break
                if out["model_loaded"]:
                    out["detail"] = "loaded in memory (/api/ps, no inference)"
                    return out
                # Reachable but nothing loaded yet — cold, not broken. Fall through
                # to /v1/models to report it as installed-but-cold vs truly empty.
        except Exception:  # noqa: BLE001 — not Ollama / ps unavailable
            pass

    # (1c) CACHE-SAFE for llama.cpp (llama-server): purpose-built /health returns
    # 200 {"status":"ok"} only when a model is loaded, 503 {"status":"loading
    # model"} while loading — zero inference. Definitive readiness for llama.cpp.
    try:
        rh = httpx.get(f"{base}/health",
                       headers={"Authorization": f"Bearer {token}"},
                       timeout=httpx.Timeout(connect_timeout, read=read_timeout))
        if rh.status_code in (200, 503):
            hstatus = ""
            try:
                hstatus = str(rh.json().get("status", "")).lower()
            except Exception:  # noqa: BLE001
                pass
            if rh.status_code == 200 and hstatus in ("ok", "", "no slot available"):
                # 200 → server up with a model loaded. "no slot available" means
                # loaded AND busy serving — still ready, just saturated.
                out["reachable"] = True
                out["queryable"] = True
                out["model_loaded"] = True
                out["loaded_confirmed"] = True
                out["detail"] = "llama.cpp /health ok (model loaded, no inference)"
                # model id isn't in /health; fill it from /v1/models below if wanted
                # but readiness is already proven — return with what we have.
                return out
            if hstatus == "loading model":
                out["reachable"] = True
                out["detail"] = "llama.cpp loading model (not ready yet)"
                return out
    except Exception:  # noqa: BLE001 — no /health (not llama.cpp) → fall through
        pass

    # (2) Standard OpenAI /v1/models listing — cache-safe, provider-agnostic.
    try:
        r = httpx.get(f"{endpoint}/models",
                      headers={"Authorization": f"Bearer {token}"},
                      timeout=httpx.Timeout(connect_timeout, read=read_timeout))
    except Exception as e:  # noqa: BLE001 — connection refused/timeout = backend down
        out["detail"] = type(e).__name__
        return out
    out["reachable"] = True
    if r.status_code >= 400:
        # Reachable but the /models call itself errored even with m3's own token.
        # Server is UP but we can't read its model list — report honestly, don't
        # claim "no model". (A 401 here means m3's real calls would fail too.)
        out["detail"] = f"HTTP {r.status_code} on /models (auth rejected — check LM_API_TOKEN)"
        return out
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        out["detail"] = "unparseable /models response"
        return out
    out["queryable"] = True
    models = data.get("data", data.get("models", []))
    for m in models:
        mid = (m.get("id") or m.get("model", "")) if isinstance(m, dict) else str(m)
        low = mid.lower()
        if any(x in low for x in EMBED_EXCLUSIONS):
            continue  # embedding model — not usable as the chat/extraction LLM
        out["model_loaded"] = True
        out["model_id"] = mid
        break
    if not out["model_loaded"]:
        out["detail"] = "no chat model loaded (only embedding models, or none)"
    return out


# ── Cache-safe inference verification ─────────────────────────────────────────
# WHICH check, cheapest-first, to avoid disturbing the LLM's OWN prompt cache
# (m3's pipelines send cache_control:ephemeral system prompts — a stray smoke
# completion evicts that warm KV slot). Every major local server exposes a
# NON-INFERENCE readiness signal; we prefer those and almost never need a smoke:
#   1a. LM Studio  → GET /api/v0/models, per-model state=="loaded".
#   1b. Ollama     → GET /api/ps (models loaded IN MEMORY; /api/tags is on-disk).
#       Empty ps on a reachable Ollama = cold (lazy-loads on 1st call), not down.
#   1c. llama.cpp  → GET /health → 200 {"status":"ok"} iff a model is loaded
#       (503 {"status":"loading model"} while loading). Purpose-built, definitive.
#   2.  Generic OpenAI-compat → GET /v1/models listing (listed, cache-safe).
#   3.  Real completion round-trip: ONLY a fallback for a server exposing NONE of
#       the above and whose /v1/models can't confirm loaded-state. It DOES run a
#       forward pass, so it is long-TTL cached, uses a single CONSTANT prompt (at
#       most one stable cache slot, never churns), and is opt-out-able entirely.
# Default TTL is deliberately long (15 min): the failover selection rarely changes
# and we must not periodically cold-start the server's cache.
_SMOKE_CACHE: dict = {}                     # key -> (monotonic_deadline, result)
_SMOKE_TTL_S = float(os.environ.get("M3_DASHBOARD_LLM_SMOKE_TTL_S", "900"))  # 15 min
_SMOKE_LOCK = __import__("threading").Lock()
# A single constant, innocuous prompt so repeated smokes reuse ONE cache slot
# rather than evicting a different one each time. Kept distinct from any pipeline
# prompt so it never collides with a real warm prefix.
_SMOKE_PROMPT = "m3-dashboard-healthcheck: reply with the single word ok"


def _smoke_enabled() -> bool:
    """The real-completion fallback can be turned off entirely for users who never
    want the dashboard to send inference traffic. Cache-safe checks still run."""
    return os.environ.get("M3_DASHBOARD_LLM_SMOKE", "1").strip().lower() not in ("0", "false", "no")


def _smoke_llm_completion(base_url: str, model: str, token: str, timeout_s: float = 6.0) -> dict:
    """Send ONE tiny OpenAI-style completion with a CONSTANT prompt — the real
    'can you take an inference request?' test, mirroring memory_core's failover
    call. Used only as a fallback (see module note) so it rarely runs. Returns
    {ok, status, model_id, detail}. status ∈ {ok, auth_failed, no_model, down,
    bad_response}. Never raises."""
    import httpx

    res = {"ok": False, "status": "down", "model_id": model, "detail": ""}
    try:
        r = httpx.post(
            f"{base_url}/chat/completions",
            json={"model": model,
                  "messages": [{"role": "user", "content": _SMOKE_PROMPT}],
                  "max_tokens": 1, "temperature": 0.0, "stream": False},
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(min(3.0, timeout_s), read=timeout_s),
        )
    except Exception as e:  # noqa: BLE001 — refused/timeout = backend not taking requests
        res["detail"] = type(e).__name__
        res["status"] = "down"
        return res
    if r.status_code in (401, 403):
        res["status"] = "auth_failed"
        res["detail"] = f"HTTP {r.status_code} (auth rejected — check the profile's api_key_service)"
        return res
    if r.status_code >= 400:
        # LM Studio / Ollama return 4xx with a body like "No models loaded" /
        # "model not found" when the endpoint is up but has nothing to serve.
        body = ""
        try:
            body = r.text[:300]
        except Exception:  # noqa: BLE001
            pass
        low = body.lower()
        if "no model" in low or "not loaded" in low or "not found" in low or "load a model" in low:
            res["status"] = "no_model"
        else:
            res["status"] = "bad_response"
        res["detail"] = f"HTTP {r.status_code}: {body.strip()[:160]}"
        return res
    # 2xx — did we actually get a completion back?
    try:
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        res["model_id"] = data.get("model", model) or model
        res["ok"] = True
        res["status"] = "ok"
        res["detail"] = "inference round-trip OK"
        _ = content  # content itself is irrelevant; a well-formed choice proves it
    except Exception as e:  # noqa: BLE001 — 200 but not a completion shape
        res["status"] = "bad_response"
        res["detail"] = f"200 but unparseable completion ({type(e).__name__})"
    return res


def _cached_smoke(base_url: str, model: str, token: str) -> dict:
    """_smoke_llm_completion memoized for _SMOKE_TTL_S. Keyed on endpoint+model+a
    short token fingerprint (so rotating the key re-smokes). Uses a monotonic
    clock via time.perf_counter — no wall-clock, safe across DST."""
    import time
    fp = str(abs(hash(token)) % 10_000_000)  # non-secret fingerprint, not the key
    key = (base_url, model, fp)
    now = time.perf_counter()
    with _SMOKE_LOCK:
        hit = _SMOKE_CACHE.get(key)
        if hit and hit[0] > now:
            cached = dict(hit[1])
            cached["cached"] = True
            return cached
    result = _smoke_llm_completion(base_url, model, token)
    with _SMOKE_LOCK:
        _SMOKE_CACHE[key] = (now + _SMOKE_TTL_S, result)
    result = dict(result)
    result["cached"] = False
    return result


# Short-TTL cache for the WHOLE inference block. The cache-safe probes
# (state=loaded / /api/ps / /health / /v1/models) run no inference, so it's fine
# to refresh them every 30-60s; this cache only spares rapid /api/health polls
# (which can fire every few seconds) from re-hitting even the cheap endpoints.
# Separate from _SMOKE_CACHE (15 min) which guards the rare real completion.
_BLOCK_CACHE: dict = {}                      # {"v": (deadline, result)}
_BLOCK_TTL_S = float(os.environ.get("M3_DASHBOARD_LLM_BLOCK_TTL_S", "45"))  # 30-60s band


def _inference_block() -> dict:
    """Short-TTL-cached wrapper (default 45s) around _inference_block_uncached, so
    rapid dashboard polls don't re-probe every few seconds. The underlying checks
    are cache-safe, so this TTL is about politeness, not cache protection."""
    import time
    now = time.perf_counter()
    hit = _BLOCK_CACHE.get("v")
    if hit and hit[0] > now:
        r = dict(hit[1]); r["block_cached"] = True
        return r
    result = _inference_block_uncached()
    _BLOCK_CACHE["v"] = (now + _BLOCK_TTL_S, result)
    r = dict(result); r["block_cached"] = False
    return r


def _inference_block_uncached() -> dict:
    """LLM/SLM inference-backend health for the dashboard. Determines WHICH backend
    m3 would use exactly as the real call path does — walks llm_failover's RESOLVED
    endpoint list (M3_LLM_URL / LM Studio default / Ollama opt-in / CSV) IN FAILOVER
    ORDER using m3's own token, verifying each hop's readiness with a CACHE-SAFE
    provider-native signal (LM Studio state=loaded / Ollama /api/ps / llama.cpp
    /health / else /v1/models) and only falling back to a real completion when none
    of those can confirm loaded-state. Reports the whole failover chain + which hops
    failed and why. The cognitive loop / entity extraction / enrichment all call
    this backend; if it can't serve, those pipelines stall (queue backs up, never
    drains). status ∈ {ok, failover_active, no_model, auth_failed, down,
    unknown, none_configured}."""
    out: dict = {"status": "none_configured", "endpoints": [], "primary": None,
                 "expected_url": "", "backend": "", "model_id": "", "remedy": "",
                 "verified_by": "inference"}
    try:
        import llm_failover as lf
        endpoints = list(lf.LLM_ENDPOINTS)
        connect_to = getattr(lf, "CONNECT_TIMEOUT", 0.3)
    except Exception as e:  # noqa: BLE001
        out["detail"] = f"llm_failover unavailable: {e}"
        return out

    if not endpoints:
        out["remedy"] = ("No LLM endpoint is configured. Set M3_LLM_URL to your "
                         "OpenAI-compatible server, or enable LM Studio "
                         "(M3_ENABLE_LMSTUDIO_FAILOVER=1) / Ollama "
                         "(M3_ENABLE_OLLAMA_FAILOVER=1).")
        return out

    token = _llm_token()
    # Cheap liveness/model listing per endpoint (drives the per-endpoint display
    # AND tells the smoke which model id to request at each hop).
    probes = [_probe_llm_endpoint(ep, connect_to, 4.0) for ep in endpoints]
    primary = probes[0]
    out["primary"] = primary
    out["expected_url"] = primary["url"]
    out["backend"] = primary["backend"]

    # ── Walk the failover CHAIN in order, exactly as get_best_llm does ─────────
    # For each endpoint: if it lists a usable model, verify it can actually take an
    # inference request (cached real completion). The FIRST hop that both lists a
    # model AND passes the smoke is where m3 lands. Every hop before it that failed
    # is a real failover event — record why. This mirrors llm_failover's "skip on
    # (connect error | HTTP error | empty list | no usable model) → try next".
    chain: list[dict] = []
    landed = None
    for p in probes:
        hop = {"url": p["url"], "backend": p["backend"], "ok": False,
               "status": "", "detail": "", "model_id": "", "cloud": p.get("cloud", False)}
        # Cloud auth failures are their own class: a missing/rejected API key means
        # m3's real calls to that frontier model fail too. Surface as auth_failed
        # regardless of the reachable/queryable flags (a missing key never reached
        # the network at all).
        if p.get("cloud") and (p.get("auth_missing") or p.get("auth_rejected")):
            hop["status"] = "auth_failed"
            hop["detail"] = p.get("detail") or "cloud API key missing/rejected"
            chain.append(hop)
            continue
        if not p["reachable"]:
            hop["status"] = "down"
            hop["detail"] = p.get("detail") or "unreachable"
            chain.append(hop)
            continue
        if not p.get("queryable", False) and not p.get("loaded_confirmed"):
            # Reachable but /models unreadable (e.g. auth). get_best_llm would skip
            # (raise_for_status), so failover treats this as a failed hop.
            hop["status"] = "auth_failed" if "401" in str(p.get("detail")) or "403" in str(p.get("detail")) else "down"
            hop["detail"] = p.get("detail") or "model list unreadable"
            chain.append(hop)
            continue
        if not p["model_loaded"]:
            hop["status"] = "no_model"
            hop["detail"] = p.get("detail") or "no usable chat model listed"
            chain.append(hop)
            continue
        # A usable model is present. Prefer the CACHE-SAFE verification:
        if p.get("loaded_confirmed"):
            # LM Studio state=="loaded" already PROVED it can serve, with zero
            # inference and zero cache disturbance. No completion needed.
            hop["status"] = "ok"
            hop["detail"] = p.get("detail") or "loaded (state-verified)"
            hop["model_id"] = p.get("model_id") or ""
            hop["ok"] = True
            hop["verified"] = "state"
            chain.append(hop)
            landed = hop
            break
        if not _smoke_enabled():
            # Completion smoke disabled by the user. Trust the cache-safe listing
            # as a liveness signal but mark it as not inference-verified.
            hop["status"] = "ok"
            hop["detail"] = "model listed (smoke disabled; not inference-verified)"
            hop["model_id"] = p.get("model_id") or ""
            hop["ok"] = True
            hop["verified"] = "listing"
            chain.append(hop)
            landed = hop
            break
        # Fallback ONLY: a real completion, long-TTL cached, constant prompt.
        smoke = _cached_smoke(p["url"], p.get("model_id") or "default", token)
        hop["status"] = smoke["status"]
        hop["detail"] = smoke.get("detail", "")
        hop["model_id"] = smoke.get("model_id") or p.get("model_id") or ""
        hop["ok"] = smoke["status"] == "ok"
        hop["cached"] = smoke.get("cached", False)
        hop["verified"] = "inference"
        chain.append(hop)
        if hop["ok"]:
            landed = hop
            break  # get_best_llm returns at the first usable endpoint
        # else: smoke failed (no_model/auth/down/bad) → failover to next hop

    out["chain"] = chain
    # Endpoints carry the merged listing+smoke view for the per-endpoint display.
    for p, hop in zip(probes, chain):
        p["smoke_status"] = hop["status"]
        p["smoke_detail"] = hop["detail"]
        p["serves"] = hop["ok"]
    out["endpoints"] = probes

    # Count real failover events: hops that failed BEFORE the landing point.
    failed_hops = [h for h in chain if not h["ok"] and h["status"] != ""]

    if landed is not None:
        out["backend"] = landed["backend"]
        out["expected_url"] = landed["url"]
        out["model_id"] = landed["model_id"]
        # Landed on the PRIMARY (first hop) with no prior failures → clean ok.
        if landed is chain[0]:
            out["status"] = "ok"
        else:
            # Landed on a SECONDARY — failover is ACTIVE. Working, but degraded:
            # the preferred endpoint(s) failed. Surface which, and why.
            out["status"] = "failover_active"
            trail = "; ".join(f"{h['backend']} {h['url']} ({h['status']}"
                              + (f": {h['detail']}" if h['detail'] else "") + ")"
                              for h in failed_hops)
            out["remedy"] = (f"Primary LLM endpoint(s) failed; m3 failed over to "
                             f"{landed['backend']} at {landed['url']}. Inference works, "
                             f"but the preferred backend is down — {trail}.")
        return out

    # Whole chain exhausted — nothing could take an inference request. Report the
    # dominant failure and the full trail.
    statuses = [h["status"] for h in chain]
    if all(s == "auth_failed" for s in statuses) and statuses:
        out["status"] = "auth_failed"
    elif any(s == "no_model" for s in statuses) and not any(s == "down" for s in statuses):
        out["status"] = "no_model"
    else:
        out["status"] = "down"
    trail = "; ".join(f"{h['backend']} {h['url']} ({h['status']}"
                      + (f": {h['detail']}" if h['detail'] else "") + ")"
                      for h in chain)
    if out["status"] == "no_model":
        out["remedy"] = (f"An LLM server is up but no chat model can serve a request. "
                         f"Load a model (`lms load <model>` / `ollama run <model>`); the "
                         f"pipeline drains once a model serves. Failover chain: {trail}.")
    elif out["status"] == "auth_failed":
        out["remedy"] = (f"Every LLM endpoint rejected m3's credentials — its real calls "
                         f"fail too. Set LM_API_TOKEN (or the profile's api_key_service) "
                         f"to the server's key. Failover chain: {trail}.")
    else:
        out["remedy"] = (f"No LLM backend could take an inference request across the whole "
                         f"failover chain. Start one (LM Studio: `lms server start` + load "
                         f"a model; Ollama: `ollama serve`). Failover chain: {trail}.")
    return out


def _active_backend():
    from memory.backends import active_backend
    return active_backend()


def _sqlite_store(db_path: str) -> "dict | None":
    """(path, rows, last_updated) for a SQLite store file, or None if absent."""
    import sqlite3
    if not db_path or not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        def _has(t: str) -> bool:
            return conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone() is not None

        rows, last = 0, None
        if _has("memory_items"):
            rows = conn.execute(
                "SELECT COUNT(*) FROM memory_items WHERE COALESCE(is_deleted,0)=0"
            ).fetchone()[0]
            last = conn.execute(
                "SELECT MAX(COALESCE(updated_at, created_at)) FROM memory_items"
            ).fetchone()[0]
        elif _has("leaves"):
            rows = conn.execute("SELECT COUNT(*) FROM leaves").fetchone()[0]
        return {"path": db_path, "rows": rows, "last_updated": _fmt_dual_time(last)}
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _backend_block() -> dict:
    """Backend identity + per-store stats, backend-agnostic."""
    out: dict[str, Any] = {"backend": "unknown", "stores": [], "note": ""}
    try:
        from memory.backends import resolve_backend_name
        out["backend"] = resolve_backend_name()
    except Exception as e:  # noqa: BLE001
        out["note"] = f"backend unresolved: {e}"
        return out

    if out["backend"] == "sqlite":
        try:
            from chatlog_config import DEFAULT_DB_PATH as chat_db
        except Exception:  # noqa: BLE001
            chat_db = ""
        try:
            from memory.config import FILES_DB_PATH as files_db
        except Exception:  # noqa: BLE001
            files_db = ""
        try:
            from m3_sdk import resolve_db_path
            core_db = resolve_db_path(None)
        except Exception:  # noqa: BLE001
            core_db = ""

        entries = [("core", core_db)]
        if chat_db and os.path.abspath(chat_db) != os.path.abspath(core_db or ""):
            entries.append(("chat", chat_db))
        else:
            entries.append(("chat", core_db))
        if files_db:
            entries.append(("files", files_db))

        seen: set = set()
        for label, path in entries:
            ap = os.path.abspath(path) if path else ""
            shared = bool(ap and ap in seen)
            if ap:
                seen.add(ap)
            st = _sqlite_store(path) if path else None
            out["stores"].append({
                "label": label,
                "path": path or "(not discernible)",
                "present": st is not None,
                "rows": st["rows"] if st else None,
                "last_updated": st["last_updated"] if st else "—",
                "shared": shared,
            })
    else:
        # PostgreSQL / other SQL backend: report identity + counts via a probe.
        try:
            import re

            from m3_sdk import resolve_primary_pg_dsn
            dsn = (resolve_primary_pg_dsn("") or "").strip()
            masked = re.sub(r"(://[^:/@]+:)[^@/]+(@)", r"\1***\2", dsn) if dsn else ""
            rows, last, reachable = None, "—", False
            if dsn:
                import psycopg2
                conn = psycopg2.connect(dsn, connect_timeout=5)
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM memory_items WHERE COALESCE(is_deleted,0)=0")
                rows = cur.fetchone()[0]
                try:
                    cur.execute("SELECT MAX(COALESCE(updated_at, created_at)) FROM memory_items")
                    last = _fmt_dual_time(cur.fetchone()[0])
                except Exception:  # noqa: BLE001
                    pass
                conn.close()
                reachable = True
            out["stores"].append({
                "label": "primary", "path": masked or "(no DSN set)",
                "present": reachable, "rows": rows, "last_updated": last, "shared": False,
            })
        except Exception as e:  # noqa: BLE001
            out["note"] = f"backend probe failed: {e}"
    return out


def _cdw_block() -> "dict | None":
    """CDW warehouse sync watermarks, or None if no warehouse is configured."""
    import sqlite3
    try:
        from m3_sdk import resolve_cdw_pg_dsn, resolve_db_path
        cdw = (resolve_cdw_pg_dsn("") or "").strip()
    except Exception:  # noqa: BLE001
        return None
    if not cdw:
        return None
    import re
    masked = re.sub(r"(://[^:/@]+:)[^@/]+(@)", r"\1***\2", cdw)
    out: dict[str, Any] = {"dsn": masked, "watermarks": []}
    try:
        core_db = resolve_db_path(None)
    except Exception:  # noqa: BLE001
        return out
    if not core_db or not os.path.exists(core_db):
        return out
    try:
        conn = sqlite3.connect(f"file:{core_db}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return out
    try:
        have = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_watermarks'"
        ).fetchone()
        if have:
            for direction, ts in conn.execute(
                "SELECT direction, last_synced_at FROM sync_watermarks ORDER BY direction"
            ).fetchall():
                out["watermarks"].append({"direction": direction, "last_sync": _fmt_dual_time(ts)})
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return out


def _pipeline_block(core_db: str) -> dict:
    """Enrichment/reflection queue status, normalized for the panel.

    Each queue_stats pipeline carries {label, queue_len, rates, eta_human}. We
    add a plain-language STATUS so a user knows if a nonzero queue is normal:
      * queue_len == 0            → "idle" (drained; NORMAL — nothing waiting).
      * queue_len > 0, draining   → "processing" (items queued but the rate is
                                     clearing them; NORMAL under load).
      * queue_len > 0, no recent  → "backlog" (items queued but nothing produced
        production                   recently; worth attention).
    A queue is NEVER 'broken' on its own — a backlog just means the background
    worker (governor / scheduled drainer) hasn't caught up yet.
    """
    out: dict[str, Any] = {"pipelines": [], "governor": None}
    try:
        from dashboard.queue_stats import collect_governor, collect_pipeline_stats
        raw = collect_pipeline_stats(core_db).get("pipelines", [])
        for p in raw:
            qlen = int(p.get("queue_len", 0) or 0)
            rates = p.get("rates", {}) or {}
            recent = any(float(v or 0) > 0 for v in rates.values())
            if qlen == 0:
                status, tone = "idle (drained)", "ok"
            elif recent:
                status, tone = "processing", "ok"
            else:
                status, tone = "backlog (worker idle)", "warn"
            out["pipelines"].append({
                "label": p.get("label", p.get("key", "queue")),
                "queue_len": qlen,
                "eta_human": p.get("eta_human", ""),
                "status": status,
                "tone": tone,
            })
        gov = collect_governor(core_db)
        out["governor"] = gov if gov.get("available") else None
    except Exception:  # noqa: BLE001 — pipeline detail is optional
        pass
    return out


def collect_health() -> dict:
    """One structured health snapshot for the dashboard's System Health view."""
    core_db = ""
    try:
        from m3_sdk import resolve_db_path
        core_db = resolve_db_path(None)
    except Exception:  # noqa: BLE001
        pass
    inference = _inference_block()
    pipeline = _pipeline_block(core_db)
    return {
        "verdict": _verdict(inference=inference, pipeline=pipeline),
        "backend": _backend_block(),
        "inference": inference,
        "cdw": _cdw_block(),
        "pipeline": pipeline,
        "generated_at": _fmt_dual_time(__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc)),
    }
