"""Governor + queue + throughput stats for the dashboard.

Pure data layer (no FastAPI import) so it is unit-testable without the web
stack. The dashboard route calls collect_pipeline_stats(db_path) and renders
the returned dict.

Throughput is TIMESTAMP-DERIVED with zero new state: a queue row is deleted
when processed, so processed-rate can't come from the queue table itself.
Instead we count rows PRODUCED in each time window on the OUTPUT tables
(memory_embeddings.created_at for embeds, memory_items.created_at filtered by
type for enrichment/entities). That is an honest measure of work actually done,
not a guess. Drain ETA = queue_len / recent_rate (assumes the recent rate holds
— labelled as an estimate in the UI).
"""
from __future__ import annotations

import sqlite3
from typing import Optional

# Time windows (minutes) the dashboard reports throughput over.
WINDOWS_MIN = (1, 10, 30, 60)

# Each pipeline: the queue table (pending count) + the "produced" signal used to
# measure throughput (table + created-timestamp column + optional type filter).
_PIPELINES = (
    {
        "key": "enrich",
        "label": "Enrichment",
        "queue_table": "observation_queue",
        "queue_ts": "enqueued_at",
        "produced_table": "memory_items",
        "produced_ts": "created_at",
        "produced_where": "type = 'fact_enriched'",
    },
    {
        "key": "reflect",
        "label": "Reflection",
        "queue_table": "reflector_queue",
        "queue_ts": "enqueued_at",
        "produced_table": "memory_items",
        "produced_ts": "created_at",
        "produced_where": "type = 'belief'",
    },
)


# SQLite can't bind identifiers (table/column names), so those must be
# f-string-interpolated. Today every caller passes a constant from _PIPELINES,
# but an f-string-into-SQL with no guard is an injection footgun the moment a
# future caller threads a dynamic value through — so we allowlist against the
# exact identifiers/fragments _PIPELINES uses (§6 defense-in-depth: never
# interpolate an unvalidated identifier).
_ALLOWED_TABLES: frozenset[str] = frozenset(
    {p["queue_table"] for p in _PIPELINES} | {p["produced_table"] for p in _PIPELINES}
)
_ALLOWED_TS_COLS: frozenset[str] = frozenset(
    {p["queue_ts"] for p in _PIPELINES} | {p["produced_ts"] for p in _PIPELINES}
)
_ALLOWED_WHERE: frozenset[str] = frozenset(
    p["produced_where"] for p in _PIPELINES if p["produced_where"]
)


def _safe_ident(value: str, allowed: frozenset[str], kind: str) -> str:
    """Return `value` only if it's in the allowlist; else raise. Guards the
    identifier interpolation in the COUNT queries below."""
    if value not in allowed:
        raise ValueError(f"unsafe {kind} identifier {value!r} (not in allowlist)")
    return value


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,)
    ).fetchone() is not None


def _count(conn: sqlite3.Connection, table: str) -> int:
    table = _safe_ident(table, _ALLOWED_TABLES, "table")
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _produced_in_window(
    conn: sqlite3.Connection, table: str, ts_col: str, where: str, minutes: int
) -> int:
    """Rows produced in the last `minutes`, using SQLite datetime math on the
    ISO-8601 created_at. SQL does the filtering (no Python-side row scan).
    `table`, `ts_col`, and `where` are validated against _PIPELINES-derived
    allowlists — no unvalidated identifier reaches the query string."""
    table = _safe_ident(table, _ALLOWED_TABLES, "table")
    ts_col = _safe_ident(ts_col, _ALLOWED_TS_COLS, "timestamp column")
    clause = f"{ts_col} > datetime('now', ?)"
    if where:
        # `where` fragments come only from _PIPELINES' produced_where constants
        # (fixed literals like "type = 'fact_enriched'"); validated below.
        clause += f" AND {_safe_ident(where, _ALLOWED_WHERE, 'where-clause')}"
    sql = f"SELECT COUNT(*) FROM {table} WHERE {clause}"
    return conn.execute(sql, (f"-{int(minutes)} minutes",)).fetchone()[0]


def _eta_seconds(queue_len: int, rate_per_min: float) -> Optional[float]:
    """Estimated seconds to drain the queue at rate_per_min. None if the queue
    is empty (nothing to drain) or the rate is zero (can't estimate — would be
    infinite; the UI shows 'stalled' / '—')."""
    if queue_len <= 0:
        return 0.0
    if rate_per_min <= 0:
        return None
    return (queue_len / rate_per_min) * 60.0


def collect_pipeline_stats(db_path: str) -> dict:
    """Return governor-independent pipeline stats for the dashboard.

    Shape:
      {
        "pipelines": [
          {"key","label","queue_len","rates":{1:.., 10:.., 30:.., 60:..},
           "eta_seconds": float|None, "eta_human": str},
          ...
        ],
      }
    Missing tables degrade to zeros rather than raising (a fresh DB has no
    queues yet). Read-only.
    """
    out: list[dict] = []
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        for p in _PIPELINES:
            if not _table_exists(conn, p["queue_table"]):
                queue_len = 0
            else:
                queue_len = _count(conn, p["queue_table"])
            rates: dict[int, float] = {}
            if _table_exists(conn, p["produced_table"]):
                for w in WINDOWS_MIN:
                    produced = _produced_in_window(
                        conn, p["produced_table"], p["produced_ts"],
                        p["produced_where"], w,
                    )
                    rates[w] = produced / w  # items per minute over the window
            else:
                rates = {w: 0.0 for w in WINDOWS_MIN}
            # Use the shortest window with signal for the freshest ETA, else the
            # longest window (smoother) — prefer recent but fall back if idle now.
            recent_rate = rates.get(1) or rates.get(10) or rates.get(30) or rates.get(60) or 0.0
            eta = _eta_seconds(queue_len, recent_rate)
            out.append({
                "key": p["key"],
                "label": p["label"],
                "queue_len": queue_len,
                "rates": rates,
                "eta_seconds": eta,
                "eta_human": _human_eta(eta),
            })
    finally:
        conn.close()
    return {"pipelines": out}


def _human_eta(eta_seconds: Optional[float]) -> str:
    if eta_seconds is None:
        return "stalled (no recent throughput)"
    if eta_seconds <= 0:
        return "drained"
    m = eta_seconds / 60.0
    if m < 1:
        return f"~{int(eta_seconds)}s"
    if m < 60:
        return f"~{int(m)}m"
    return f"~{m/60:.1f}h"


def collect_governor(db_path: str) -> dict:
    """Governor telemetry + current pacing verdict. Best-effort: returns a
    zeroed/degraded dict if the SDK isn't importable."""
    try:
        from m3_sdk import M3Context, _governor_thresholds, get_governor_pacing
    except Exception:  # noqa: BLE001
        return {"available": False}
    try:
        ctx = M3Context.for_db(db_path)
        tel = ctx.get_system_telemetry()
        pacing = get_governor_pacing(tel)
        initial, limit = _governor_thresholds()
        load = max(tel.get("cpu_total", 0.0), tel.get("ram_total", 0.0), tel.get("gpu_total", 0.0))
        return {
            "available": True,
            "cpu": tel.get("cpu_total", 0.0),
            "ram": tel.get("ram_total", 0.0),
            "gpu": tel.get("gpu_total", 0.0),
            "thermal": tel.get("thermal", "Nominal"),
            "load": load,
            "mode": pacing.get("background", "?"),
            "initial_threshold": initial,
            "limit_threshold": limit,
        }
    except Exception:  # noqa: BLE001
        return {"available": False}
