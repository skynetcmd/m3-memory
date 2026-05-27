"""memory_doctor — self-service diagnostic for the m3-memory cascade.

Probes the four things that go wrong silently in deployments:

  1. Tier 1 — in-process Rust GGUF embedder. Needs M3_EMBED_GGUF env +
     m3_core_rs Rust binding.
  2. Tier 2 — always-on m3-embed-server HTTP service (default :8082).
     Independent of tier 1; either can be present, both is normal.
  3. DB integrity — SQLite open + sentinel-row read on the active DB.
  4. Embed roundtrip — one real `_embed("ping")` call, end-to-end through
     whichever tier wins. Measures wall-clock + records which backend
     actually served the request.

Output is a structured dict, never a message string. Callers can both
display it and act on it programmatically (e.g. agent decides to install
the 8082 service when `tier_2.status == 'offline'`).

Design notes:
  - Robustness: empty fields are `None`, not missing keys
  - Efficiency: each probe has its own short timeout (2s default)
  - Effectiveness: solves "MCP hung — why?" without manual triage
  - Hardening: read-only, no write paths, no destructive ops
"""
from __future__ import annotations

import asyncio
import http.client
import logging
import os
import time
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("memory.doctor")

# Per-probe timeout — short, because hangs are exactly what this tool diagnoses.
PROBE_TIMEOUT_S = 2.0


def _probe_tier1() -> dict[str, Any]:
    """Tier 1: in-process Rust GGUF embedder via m3_core_rs."""
    res: dict[str, Any] = {
        "status": "offline",
        "gguf_path": None,
        "gguf_exists": False,
        "m3_core_rs_importable": False,
        "embedder_initialized": False,
        "error": None,
    }
    gguf = os.environ.get("M3_EMBED_GGUF") or ""
    res["gguf_path"] = gguf or None
    if gguf:
        res["gguf_exists"] = os.path.exists(gguf)
    try:
        import m3_core_rs  # noqa: F401
        res["m3_core_rs_importable"] = True
    except Exception as e:
        res["error"] = f"m3_core_rs import: {type(e).__name__}: {e}"
        return res
    if not gguf or not res["gguf_exists"]:
        res["status"] = "not-configured"
        if gguf and not res["gguf_exists"]:
            res["error"] = f"GGUF path set but file missing: {gguf}"
        return res
    try:
        from memory import embed as _embed_mod
        emb = _embed_mod._get_embedded_embedder()
        res["embedder_initialized"] = emb is not None
        res["status"] = "online" if emb is not None else "init-failed"
    except Exception as e:
        res["error"] = f"embedder init: {type(e).__name__}: {e}"
    return res


