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
    {
        # Entity extraction: pull named entities out of memories. This is the
        # queue the Graph Explorer's "Queue Pending" card counts — surfaced here
        # so System Health and the Explorer agree on the same number.
        # queue_where counts only GENUINELY-pending rows. Two exclusions:
        #  1. Completed rows: the table KEEPS processed rows as status='done'
        #     (not purged), so a plain COUNT(*) grows forever and falsely reads as
        #     a growing backlog. Not-done rows (NULL/failed) are still eligible.
        #  2. Orphaned rows: a row whose memory was DELETED (soft-delete, or the
        #     row is gone) will NEVER be picked up — the extraction worker only
        #     selects live memories (is_deleted=0). Such rows would otherwise
        #     count as "pending" forever (they can't clear). The correlated EXISTS
        #     keeps only rows pointing at a live memory. (This mirrors the worker's
        #     own eligibility filter, so the count matches reality.)
        "key": "entities",
        "label": "Entity extraction",
        "queue_table": "entity_extraction_queue",
        "queue_ts": "enqueued_at",
        "queue_where": (
            "COALESCE(status,'') != 'done' AND EXISTS ("
            "SELECT 1 FROM memory_items mi WHERE mi.id = entity_extraction_queue.memory_id "
            "AND COALESCE(mi.is_deleted,0) = 0)"
        ),
        "produced_table": "entities",
        "produced_ts": "created_at",
        "produced_where": "",
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
_ALLOWED_QUEUE_WHERE: frozenset[str] = frozenset(
    p["queue_where"] for p in _PIPELINES if p.get("queue_where")
)


def _safe_ident(value: str, allowed: frozenset[str], kind: str) -> str:
    """Return `value` only if it's in the allowlist; else raise. Guards the
    identifier interpolation in the COUNT queries below."""
    if value not in allowed:
        raise ValueError(f"unsafe {kind} identifier {value!r} (not in allowlist)")
    return value


def _dialect():
    """The active backend's SQL dialect (placeholder/table_exists/time math).
    Backend-agnostic: SQLite, PostgreSQL, and any future SQL backend."""
    from memory.backends import dialect
    return dialect()


def _ph(n: int = 1) -> str:
    """`n` positional placeholders in the active backend's style (?, or %s)."""
    return _dialect().placeholder(n)


def _table_exists(conn, name: str) -> bool:
    """Backend-agnostic table-existence probe (dialect().table_exists → sqlite_master
    / to_regclass / information_schema per backend)."""
    try:
        sql, params = _dialect().table_exists(name)
        return conn.execute(sql, params).fetchone() is not None
    except Exception:  # noqa: BLE001 — treat a probe failure as "absent"
        return False


def _entity_backlog_count(conn) -> int:
    """The REAL entity-extraction backlog: memories that still need entities.

    Entity extraction is NOT queue-driven — the worker scans memory_items
    directly for LIVE, PRODUCTION (variant IS NULL) memories of an extractable
    type that lack a memory_item_entities row and aren't in a terminal queue
    state. Counting entity_extraction_queue rows measures nothing — it's a
    DONE-MARKER LOG, not a work queue, so it reads ~0 while a big backlog grinds.
    Mirrors the worker's eligibility (bin/m3_entities, which runs
    --source-variant __none__), INCLUDING the variant IS NULL scope — without it
    the count includes variant-tagged rows (bench corpora) the worker never
    processes, so the number sits stuck forever. Backend-agnostic (dialect
    placeholders); best-effort → 0 if tables/columns are absent.
    """
    # Extractable types + always-skip, kept in sync with bin/m3_entities.
    DEFAULT_TYPES = (
        "message", "conversation", "chat_log", "note", "decision", "knowledge",
        "reference", "fact", "plan", "document", "observation", "project",
        "config", "infrastructure", "network_config", "local_device",
        "home_automation", "log", "preference",
    )
    SKIP = ("auto", "scratchpad", "summary")
    try:
        if not (_table_exists(conn, "memory_items") and _table_exists(conn, "memory_item_entities")):
            return 0
        has_queue = _table_exists(conn, "entity_extraction_queue")
        type_ph = _ph(len(DEFAULT_TYPES))
        skip_ph = _ph(len(SKIP))
        # Mirror the worker's TERMINAL statuses, not just 'done': the worker
        # (bin/m3_entities._query_eligible_rows) also excludes 'ctx_error' and
        # 'failed' rows past the retry cap. Counting only 'done' would leave those
        # terminal rows reading as "pending" forever.
        done_clause = (
            " AND mi.id NOT IN (SELECT memory_id FROM entity_extraction_queue"
            "                   WHERE status IN ('done', 'ctx_error'))"
            if has_queue else ""
        )
        sql = (
            "SELECT COUNT(*) FROM memory_items mi "
            "WHERE COALESCE(mi.is_deleted,0)=0 "
            # variant IS NULL: the loop's entity pass runs --source-variant __none__,
            # so it ONLY processes production (variant-NULL) memories. Variant-tagged
            # rows (e.g. bench corpora) are never eligible — WITHOUT this filter the
            # backlog counts rows the worker will never touch, so the number sits
            # "stuck" forever regardless of how long the worker runs.
            "AND mi.variant IS NULL "
            f"AND mi.type IN ({type_ph}) AND mi.type NOT IN ({skip_ph}) "
            "AND mi.id NOT IN (SELECT DISTINCT memory_id FROM memory_item_entities)"
            + done_clause
        )
        return conn.execute(sql, (*DEFAULT_TYPES, *SKIP)).fetchone()[0]
    except Exception:  # noqa: BLE001 — best-effort
        return 0


def _count(conn, table: str, where: str = "") -> int:
    """COUNT(*) of a queue table, optionally filtered by a validated WHERE
    fragment (e.g. exclude status='done' rows that are kept but not pending)."""
    table = _safe_ident(table, _ALLOWED_TABLES, "table")
    if where:
        where = _safe_ident(where, _ALLOWED_QUEUE_WHERE, "queue-where")
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _produced_in_window(conn, table: str, ts_col: str, where: str, minutes: int) -> int:
    """Rows produced in the last `minutes`. Backend-agnostic time math via
    dialect().now_minus_minutes (SQLite datetime vs PG NOW()-interval). SQL does
    the filtering. `table`/`ts_col`/`where` are validated against _PIPELINES
    allowlists — no unvalidated identifier reaches the query string."""
    table = _safe_ident(table, _ALLOWED_TABLES, "table")
    ts_col = _safe_ident(ts_col, _ALLOWED_TS_COLS, "timestamp column")
    _d = _dialect()
    clause = f"{ts_col} > {_d.now_minus_minutes(_d.placeholder())}"
    if where:
        clause += f" AND {_safe_ident(where, _ALLOWED_WHERE, 'where-clause')}"
    sql = f"SELECT COUNT(*) FROM {table} WHERE {clause}"
    return conn.execute(sql, (int(minutes),)).fetchone()[0]


def _entity_memories_processed(conn, minutes: int) -> int:
    """DISTINCT memories that gained entities in the last `minutes` — the correct
    throughput unit for the entity backlog (which is measured in MEMORIES).

    The old rate counted rows in `entities` (many per memory → wildly inflated
    rate → absurdly short ETA, e.g. '~54s' for thousands of items). This counts
    distinct memory_ids processed, so rate and backlog share the same unit.
    Backend-agnostic; best-effort → 0 if tables absent."""
    try:
        if not (_table_exists(conn, "memory_item_entities") and _table_exists(conn, "entities")):
            return 0
        _d = _dialect()
        sql = (
            "SELECT COUNT(DISTINCT me.memory_id) FROM memory_item_entities me "
            "JOIN entities e ON e.id = me.entity_id "
            f"WHERE e.created_at > {_d.now_minus_minutes(_d.placeholder())}"
        )
        return conn.execute(sql, (int(minutes),)).fetchone()[0]
    except Exception:  # noqa: BLE001 — best-effort
        return 0


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
    Backend-agnostic: reads through the active storage backend's seam
    (SQLite/PostgreSQL/…), dialect placeholders, and dialect time math — NOT a
    raw sqlite3 connection. Uses ``open_readonly(db_path)`` which HONORS the
    specific ``db_path`` on file backends (SQLite) and ignores it on pooled
    backends (PostgreSQL: one store, db_path is meaningless). Missing tables
    degrade to zeros rather than raising. Read-only.
    """
    out: list[dict] = []
    try:
        from memory.backends import active_backend
        _cm = active_backend().open_readonly(db_path)
    except Exception:  # noqa: BLE001 — seam unavailable → empty (never crash)
        return {"pipelines": []}
    with _cm as conn:
        for p in _PIPELINES:
            if p["key"] == "entities":
                # Entity extraction is scan-driven, not queue-driven — count the
                # REAL backlog (memories lacking entities), not the done-marker log.
                queue_len = _entity_backlog_count(conn)
            elif not _table_exists(conn, p["queue_table"]):
                queue_len = 0
            else:
                queue_len = _count(conn, p["queue_table"], p.get("queue_where", ""))
            rates: dict[int, float] = {}
            if p["key"] == "entities":
                # Entity throughput is measured in MEMORIES processed (not entity
                # rows — many per memory), so rate and backlog share one unit.
                for w in WINDOWS_MIN:
                    rates[w] = _entity_memories_processed(conn, w) / w
            elif _table_exists(conn, p["produced_table"]):
                for w in WINDOWS_MIN:
                    produced = _produced_in_window(
                        conn, p["produced_table"], p["produced_ts"],
                        p["produced_where"], w,
                    )
                    rates[w] = produced / w  # items per minute over the window
            else:
                rates = {w: 0.0 for w in WINDOWS_MIN}
            # Prefer a STABLE window for the ETA: the 1-min window is jumpy (a
            # burst reads as a huge rate → absurdly short ETA). Use the longest
            # window with signal (smoother, honest), falling back to shorter ones.
            recent_rate = rates.get(60) or rates.get(30) or rates.get(10) or rates.get(1) or 0.0
            eta = _eta_seconds(queue_len, recent_rate)
            out.append({
                "key": p["key"],
                "label": p["label"],
                "queue_len": queue_len,
                "rates": rates,
                "eta_seconds": eta,
                "eta_human": _human_eta(eta),
            })
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
