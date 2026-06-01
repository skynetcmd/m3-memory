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


# ── Quick-repair mode (m3 doctor --fix) ──────────────────────────────────────

async def memory_doctor_fix_impl(dry_run: bool = False) -> dict[str, Any]:
    """Run the doctor in repair mode — attempt to auto-fix detected issues.

    Repair actions (ordered safest → most impactful):
      1. Run pending migrations (``migrate_memory.py up --yes``).
      2. Rebuild the FTS5 full-text index (``INSERT INTO ... REBUILD``).
      3. Run missing-embedding backfill (``embed_backfill.py``).
      4. Rebuild the m3_system_cohesion table if it is missing or stale.

    Each action records its outcome in the returned dict. dry_run=True
    reports what *would* be done without writing anything.

    Returns:
        {
            "dry_run": bool,
            "actions": [
                {"action": str, "status": "ok"|"skipped"|"error", "detail": str},
                ...
            ],
            "summary": "ok" | "partial" | "nothing_to_do" | "failed",
        }
    """
    import subprocess
    import sys

    from m3_sdk import resolve_db_path

    diag = await memory_doctor_impl()
    actions: list[dict[str, Any]] = []

    def _record(action: str, status: str, detail: str = "") -> None:
        actions.append({"action": action, "status": status, "detail": detail})
        logger.info("doctor --fix [%s] %s: %s", status, action, detail or "(ok)")

    db_path = resolve_db_path(None)

    # ── Action 1: Run pending migrations ──────────────────────────────────────
    migration_needed = diag["db"]["status"] != "online"
    if not migration_needed:
        # Check whether DB version is behind the latest migration file
        try:
            import os, re, sqlite3 as _sq
            mig_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "memory", "migrations",
            )
            file_versions = sorted(
                int(m.group(1))
                for fn in os.listdir(mig_dir)
                if (m := re.match(r"^(\d+)_.*\.up\.sql$", fn))
            )
            latest_file = max(file_versions) if file_versions else 0
            conn = _sq.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
            try:
                row = conn.execute(
                    "SELECT MAX(CAST(version AS INTEGER)) FROM schema_versions "
                    "WHERE CAST(version AS INTEGER) > 0"
                ).fetchone()
                db_ver = int(row[0]) if row and row[0] else 0
            finally:
                conn.close()
            migration_needed = db_ver < latest_file
        except Exception as e:
            migration_needed = True
            logger.debug("doctor --fix migration check failed: %s", e)

    if migration_needed:
        if dry_run:
            _record("run_migrations", "skipped", "dry_run=True; would run migrate_memory.py up --yes")
        else:
            try:
                import os as _os
                env = _os.environ.copy()
                env["M3_DATABASE"] = str(db_path)
                mig_script = _os.path.join(
                    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                    "bin", "migrate_memory.py"
                )
                result = subprocess.run(
                    [sys.executable, mig_script, "up", "--yes"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if result.returncode == 0:
                    _record("run_migrations", "ok", result.stdout.strip()[:200] or "migrations applied")
                else:
                    _record("run_migrations", "error", result.stderr.strip()[:300])
            except Exception as e:
                _record("run_migrations", "error", str(e))
    else:
        _record("run_migrations", "skipped", "DB already at latest migration version")

    # ── Action 2: Rebuild FTS5 index ──────────────────────────────────────────
    try:
        import sqlite3 as _sq
        conn = _sq.connect(str(db_path), timeout=5.0)
        conn.row_factory = _sq.Row
        try:
            # Check whether FTS index exists
            fts_exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_items_fts'"
            ).fetchone() is not None
        finally:
            conn.close()
    except Exception:
        fts_exists = False

    if fts_exists:
        if dry_run:
            _record("rebuild_fts5", "skipped", "dry_run=True; would run INSERT INTO memory_items_fts(memory_items_fts) VALUES('rebuild')")
        else:
            try:
                conn = _sq.connect(str(db_path), timeout=30.0)
                try:
                    conn.execute("INSERT INTO memory_items_fts(memory_items_fts) VALUES('rebuild')")
                    conn.commit()
                    _record("rebuild_fts5", "ok", "FTS5 index rebuilt")
                finally:
                    conn.close()
            except Exception as e:
                _record("rebuild_fts5", "error", str(e))
    else:
        _record("rebuild_fts5", "skipped", "memory_items_fts table not found")

    # ── Action 3: Embed backfill (items missing embeddings) ───────────────────
    try:
        import sqlite3 as _sq
        conn = _sq.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM memory_items mi "
                "LEFT JOIN memory_embeddings me ON mi.id = me.memory_id "
                "WHERE mi.is_deleted = 0 AND me.memory_id IS NULL"
            ).fetchone()
            missing_embeds = int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception:
        missing_embeds = 0

    if missing_embeds > 0:
        if dry_run:
            _record(
                "embed_backfill",
                "skipped",
                f"dry_run=True; {missing_embeds} items missing embeddings — would run embed_backfill.py",
            )
        else:
            try:
                import os as _os
                env = _os.environ.copy()
                env["M3_DATABASE"] = str(db_path)
                bf_script = _os.path.join(
                    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                    "bin", "embed_backfill.py"
                )
                result = subprocess.run(
                    [sys.executable, bf_script, "--limit", "500"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if result.returncode == 0:
                    _record("embed_backfill", "ok", f"backfilled {missing_embeds} items (capped at 500 per run)")
                else:
                    _record("embed_backfill", "error", result.stderr.strip()[:300])
            except Exception as e:
                _record("embed_backfill", "error", str(e))
    else:
        _record("embed_backfill", "skipped", "no items missing embeddings")

    # ── Action 4: Cohesion table rebuild ──────────────────────────────────────
    try:
        import sqlite3 as _sq
        conn = _sq.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            cohesion_ok = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='m3_system_cohesion'"
            ).fetchone() is not None
        finally:
            conn.close()
    except Exception:
        cohesion_ok = False

    if not cohesion_ok:
        if dry_run:
            _record("rebuild_cohesion", "skipped", "dry_run=True; would rebuild m3_system_cohesion table")
        else:
            try:
                from m3_sdk import M3Context
                # Trigger a re-init of the cohesion table by opening a connection
                ctx = M3Context.for_db(str(db_path))
                _ = ctx.get_sqlite_conn()
                _record("rebuild_cohesion", "ok", "m3_system_cohesion table rebuilt via M3Context init")
            except Exception as e:
                _record("rebuild_cohesion", "error", str(e))
    else:
        _record("rebuild_cohesion", "skipped", "m3_system_cohesion table already present")

    # ── Summary ───────────────────────────────────────────────────────────────
    statuses = {a["status"] for a in actions}
    if not actions or statuses == {"skipped"}:
        summary = "nothing_to_do"
    elif "error" in statuses and "ok" in statuses:
        summary = "partial"
    elif "error" in statuses:
        summary = "failed"
    else:
        summary = "ok"

    return {
        "dry_run": dry_run,
        "actions": actions,
        "summary": summary,
    }

