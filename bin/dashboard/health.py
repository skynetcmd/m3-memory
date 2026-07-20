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
    inf_status = (inference or {}).get("status")
    if inf_status in ("down", "no_model", "unknown"):
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
            else:  # unknown — model state unverifiable; warn, don't alarm red
                why = "reachable but its model state could not be verified"
            reasons.insert(0, f"Inference backend (LLM/SLM) is {why} — background "
                           f"pipelines are backlogged and cannot drain. {remedy}")

    # Choose the least-alarming accurate label from the real cause.
    if inference_stall:
        label = "INFERENCE BACKEND DOWN"
        tone = "bad"
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


def _probe_llm_endpoint(endpoint: str, connect_timeout: float, read_timeout: float) -> dict:
    """Probe one LLM endpoint's GET /v1/models (sync, read-only). Classifies into
    reachable? / has a usable (non-embedding) chat model loaded?  Never raises.
    Authenticates with m3's OWN token (_llm_token) — same key + fallback the real
    LLM call path uses — so a working backend is never mis-reported as 401/down."""
    import httpx
    from llm_failover import EMBED_EXCLUSIONS

    out = {"url": endpoint, "backend": _backend_label_for_endpoint(endpoint),
           "reachable": False, "model_loaded": False, "model_id": "",
           "queryable": False, "detail": ""}
    token = _llm_token()
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


def _inference_block() -> dict:
    """LLM/SLM inference-backend health for the dashboard. Reuses llm_failover's
    RESOLVED endpoint list (respects M3_LLM_URL, LM Studio on-by-default, Ollama
    opt-in, LLM_ENDPOINTS_CSV) so we report exactly where m3 expects its LLM — no
    hardcoded port. Reports, per endpoint, whether it is reachable and has a usable
    chat model loaded. The cognitive loop / entity extraction / enrichment all call
    this backend; if it is down or empty, those pipelines stall (queue backs up but
    never drains). status ∈ {ok, no_model, down, none_configured}."""
    out: dict = {"status": "none_configured", "endpoints": [], "primary": None,
                 "expected_url": "", "backend": "", "remedy": ""}
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

    probes = [_probe_llm_endpoint(ep, connect_to, 4.0) for ep in endpoints]
    out["endpoints"] = probes
    # Primary = the first endpoint in failover order (what discovery would pick).
    primary = probes[0]
    out["primary"] = primary
    out["expected_url"] = primary["url"]
    out["backend"] = primary["backend"]

    serving = next((p for p in probes if p["model_loaded"]), None)
    # "empty" = we could query the model list and it had no usable chat model —
    # a DEFINITE no-model. "reachable-but-not-queryable" (e.g. 401 on /models) is
    # only a MAYBE, reported as unknown, never a false "no model loaded".
    empty = next((p for p in probes if p["queryable"] and not p["model_loaded"]), None)
    reachable = next((p for p in probes if p["reachable"]), None)
    if serving:
        out["status"] = "ok"
        out["backend"] = serving["backend"]
        out["expected_url"] = serving["url"]
    elif empty:
        # A server is up and its model list is empty of chat models — the exact
        # stall cause.
        out["status"] = "no_model"
        out["backend"] = empty["backend"]
        out["expected_url"] = empty["url"]
        out["remedy"] = (f"{empty['backend']} is running at {empty['url']} "
                         f"but no chat model is loaded. Load one (e.g. `lms load "
                         f"<model>` for LM Studio, or `ollama run <model>`); the "
                         f"pipeline drains automatically once a model is serving.")
    elif reachable:
        # Reachable but we couldn't read the model list (auth/format). Server is
        # up; model state unknown. Don't cry wolf — surface it as an advisory.
        out["status"] = "unknown"
        out["backend"] = reachable["backend"]
        out["expected_url"] = reachable["url"]
        out["remedy"] = (f"{reachable['backend']} is reachable at {reachable['url']} "
                         f"but its model list could not be read with m3's own token "
                         f"({reachable.get('detail','')}). If auth was rejected, m3's real "
                         f"LLM calls will fail too — set the LM_API_TOKEN secret to the "
                         f"server's API key. Otherwise confirm a chat model is loaded.")
    else:
        out["status"] = "down"
        out["remedy"] = (f"No LLM backend is reachable. Expected {primary['backend']} "
                         f"at {primary['url']} — start it (LM Studio: `lms server "
                         f"start` + load a model; Ollama: `ollama serve`).")
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
