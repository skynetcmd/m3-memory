"""Trust & corroboration ledger operations (knowledge-maintenance Phase 2).

DB-touching counterpart to the pure `confidence` module. Reads/writes the
agents.trust_score column and the append-only memory_corroborations ledger, and
re-aggregates a memory's stored confidence from those signals.

All functions are ABSENCE-TOLERANT: on a DB that predates migration 036 (no
trust_score / no ledger) they degrade to neutral defaults rather than raising, so
the write path stays safe across deployments at different migration states.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from . import confidence as _conf

logger = logging.getLogger("memory.trust")

TRUST_FLOOR = _conf.TRUST_MIN
TRUST_CEIL = _conf.TRUST_MAX


def _is_missing(exc) -> bool:
    """True for the pre-036 'no such column/table' errors we tolerate."""
    msg = str(exc).lower()
    return ("no such column" in msg or "no such table" in msg
            or "no column named" in msg)


def get_agent_trust(db, agent_id: str) -> float:
    """Return an agent's trust_score, or neutral 1.0 if unknown / pre-036."""
    if not agent_id:
        return _conf.TRUST_NEUTRAL
    try:
        row = db.execute(
            "SELECT trust_score FROM agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()
    except Exception as e:  # noqa: BLE001 — pre-036 DB has no trust_score
        if _is_missing(e):
            return _conf.TRUST_NEUTRAL
        raise
    if not row or row[0] is None:
        return _conf.TRUST_NEUTRAL
    return _conf.clamp_trust(float(row[0]))


def set_agent_trust(db, agent_id: str, trust_score: float) -> float:
    """Explicitly set an agent's trust_score (clamped [0.5,1.0]). Upserts the
    agent row if absent. Returns the stored value. Raises on pre-036 DBs (the
    caller — agent_set_trust tool — should surface that clearly)."""
    value = _conf.clamp_trust(float(trust_score))
    now = datetime.now(timezone.utc).isoformat()
    # Upsert: create a minimal agent row if it doesn't exist yet.
    db.execute(
        "INSERT INTO agents (agent_id, trust_score, last_seen) VALUES (?, ?, ?) "
        "ON CONFLICT(agent_id) DO UPDATE SET trust_score = excluded.trust_score",
        (agent_id, value, now),
    )
    return value


def corroboration_inputs(db, memory_id: str) -> tuple[float, int]:
    """Return (distinct_trust_sum, contradiction_count) for a memory from its
    ledger. distinct_trust_sum = summed trust of distinct positive-delta sources;
    contradiction_count = number of negative-delta events. Neutral (0.0, 0) on a
    pre-036 DB."""
    try:
        rows = db.execute(
            "SELECT source_kind, source_ref, trust_at_write, delta "
            "FROM memory_corroborations WHERE memory_id = ?",
            (memory_id,),
        ).fetchall()
    except Exception as e:  # noqa: BLE001 — pre-036 DB has no ledger
        if _is_missing(e):
            return 0.0, 0
        raise
    distinct: dict[tuple, float] = {}
    contradictions = 0
    for r in rows:
        kind, ref, trust, delta = r[0], r[1], float(r[2] or 1.0), float(r[3] or 0.0)
        if delta > 0:
            # Keep the max trust seen for a given distinct source.
            key = (kind, ref)
            distinct[key] = max(distinct.get(key, 0.0), trust)
        elif delta < 0:
            contradictions += 1
    return sum(distinct.values()), contradictions


def record_corroboration(db, memory_id: str, *, source_kind: str, source_ref: str,
                         trust_at_write: float, delta: float) -> bool:
    """Append a corroboration (delta>0) or contradiction (delta<0) event.

    Idempotent for positive deltas via the unique (memory_id, source_kind,
    source_ref) index — a source corroborating the same memory twice is a no-op.
    Returns True if a new row landed, False if it was a dedup no-op or the ledger
    is absent (pre-036). Never raises on the tolerated missing-table case.
    """
    try:
        # Dedup is on the PARTIAL unique index (memory_id, source_kind,
        # source_ref) WHERE delta > 0 — NOT the random-uuid PK. On SQLite the
        # dialect emits "INSERT OR IGNORE" with an empty suffix (unchanged); on
        # Postgres it emits "INSERT INTO ... ON CONFLICT (...) WHERE delta > 0 DO
        # NOTHING", naming that exact partial index so the conflict is caught.
        from memory.backends import active_backend

        _d = active_backend().dialect()
        _ins = _d.insert_or_ignore()
        _suffix = _d.on_conflict_ignore(
            conflict_target="(memory_id, source_kind, source_ref)",
            index_predicate="delta > 0",
        )
        cur = db.execute(
            f"{_ins} memory_corroborations "
            "(id, memory_id, source_kind, source_ref, trust_at_write, delta) "
            f"VALUES ({_d.placeholder(6)}) {_suffix}".rstrip(),
            (str(uuid.uuid4()), memory_id, source_kind, source_ref,
             _conf.clamp_trust(trust_at_write), float(delta)),
        )
        return cur.rowcount > 0
    except Exception as e:  # noqa: BLE001 — pre-036 DB has no ledger
        if _is_missing(e):
            return False
        raise


def reaggregate_confidence(db, memory_id: str) -> "float | None":
    """Recompute and store a memory's confidence from its current provenance +
    ledger. Returns the new value, or None if unsupported (pre-035/036). Keeps
    corroboration_count / contradiction_count columns in sync with the ledger."""
    try:
        row = db.execute(
            "SELECT source, change_agent, metadata_json FROM memory_items WHERE id = ?",
            (memory_id,),
        ).fetchone()
    except Exception as e:  # noqa: BLE001
        if _is_missing(e):
            return None
        raise
    if not row:
        return None
    source, change_agent, metadata = row[0] or "", row[1] or "", row[2] or "{}"
    trust_sum, contradictions = corroboration_inputs(db, memory_id)
    # distinct positive sources ≈ count of contributing keys; recompute count.
    corr_count = _distinct_positive_count(db, memory_id)
    observer = _observer_conf(metadata)
    value = _conf.aggregate(
        source=source,
        change_agent=change_agent,
        observer_confidence=observer,
        distinct_trust_sum=trust_sum,
        contradiction_count=contradictions,
    )
    try:
        db.execute(
            "UPDATE memory_items SET confidence = ?, "
            "corroboration_count = ?, contradiction_count = ? WHERE id = ?",
            (value, corr_count, contradictions, memory_id),
        )
    except Exception as e:  # noqa: BLE001 — pre-035 DB lacks the columns
        if _is_missing(e):
            return None
        raise
    return value


def _distinct_positive_count(db, memory_id: str) -> int:
    try:
        row = db.execute(
            "SELECT COUNT(DISTINCT source_kind || '|' || source_ref) "
            "FROM memory_corroborations WHERE memory_id = ? AND delta > 0",
            (memory_id,),
        ).fetchone()
        return int(row[0]) if row else 0
    except Exception as e:  # noqa: BLE001
        if _is_missing(e):
            return 0
        raise


def _observer_conf(metadata) -> "float | None":
    import json
    try:
        meta = json.loads(metadata) if isinstance(metadata, str) else (metadata or {})
        if isinstance(meta, dict) and meta.get("confidence") is not None:
            return float(meta["confidence"])
    except (ValueError, TypeError):
        pass
    return None