def _probe_tier2() -> dict[str, Any]:
    """Tier 2: m3-embed-server HTTP service (default :8082)."""
    url = os.environ.get("M3_EMBED_FALLBACK_URL") or "http://127.0.0.1:8082"
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8082
    res: dict[str, Any] = {
        "status": "offline",
        "url": url,
        "host": host,
        "port": port,
        "health": None,
        "model": None,
        "metrics": None,
        "latency_ms": None,
        "error": None,
    }
    t0 = time.perf_counter()
    try:
        conn = http.client.HTTPConnection(host, port, timeout=PROBE_TIMEOUT_S)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        body = resp.read().decode(errors="replace").strip()
        conn.close()
        res["health"] = body
        res["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        if resp.status != 200 or body != "OK":
            res["status"] = f"unhealthy-{resp.status}"
            return res
        # /metrics: model + queue stats
        conn = http.client.HTTPConnection(host, port, timeout=PROBE_TIMEOUT_S)
        conn.request("GET", "/metrics")
        mresp = conn.getresponse()
        if mresp.status == 200:
            import json as _json
            try:
                metrics = _json.loads(mresp.read().decode())
                res["metrics"] = metrics
                res["model"] = metrics.get("model")
            except Exception:
                pass
        conn.close()
        res["status"] = "online"
    except (ConnectionRefusedError, OSError, http.client.HTTPException) as e:
        res["error"] = f"{type(e).__name__}: {e}"
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
    return res


def _probe_db() -> dict[str, Any]:
    """SQLite open + sentinel query on the active DB."""
    res: dict[str, Any] = {
        "status": "offline",
        "db_path": None,
        "schema_migrations": None,
        "memory_items_count": None,
        "error": None,
    }
    try:
        from m3_sdk import resolve_db_path

        from memory import db as _db_mod
        path = resolve_db_path(None)
        res["db_path"] = str(path)
        with _db_mod._db() as conn:
            try:
                row = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()
                res["schema_migrations"] = int(row[0]) if row else 0
            except Exception:
                pass
            try:
                row = conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()
                res["memory_items_count"] = int(row[0]) if row else 0
            except Exception:
                pass
            res["status"] = "online"
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
    return res


async def _probe_roundtrip() -> dict[str, Any]:
    """End-to-end embed call. Records latency + which backend served."""
    res: dict[str, Any] = {
        "status": "failed",
        "latency_ms": None,
        "dim": None,
        "model": None,
        "error": None,
    }
    try:
        from memory import embed as _embed_mod
        t0 = time.perf_counter()
        vec, model = await asyncio.wait_for(
            _embed_mod._embed("ping"), timeout=10.0,
        )
        res["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        if vec is None:
            res["error"] = "all cascade tiers returned no vector"
            return res
        res["dim"] = len(vec)
        res["model"] = model
        res["status"] = "ok"
    except asyncio.TimeoutError:
        res["error"] = "embed call exceeded 10s timeout — cascade is hung"
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
    return res


async def memory_doctor_impl() -> dict[str, Any]:
    """Run all four probes concurrently, return a structured diagnostic.

    Returns:
        {
            "summary": "healthy" | "degraded" | "broken",
            "tier_1": {...},          # in-proc GGUF
            "tier_2": {...},          # 8082 HTTP service
            "db": {...},              # SQLite integrity
            "roundtrip": {...},       # end-to-end embed call
            "issues": [str, ...],     # human-readable problems
            "recommendations": [str, ...],
        }
    """
    # Run sync probes in threads to keep the async probe truly parallel.
    tier1, tier2, db, roundtrip = await asyncio.gather(
        asyncio.to_thread(_probe_tier1),
        asyncio.to_thread(_probe_tier2),
        asyncio.to_thread(_probe_db),
        _probe_roundtrip(),
    )

    issues: list[str] = []
    recommendations: list[str] = []

    # Tier classification
    t1_ok = tier1["status"] == "online"
    t2_ok = tier2["status"] == "online"
    db_ok = db["status"] == "online"
    rt_ok = roundtrip["status"] == "ok"

    if not t1_ok and tier1["status"] == "not-configured":
        if not tier1["gguf_path"]:
            recommendations.append(
                "Set M3_EMBED_GGUF to a BGE-M3 GGUF path to enable tier-1 "
                "in-process embedding (10-100x faster than HTTP fallback)."
            )
        elif not tier1["gguf_exists"]:
            issues.append(
                f"M3_EMBED_GGUF points to a missing file: {tier1['gguf_path']}"
            )
    elif tier1.get("error"):
        issues.append(f"tier-1 error: {tier1['error']}")

    if not t2_ok:
        issues.append(
            f"tier-2 (m3-embed-server at {tier2['url']}) is {tier2['status']}: "
            f"{tier2.get('error') or 'no error'}"
        )
        recommendations.append(
            "Install/start the m3-embed-server service. On Windows: "
            "`m3-embed-server.exe install && m3-embed-server.exe start` from "
            "an elevated shell. On Unix: systemd unit (see EMBED_DEPLOYMENT.md)."
        )

    if not db_ok:
        issues.append(f"DB error: {db.get('error')}")

    if not rt_ok:
        issues.append(f"embed roundtrip: {roundtrip.get('error')}")

    # Summary
    if rt_ok and db_ok and (t1_ok or t2_ok):
        summary = "healthy" if (t1_ok and t2_ok) else "degraded"
    else:
        summary = "broken"

    if summary == "degraded" and not t1_ok and t2_ok:
        recommendations.append(
            "System functional via tier-2 (HTTP) only. Consider configuring "
            "tier-1 (M3_EMBED_GGUF) for better latency on the hot path."
        )
    if summary == "degraded" and t1_ok and not t2_ok:
        recommendations.append(
            "tier-1 active but tier-2 missing. Cold-cascade requests (e.g. "
            "new MCP server processes) will be slow until tier-2 is installed."
        )

    return {
        "summary": summary,
        "tier_1": tier1,
        "tier_2": tier2,
        "db": db,
        "roundtrip": roundtrip,
        "issues": issues,
        "recommendations": recommendations,
    }
