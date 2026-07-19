import base64
import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

DEFAULT_PROTECTED_TYPES = ("preference", "user_fact", "task", "plan")

import memory_core
from memory_core import (
    ARCHIVE_DB_PATH,
    DEDUP_LIMIT,
    DEDUP_THRESHOLD,
    EMBED_DIM,
    _content_hash,
    _cosine,
    _db,
    _embed,
    _get_embed_client,
    _pack,
    _unpack,
    ctx,
    get_best_llm,
    m3_core_rs,
    memory_link_impl,
)

logger = logging.getLogger("memory_maintenance")

def _archive_conn():
    conn = sqlite3.connect(ARCHIVE_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _transfer_to_archive(item_id, reason, db):
    now = datetime.now(timezone.utc).isoformat()
    row = db.execute("SELECT * FROM memory_items WHERE id = ?", (item_id,)).fetchone()
    if not row: return False
    adb = _archive_conn()
    try:
        adb.execute("INSERT OR REPLACE INTO archived_items (id, content, archive_reason, archived_at) VALUES (?,?,?,?)",
                    (row["id"], row["content"], reason, now))
        adb.commit()
        return True
    except Exception:
        adb.rollback()
        return False
    finally: adb.close()

def memory_dedup_impl(threshold=DEDUP_THRESHOLD, dry_run=True, limit=0):
    import time

    from m3_sdk import _LAST_USER_INTERACTION
    if time.time() - _LAST_USER_INTERACTION < 15.0:
        logger.info("Active query session detected. Suspending curation pass to yield resources.")
        time.sleep(5.0)

    """Find near-duplicate memory items by cosine similarity over embeddings.

    Returns a structured dict:
      {
        "count": <int total groups found>,
        "groups": [
            {"a": <id>, "b": <id>, "title_a": <str>, "title_b": <str>, "score": <float>},
            ...
        ],
        "threshold": <float>,
        "scanned": <int rows scanned>,
        "applied": <bool — True if dry_run=False and duplicates were soft-deleted>,
      }

    Why structured: prior to 2026-05-17 this returned the bare string
    "Found N duplicate groups." which is information-free for any caller
    that wanted to act on the duplicates. The curate-memory agent had to
    fall back to direct sqlite queries to enumerate the pairs, ballooning
    survey-phase tool calls from ~2 to 30+. The structured return gives
    the caller everything it needs from one round-trip.

    `limit`: cap the returned `groups` list at this many entries (0 = no cap).
    `count` always reflects the true total found, even when groups is trimmed.
    Use this to keep payloads tractable on stores with many duplicates.

    Soft-delete behavior on dry_run=False is unchanged from the legacy impl.
    """
    with _db() as db:
        rows = db.execute(
            f"SELECT me.memory_id, me.embedding, mi.title FROM memory_embeddings me "
            f"JOIN memory_items mi ON me.memory_id = mi.id "
            f"WHERE mi.is_deleted = 0 ORDER BY mi.created_at DESC LIMIT {DEDUP_LIMIT}"
        ).fetchall()

    # Rust hot path: concatenate all packed blobs into one contiguous bytes
    # buffer, then for each row i call cosine_batch_packed_flat over the
    # tail slice blobs[i+1:]. Each FFI hop scores up to N-1 cosines in
    # parallel via rayon with zero per-row Python→Rust copies. 499,500
    # pair-cosines on a 1000-row scan collapses from ~4.3s of pure-Python
    # _cosine() loops to <0.1s of Rust SIMD.
    #
    # Python fallback (m3_core_rs unavailable, or the Rust path errors out
    # mid-scan) preserves the original per-pair _cosine semantics so the
    # dedup output is byte-identical across paths.
    ids = [r["memory_id"] for r in rows]
    titles = [r["title"] for r in rows]
    raw_blobs = [r["embedding"] for r in rows]
    n = len(ids)
    bytes_per_row = EMBED_DIM * 4
    duplicates: list[tuple] = []  # (id_a, id_b, title_a, title_b, score)
    seen: set[str] = set()

    use_rust = m3_core_rs is not None and n > 1 and all(
        isinstance(b, (bytes, bytearray)) and len(b) == bytes_per_row for b in raw_blobs
    )

    if use_rust:
        flat = b"".join(bytes(b) if isinstance(b, bytearray) else b for b in raw_blobs)
        # Decode each row once for the `query` argument; the candidate side
        # stays as raw bytes inside `flat`.
        unpacked = [_unpack(b) for b in raw_blobs]
        try:
            for i in range(n):
                if ids[i] in seen:
                    continue
                tail = flat[(i + 1) * bytes_per_row :]
                if not tail:
                    break
                scores = m3_core_rs.cosine_batch_packed_flat(unpacked[i], tail, EMBED_DIM)
                # scores[k] is the cosine of row i vs row (i+1+k)
                for k, score in enumerate(scores):
                    j = i + 1 + k
                    if ids[j] in seen:
                        continue
                    # Skip self-pairs: when a memory has multiple embedding
                    # rows (e.g. v022 dual-embed default+enriched), the same
                    # memory_id shows up at multiple indices. Without this
                    # guard the scan emits {a: X, b: X, score: 1.0} pairs.
                    if ids[i] == ids[j]:
                        continue
                    if score >= threshold:
                        duplicates.append(
                            (ids[i], ids[j], titles[i], titles[j], float(score))
                        )
                        seen.add(ids[j])
        except Exception as e:  # noqa: BLE001 — fall back rather than fail the survey
            logger.warning(
                f"dedup Rust path failed mid-scan ({type(e).__name__}: {e}); "
                f"falling back to pure-Python loop"
            )
            duplicates = []
            seen = set()
            use_rust = False

    if not use_rust:
        # Pure-Python fallback (original loop, byte-identical semantics).
        items = [(ids[i], _unpack(raw_blobs[i]), titles[i]) for i in range(n)]
        for i, (mid_a, vec_a, title_a) in enumerate(items):
            if mid_a in seen:
                continue
            for j in range(i + 1, len(items)):
                mid_b, vec_b, title_b = items[j]
                if mid_b in seen:
                    continue
                # Skip self-pairs (same memory_id at two indices when a
                # memory has multiple embedding rows — see Rust path note).
                if mid_a == mid_b:
                    continue
                score = _cosine(vec_a, vec_b)
                if score >= threshold:
                    duplicates.append((mid_a, mid_b, title_a, title_b, float(score)))
                    seen.add(mid_b)

    applied = False
    if not dry_run and duplicates:
        with _db() as db:
            for _, mid_b, _, _, _ in duplicates:
                db.execute("UPDATE memory_items SET is_deleted = 1 WHERE id = ?", (mid_b,))
        applied = True

    groups = duplicates if not limit else duplicates[: int(limit)]
    return {
        "count": len(duplicates),
        "groups": [
            {"a": a, "b": b, "title_a": ta, "title_b": tb, "score": round(sc, 4)}
            for a, b, ta, tb, sc in groups
        ],
        "threshold": float(threshold),
        "scanned": n,
        "applied": applied,
    }

def memory_feedback_impl(memory_id, feedback="useful"):
    fb = feedback.lower()
    with _db() as db:
        if fb == "useful":
            db.execute("UPDATE memory_items SET importance = MIN(1.0, importance + 0.1) WHERE id = ?", (memory_id,))
        elif fb == "wrong":
            db.execute("UPDATE memory_items SET is_deleted = 1 WHERE id = ?", (memory_id,))
    return f"Feedback '{fb}' applied to {memory_id}"

def _reinforce_confidence(db):
    """Reinforcement pass (knowledge-maintenance Phase 3): make confidence a
    living signal.

      1. Re-aggregate confidence for memories with corroboration-ledger activity
         (the only ones whose evidence changed) — corroboration raised it,
         contradiction lowered it. Reuses the Phase-2 ledger aggregation.
      2. Decay-toward-neutral for memories that have a confidence but NO recent
         reinforcement (no ledger row, not accessed in the decay window). Unlike
         importance (which decays toward 0), confidence forgets toward
         *uncertainty* (NEUTRAL=0.5) — a fact nobody reconfirmed becomes less
         certain, not worthless. Done as ONE set-based UPDATE (§4): the pure
         decay_toward_neutral() is a linear interpolation toward NEUTRAL, which
         SQL expresses directly.

    ABSENCE-TOLERANT: a pre-035/036 DB (no confidence column / no ledger) makes
    this a no-op. Returns (reaggregated, decayed) counts.
    """
    from memory import confidence as _conf
    from memory import trust as _trust

    reaggregated = 0
    decayed = 0
    # Bind BEFORE the try: if the ledger query below fails with a missing-schema
    # error, the except swallows it (pre-036 DB has no ledger) and execution falls
    # through to the decay block, which reads active_ids — so it must always be
    # defined, or that path raises UnboundLocalError (observed in the cognitive
    # loop's maintenance pass).
    active_ids: set = set()
    try:
        # (1) Re-aggregate the memories with ledger activity.
        active = db.execute(
            "SELECT DISTINCT memory_id FROM memory_corroborations"
        ).fetchall()
        active_ids = {r[0] for r in active}
        for mid in active_ids:
            if _trust.reaggregate_confidence(db, mid) is not None:
                reaggregated += 1
    except Exception as e:  # noqa: BLE001 — pre-036 DB has no ledger
        if not _is_missing_schema(e):
            raise

    try:
        # (2) Decay un-reinforced memories toward NEUTRAL in one UPDATE.
        # new = c + (NEUTRAL - c) * DECAY_RATE, clamped, skipping rows touched by
        # the ledger (already re-aggregated) and rows accessed in the last 7 days.
        placeholders = ",".join("?" * len(active_ids)) if active_ids else "''"
        params = [_conf.NEUTRAL, _conf.DECAY_RATE, *active_ids]
        try:
            res = db.execute(
                f"""
                UPDATE memory_items
                   SET confidence = MAX(0.0, MIN(1.0,
                           confidence + (? - confidence) * ?))
                 WHERE is_deleted = 0
                   AND confidence IS NOT NULL
                   AND id NOT IN ({placeholders})
                   AND (last_accessed_at IS NULL
                        OR julianday('now') - julianday(last_accessed_at) > 7)
                   AND COALESCE(pinned, 0) = 0
                """,
                params,
            )
        except Exception as e:  # noqa: BLE001 — pre-pinned-column DB
            if not _is_missing_schema(e):
                raise
            res = db.execute(
                f"""
                UPDATE memory_items
                   SET confidence = MAX(0.0, MIN(1.0,
                           confidence + (? - confidence) * ?))
                 WHERE is_deleted = 0
                   AND confidence IS NOT NULL
                   AND id NOT IN ({placeholders})
                   AND (last_accessed_at IS NULL
                        OR julianday('now') - julianday(last_accessed_at) > 7)
                """,
                params,
            )
        decayed = res.rowcount
    except Exception as e:  # noqa: BLE001 — pre-035 DB has no confidence column
        if not _is_missing_schema(e):
            raise

    return reaggregated, decayed


def _is_missing_schema(exc) -> bool:
    """True for the pre-035/036 'no such column/table' errors the reinforcement
    pass tolerates (degrades to a no-op rather than failing maintenance)."""
    msg = str(exc).lower()
    return "no such column" in msg or "no such table" in msg or "no column named" in msg


def memory_lifecycle_summary_impl(window_days: int = 7, top_n: int = 5) -> dict:
    """Windowed summary of what the memory system did to itself.

    Aggregates two append-only, timestamp-indexed ledgers over the last
    ``window_days`` days:
      * ``memory_history`` (mig 009) — create / update / delete / supersede
        events (``event`` column, indexed on ``created_at``).
      * ``memory_corroborations`` (mig 036) — corroboration (``delta>0``) and
        contradiction (``delta<0``) events.

    Returns a structured dict so an agent can narrate "we updated this belief 3
    times" and an operator can see lifecycle churn at a glance. Read-only; no
    writes, no background job. Both queries hit existing indexes.

    Old-DB tolerance: ``memory_corroborations`` only exists post-036. A missing
    table degrades that section to zero counts (via ``_is_missing_schema``)
    rather than failing the whole summary — mirroring the reinforcement pass.
    """
    window_days = max(1, int(window_days))
    top_n = max(0, int(top_n))
    cutoff = f"-{window_days} days"
    out: dict = {
        "window_days": window_days,
        "events": {"create": 0, "update": 0, "delete": 0, "supersede": 0},
        "corroboration": {"corroborated": 0, "contradicted": 0},
        "top_contradicted": [],
        "most_revised": [],
    }
    with _db() as db:
        db.row_factory = sqlite3.Row
        # Lifecycle events by type in the window.
        try:
            for row in db.execute(
                "SELECT event, COUNT(*) AS n FROM memory_history "
                "WHERE created_at >= datetime('now', ?) GROUP BY event",
                (cutoff,),
            ):
                if row["event"] in out["events"]:
                    out["events"][row["event"]] = row["n"]
        except sqlite3.OperationalError as e:
            if not _is_missing_schema(e):
                raise  # a real error, not a pre-009 DB

        # Most-revised memories (update + supersede events per memory_id).
        if top_n:
            try:
                out["most_revised"] = [
                    {"memory_id": r["memory_id"], "revisions": r["n"], "title": r["title"]}
                    for r in db.execute(
                        "SELECT h.memory_id AS memory_id, COUNT(*) AS n, "
                        "       COALESCE(m.title, '') AS title "
                        "FROM memory_history h "
                        "LEFT JOIN memory_items m ON m.id = h.memory_id "
                        "WHERE h.created_at >= datetime('now', ?) "
                        "  AND h.event IN ('update', 'supersede') "
                        "GROUP BY h.memory_id ORDER BY n DESC LIMIT ?",
                        (cutoff, top_n),
                    )
                ]
            except sqlite3.OperationalError as e:
                if not _is_missing_schema(e):
                    raise

        # Corroboration vs contradiction in the window (post-036 table).
        try:
            for row in db.execute(
                "SELECT CASE WHEN delta > 0 THEN 'corroborated' ELSE 'contradicted' END AS kind, "
                "       COUNT(*) AS n FROM memory_corroborations "
                "WHERE created_at >= datetime('now', ?) GROUP BY kind",
                (cutoff,),
            ):
                out["corroboration"][row["kind"]] = row["n"]
        except sqlite3.OperationalError as e:
            if not _is_missing_schema(e):
                raise  # pre-036 DB → leave zeros

        # Most-contradicted memories (post-036 table).
        if top_n:
            try:
                out["top_contradicted"] = [
                    {"memory_id": r["memory_id"], "contradiction_count": r["n"], "title": r["title"]}
                    for r in db.execute(
                        "SELECT c.memory_id AS memory_id, COUNT(*) AS n, "
                        "       COALESCE(m.title, '') AS title "
                        "FROM memory_corroborations c "
                        "LEFT JOIN memory_items m ON m.id = c.memory_id "
                        "WHERE c.created_at >= datetime('now', ?) AND c.delta < 0 "
                        "GROUP BY c.memory_id ORDER BY n DESC LIMIT ?",
                        (cutoff, top_n),
                    )
                ]
            except sqlite3.OperationalError as e:
                if not _is_missing_schema(e):
                    raise
    return out


def _enforce_retention_policies(db):
    """Enforce per-agent memory limits and TTLs from agent_retention_policies table."""
    try:
        policies = db.execute("SELECT * FROM agent_retention_policies").fetchall()
    except Exception:
        return 0  # Table may not exist yet
    purged = 0
    for p in policies:
        agent_id = p["agent_id"]
        # TTL enforcement
        if p["ttl_days"] and p["ttl_days"] > 0:
            try:
                res = db.execute(
                    "UPDATE memory_items SET is_deleted = 1 WHERE agent_id = ? AND is_deleted = 0 "
                    "AND julianday('now') - julianday(created_at) > ? "
                    "AND COALESCE(pinned, 0) = 0",
                    (agent_id, p["ttl_days"])
                )
            except Exception as e:  # noqa: BLE001 — pre-pinned-column DB
                if not _is_missing_schema(e):
                    raise
                res = db.execute(
                    "UPDATE memory_items SET is_deleted = 1 WHERE agent_id = ? AND is_deleted = 0 "
                    "AND julianday('now') - julianday(created_at) > ?",
                    (agent_id, p["ttl_days"])
                )
            purged += res.rowcount
        # Max count enforcement (keep newest, soft-delete oldest excess)
        if p["max_memories"] and p["max_memories"] > 0:
            try:
                excess = db.execute(
                    "SELECT id FROM memory_items WHERE agent_id = ? AND is_deleted = 0 "
                    "AND COALESCE(pinned, 0) = 0 "
                    "ORDER BY created_at DESC LIMIT -1 OFFSET ?",
                    (agent_id, p["max_memories"])
                ).fetchall()
            except Exception as e:  # noqa: BLE001 — pre-pinned-column DB
                if not _is_missing_schema(e):
                    raise
                excess = db.execute(
                    "SELECT id FROM memory_items WHERE agent_id = ? AND is_deleted = 0 "
                    "ORDER BY created_at DESC LIMIT -1 OFFSET ?",
                    (agent_id, p["max_memories"])
                ).fetchall()
            for row in excess:
                if p["auto_archive"]:
                    _transfer_to_archive(row["id"], "retention_limit", db)
                db.execute("UPDATE memory_items SET is_deleted = 1 WHERE id = ?", (row["id"],))
                purged += 1
    return purged

def memory_maintenance_impl(decay=True, purge_expired=True, prune_orphan_embeddings=True,
                            reinforce=True, prune_orphan_queues=True):
    import time

    from m3_sdk import _LAST_USER_INTERACTION
    if time.time() - _LAST_USER_INTERACTION < 15.0:
        logger.info("Active query session detected. Suspending curation pass to yield resources.")
        time.sleep(5.0)

    now = datetime.now(timezone.utc).isoformat()
    report = []
    with _db() as db:
        if decay:
            try:
                res = db.execute(
                    "UPDATE memory_items SET importance = MAX(0.0, importance * 0.995) "
                    "WHERE is_deleted = 0 AND julianday('now') - julianday(created_at) > 7 "
                    "AND COALESCE(pinned, 0) = 0"
                )
            except Exception as e:  # noqa: BLE001 — pre-pinned-column DB
                if not _is_missing_schema(e):
                    raise
                res = db.execute(
                    "UPDATE memory_items SET importance = MAX(0.0, importance * 0.995) "
                    "WHERE is_deleted = 0 AND julianday('now') - julianday(created_at) > 7"
                )
            report.append(f"Decayed {res.rowcount} items")
        if reinforce:
            # Confidence reinforcement (Phase 3): re-aggregate ledger-active
            # memories, decay the un-reinforced toward NEUTRAL. No-op on pre-035/
            # 036 DBs. Distinct from importance decay above (toward 0).
            r_count, d_count = _reinforce_confidence(db)
            if r_count or d_count:
                report.append(
                    f"Confidence: reaggregated {r_count}, decayed {d_count} toward neutral"
                )
        if purge_expired:
            try:
                expired = db.execute(
                    "SELECT id FROM memory_items WHERE expires_at < ? AND COALESCE(pinned, 0) = 0",
                    (now,),
                ).fetchall()
            except Exception as e:  # noqa: BLE001 — pre-pinned-column DB
                if not _is_missing_schema(e):
                    raise
                expired = db.execute("SELECT id FROM memory_items WHERE expires_at < ?", (now,)).fetchall()
            for row in expired: _transfer_to_archive(row[0], "expired", db)
            try:
                res = db.execute(
                    "DELETE FROM memory_items WHERE expires_at < ? AND COALESCE(pinned, 0) = 0",
                    (now,),
                )
            except Exception as e:  # noqa: BLE001 — pre-pinned-column DB
                if not _is_missing_schema(e):
                    raise
                res = db.execute("DELETE FROM memory_items WHERE expires_at < ?", (now,))
            report.append(f"Purged {res.rowcount} expired")
        if prune_orphan_embeddings:
            res = db.execute("DELETE FROM memory_embeddings WHERE memory_id NOT IN (SELECT id FROM memory_items)")
            report.append(f"Pruned {res.rowcount} orphans")
        if prune_orphan_queues:
            # Reap entity_extraction_queue rows whose target memory is GONE or
            # SOFT-DELETED. The extraction worker only processes LIVE memories
            # (is_deleted=0), so a row pointing at a deleted memory can NEVER be
            # processed — it lingers forever, inflating the "pending" count. A row
            # is orphaned if its memory_id has no LIVE row in memory_items.
            #
            # Scoped to entity_extraction_queue only: it is the memory-keyed queue.
            # observation_queue / reflector_queue are CONVERSATION-keyed (no
            # memory_id column) — a different shape, out of scope here. Best-effort:
            # a missing table/column on an older schema is skipped, never fatal.
            try:
                res = db.execute(
                    "DELETE FROM entity_extraction_queue WHERE memory_id NOT IN "
                    "(SELECT id FROM memory_items WHERE COALESCE(is_deleted,0)=0)"
                )
                if res.rowcount:
                    report.append(f"Reaped {res.rowcount} orphaned entity-queue row(s)")
            except Exception as e:  # noqa: BLE001 — missing table/column on old schema
                if not _is_missing_schema(e):
                    raise

        # Auto-archive low-importance memories older than 30 days
        archivable = db.execute(
            "SELECT id FROM memory_items WHERE is_deleted = 0 AND importance < 0.05 "
            "AND julianday('now') - julianday(created_at) > 30"
        ).fetchall()
        archived = 0
        for row in archivable:
            if _transfer_to_archive(row["id"], "low_importance", db):
                db.execute("UPDATE memory_items SET is_deleted = 1 WHERE id = ?", (row["id"],))
                archived += 1
        report.append(f"Archived {archived} low-importance items")

        # Enforce agent retention policies
        retention_purged = _enforce_retention_policies(db)
        if retention_purged:
            report.append(f"Retention policies: purged {retention_purged} items")

        # Refresh queue: count memories whose refresh_on has arrived, and emit
        # one push notification per distinct agent with newly-due memories.
        # - Maintenance never mutates refresh flags (that's memory_update's job).
        # - Dedup against existing unacked refresh_due notifications so repeated
        #   maintenance runs don't flood the channel with duplicates.
        try:
            refresh_due = db.execute(
                "SELECT COUNT(*) FROM memory_items "
                "WHERE is_deleted = 0 AND refresh_on IS NOT NULL AND refresh_on <= ?",
                (now,)
            ).fetchone()[0]
            if refresh_due:
                report.append(f"Refresh queue: {refresh_due} memor{'y' if refresh_due == 1 else 'ies'} due for review")

                # Fan-out notifications by agent_id. NULL/empty agent_ids are
                # grouped under a synthetic '(unassigned)' bucket and skipped —
                # notifications require a real agent_id.
                agent_rows = db.execute(
                    "SELECT agent_id, COUNT(*) as n, GROUP_CONCAT(id) as ids "
                    "FROM memory_items "
                    "WHERE is_deleted = 0 AND refresh_on IS NOT NULL AND refresh_on <= ? "
                    "  AND agent_id IS NOT NULL AND agent_id != '' "
                    "GROUP BY agent_id",
                    (now,)
                ).fetchall()

                notified = 0
                for ar in agent_rows:
                    aid = ar["agent_id"]
                    # Dedup: skip if this agent already has an unacked refresh_due notif
                    existing = db.execute(
                        "SELECT 1 FROM notifications "
                        "WHERE agent_id = ? AND kind = 'refresh_due' AND read_at IS NULL LIMIT 1",
                        (aid,)
                    ).fetchone()
                    if existing:
                        continue
                    sample = (ar["ids"] or "").split(",")[:3]
                    payload = json.dumps({"count": ar["n"], "sample_ids": sample})
                    db.execute(
                        "INSERT INTO notifications (agent_id, kind, payload_json, created_at) "
                        "VALUES (?, 'refresh_due', ?, ?)",
                        (aid, payload, now)
                    )
                    notified += 1
                if notified:
                    report.append(f"Refresh queue: notified {notified} agent(s)")
        except Exception as e:
            # refresh_on column may not exist on very old DBs that haven't run v014
            logger.debug(f"refresh queue check skipped: {e}")

        db.execute("ANALYZE")
        report.append("Statistics updated (ANALYZE)")

    # VACUUM is SQLite-specific (PostgreSQL has autovacuum and no client-issued
    # file-compaction). On a PG-primary deployment, skip it explicitly with a
    # clear note rather than sqlite3.connect a stale file and appear to maintain
    # the live store.
    from memory.backends import resolve_backend_name
    if resolve_backend_name() != "sqlite":
        report.append("VACUUM skipped: not applicable on PostgreSQL (autovacuum handles this)")
        return "Maintenance complete:\n" + "\n".join(report)

    # VACUUM must run outside any transaction and needs the *active* DB path,
    # which may differ from the import-time DB_PATH constant when a caller has
    # set active_database() or M3_DATABASE.
    try:
        active_path = memory_core._current_ctx().db_path
        # Skip VACUUM on databases > 500MB to prevent multi-minute hangs (#46)
        db_size = os.path.getsize(active_path)
        if db_size > 500 * 1024 * 1024:
            report.append(f"VACUUM skipped: database too large ({db_size / 1e9:.2f} GB)")
        else:
            vconn = sqlite3.connect(active_path)
            vconn.execute("VACUUM")
            vconn.close()
            report.append("Space reclaimed (VACUUM)")
    except Exception as e:
        report.append(f"VACUUM skipped: {e}")

    return "Maintenance complete:\n" + "\n".join(report)

def gdpr_export_impl(user_id: str) -> str:
    """Export all memories for a data subject (GDPR Article 20 - Right to data portability).

    Backend-aware: routes through the seam (_db()) and dialects placeholders /
    now() so it works on a PostgreSQL-primary store, not just SQLite. Previously
    the ``?`` placeholders + ``strftime('now')`` were SQLite-only, so GDPR export
    silently failed on PG."""
    import json
    if not user_id or not user_id.strip():
        return "Error: user_id is required"
    from memory.backends import dialect
    _d = dialect()
    _p = _d.param()
    with _db() as db:
        rows = db.execute(
            "SELECT id, type, title, content, metadata_json, agent_id, importance, created_at, updated_at "
            f"FROM memory_items WHERE user_id = {_p} AND is_deleted = 0",
            (user_id,)
        ).fetchall()
        items = [dict(r) for r in rows]

        # Log the export request
        import uuid
        req_id = str(uuid.uuid4())
        try:
            db.execute(
                "INSERT INTO gdpr_requests (id, subject_id, request_type, status, items_affected, completed_at) "
                f"VALUES ({_p}, {_p}, 'export', 'completed', {_p}, {_d.now()})",
                (req_id, user_id, len(items))
            )
        except Exception:
            pass  # gdpr_requests table may not exist yet

    return json.dumps({"user_id": user_id, "request_id": req_id, "items_count": len(items), "items": items}, indent=2, default=str)

def gdpr_forget_impl(user_id: str) -> str:
    """Right to be forgotten (GDPR Article 17). Hard-deletes all data for a user_id.

    Backend-aware: seam (_db()) + dialected placeholders / now() so the cascade
    delete runs on PostgreSQL as well as SQLite. The bypass_surface guard catches
    the backend-specific "table absent" error (sqlite3.OperationalError /
    psycopg2 UndefinedTable) rather than only the SQLite one."""
    import uuid
    if not user_id or not user_id.strip():
        return "Error: user_id is required"
    from memory.backends import dialect
    _d = dialect()
    _p = _d.param()

    req_id = str(uuid.uuid4())
    total_deleted = 0

    with _db() as db:
        # Count items before deletion
        count_row = db.execute(
            f"SELECT COUNT(*) as cnt FROM memory_items WHERE user_id = {_p}", (user_id,)
        ).fetchone()
        total_deleted = count_row["cnt"] if count_row else 0

        # Get all memory IDs for cascade deletion
        item_ids = [r["id"] for r in db.execute(
            f"SELECT id FROM memory_items WHERE user_id = {_p}", (user_id,)
        ).fetchall()]

        if item_ids:
            placeholders = _d.placeholder(len(item_ids))
            # Delete embeddings
            db.execute(f"DELETE FROM memory_embeddings WHERE memory_id IN ({placeholders})", item_ids)
            # Delete relationships
            db.execute(f"DELETE FROM memory_relationships WHERE from_id IN ({placeholders}) OR to_id IN ({placeholders})", item_ids + item_ids)
            # Delete history
            db.execute(f"DELETE FROM memory_history WHERE memory_id IN ({placeholders})", item_ids)
            # Delete materialized bypass-surface rows (ADR-0001 §7/§9). The surface FK
            # cascades, but gdpr_forget purges by EXPLICIT enumeration and must not rely
            # on cascade firing — so delete here too. Guarded: table may not exist on a
            # DB migrated below v033. By memory_id (the surfaced pointer) AND user_id.
            try:
                db.execute(f"DELETE FROM bypass_surface WHERE memory_id IN ({placeholders})", item_ids)
                db.execute(f"DELETE FROM bypass_surface WHERE user_id = {_p}", (user_id,))
            except Exception:
                # table absent (pre-v033 SQLite, or not-yet-migrated PG) — nothing
                # to purge. Broadened from sqlite3.OperationalError so a PG
                # UndefinedTable doesn't abort the forget.
                pass
            # Hard-delete the items themselves
            db.execute(f"DELETE FROM memory_items WHERE user_id = {_p}", (user_id,))

        try:
            db.execute(
                "INSERT INTO gdpr_requests (id, subject_id, request_type, status, items_affected, completed_at) "
                f"VALUES ({_p}, {_p}, 'forget', 'completed', {_p}, {_d.now()})",
                (req_id, user_id, total_deleted)
            )
        except Exception:
            pass  # gdpr_requests table may not exist yet

    try:
        from audit_trail import write_audit_entry
        write_audit_entry(
            action="gdpr_forget",
            target_id=user_id,
            metadata={"request_id": req_id, "items_affected": total_deleted}
        )
    except Exception as e:
        logger.warning(f"Failed to write audit trail entry for gdpr_forget: {e}")

    return f"GDPR forget completed: {total_deleted} items hard-deleted for user_id={user_id} (request: {req_id})"

def memory_set_retention_impl(agent_id: str, max_memories: int = 1000, ttl_days: int = 0, auto_archive: int = 1) -> str:
    """Set or update agent retention policy."""
    if not agent_id or not agent_id.strip():
        return "Error: agent_id is required"
    try:
        with _db() as db:
            db.execute(
                "INSERT INTO agent_retention_policies (agent_id, max_memories, ttl_days, auto_archive, updated_at) "
                "VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
                "ON CONFLICT(agent_id) DO UPDATE SET max_memories=excluded.max_memories, ttl_days=excluded.ttl_days, "
                "auto_archive=excluded.auto_archive, updated_at=excluded.updated_at",
                (agent_id, max_memories, ttl_days, auto_archive)
            )
        return f"Retention policy set for agent '{agent_id}': max={max_memories}, ttl={ttl_days}d, auto_archive={bool(auto_archive)}"
    except Exception as e:
        return f"Error setting retention policy: {e}"

def memory_export_impl(agent_filter="", type_filter="", since="", output_format="json"):
    """Export memories as portable JSON. Filter by agent, type, or date."""
    where = ["mi.is_deleted = 0"]
    params = []
    if agent_filter:
        where.append("mi.agent_id = ?")
        params.append(agent_filter)
    if type_filter:
        where.append("mi.type = ?")
        params.append(type_filter)
    if since:
        where.append("mi.created_at >= ?")
        params.append(since)

    where_sql = " AND ".join(where)

    with _db() as db:
        rows = db.execute(f"SELECT * FROM memory_items mi WHERE {where_sql}", params).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            mid = item["id"]
            # Fetch embeddings
            embs = db.execute("SELECT embedding, embed_model, dim, created_at, content_hash FROM memory_embeddings WHERE memory_id = ?", (mid,)).fetchall()
            item["embeddings"] = []
            for e in embs:
                edata = dict(e)
                if edata["embedding"]:
                    edata["embedding"] = base64.b64encode(edata["embedding"]).decode("utf-8")
                item["embeddings"].append(edata)

            # Fetch relationships
            rels = db.execute("SELECT to_id, relationship_type, created_at FROM memory_relationships WHERE from_id = ?", (mid,)).fetchall()
            item["relationships"] = [dict(r) for r in rels]
            items.append(item)

    return json.dumps({
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "item_count": len(items),
        "items": items
    }, indent=2, default=str)

def memory_import_impl(data: str):
    """Import memories from a JSON export. UPSERT semantics — safe to re-run."""
    try:
        payload = json.loads(data)
        items = payload.get("items", [])
    except Exception as e:
        return f"Error parsing import data: {e}"

    i_count, e_count, r_count = 0, 0, 0
    with _db() as db:
        for item in items:
            # 1. UPSERT memory_items
            fields = ["id", "type", "title", "content", "metadata_json", "agent_id", "model_id", "change_agent", "importance", "source", "origin_device", "user_id", "scope", "expires_at", "created_at", "updated_at", "valid_from", "valid_to", "content_hash", "is_deleted"]
            # Filter item to only include known fields
            clean_item = {k: item.get(k) for k in fields if k in item}
            placeholders = ", ".join(["?"] * len(clean_item))
            columns = ", ".join(clean_item.keys())
            update_stmt = ", ".join([f"{k}=excluded.{k}" for k in clean_item.keys() if k != "id"])

            sql = f"INSERT INTO memory_items ({columns}) VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {update_stmt}"
            db.execute(sql, list(clean_item.values()))
            i_count += 1

            # 2. Re-insert embeddings
            mid = clean_item["id"]
            for edata in item.get("embeddings", []):
                eblob = base64.b64decode(edata["embedding"]) if edata.get("embedding") else None
                db.execute(
                    "INSERT OR REPLACE INTO memory_embeddings (id, memory_id, embedding, embed_model, dim, created_at, content_hash) VALUES (?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), mid, eblob, edata.get("embed_model"), edata.get("dim"), edata.get("created_at"), edata.get("content_hash"))
                )
                e_count += 1

            # 3. Re-insert relationships
            for rdata in item.get("relationships", []):
                db.execute(
                    "INSERT OR REPLACE INTO memory_relationships (id, from_id, to_id, relationship_type, created_at) VALUES (?,?,?,?,?)",
                    (str(uuid.uuid4()), mid, rdata.get("to_id"), rdata.get("relationship_type"), rdata.get("created_at"))
                )
                r_count += 1

    return f"Imported {i_count} items, {e_count} embeddings, {r_count} relationships"

async def memory_consolidate_impl(
    type_filter="",
    agent_filter="",
    threshold=20,
    stale_days: int = 0,
    max_importance: float = 1.0,
    protected_types=DEFAULT_PROTECTED_TYPES,
    dry_run: bool = False,
    target_type: str = "summary",
):
    """Consolidate old memories of the same type into summaries using the local LLM.

    Safety gates:
      stale_days: only consider items older than N days (0 = no age filter)
      max_importance: skip items with importance above this floor (default 1.0 = no filter)
      protected_types: types never consolidated (defaults to preference/user_fact/task/plan)
      dry_run: preview what would happen without writes or LLM calls
      target_type: the type of the consolidated output row. Default 'summary'
        (manual/curator rollups). Autonomous belief consolidation (Phase 4)
        passes 'belief' so the two provenance paths stay distinguishable; a
        'belief' row also gets a high first-class confidence.
    """
    now_dt = datetime.now(timezone.utc)
    stale_cutoff = (now_dt - timedelta(days=stale_days)).isoformat() if stale_days > 0 else None

    # 1. Query groups exceeding threshold
    sql = "SELECT type, agent_id, user_id, COUNT(*) as cnt FROM memory_items WHERE is_deleted = 0"
    params = []
    if type_filter:
        sql += " AND type = ?"
        params.append(type_filter)
    if agent_filter:
        sql += " AND agent_id = ?"
        params.append(agent_filter)
    if protected_types:
        placeholders = ",".join(["?"] * len(protected_types))
        sql += f" AND type NOT IN ({placeholders})"
        params.extend(protected_types)
    sql += " GROUP BY type, agent_id, user_id HAVING cnt > ?"
    params.append(threshold)

    with _db() as db:
        groups = db.execute(sql, params).fetchall()

    if not groups:
        return "No memory groups exceed consolidation threshold."

    if dry_run:
        preview = [f"{g['type']}/{g['agent_id']} (user={g['user_id']}): {g['cnt'] - threshold} items would consolidate" for g in groups]
        return "DRY RUN — no changes. Candidates:\n" + "\n".join(preview)

    token = ctx.get_secret("LM_API_TOKEN") or "lm-studio"
    client = _get_embed_client()
    result = await get_best_llm(client, token)
    if not result:
        return "Error: No local LLM available for consolidation."
    base_url, model = result

    results = []
    for g in groups:
        g_type, g_agent, g_user = g["type"], g["agent_id"], g["user_id"]
        n_to_consolidate = g["cnt"] - threshold

        # 2. Fetch oldest N items, honoring stale_days + importance gates
        fetch_sql = (
            "SELECT id, title, content FROM memory_items "
            "WHERE type = ? AND agent_id = ? AND user_id = ? AND is_deleted = 0 "
            "AND COALESCE(importance, 0) <= ?"
        )
        fetch_params = [g_type, g_agent, g_user, max_importance]
        if stale_cutoff:
            fetch_sql += " AND created_at < ?"
            fetch_params.append(stale_cutoff)
        fetch_sql += " ORDER BY created_at ASC LIMIT ?"
        fetch_params.append(n_to_consolidate)

        with _db() as db:
            rows = db.execute(fetch_sql, fetch_params).fetchall()

        if not rows: continue

        # 3. Concatenate content
        items_text = "\n".join(f"- {r['title'] or '(untitled)'}: {r['content']}" for r in rows)

        # 4. Call LLM
        prompt = f"Consolidate these {len(rows)} memory items into a single comprehensive summary. Preserve all facts, decisions, and key details.\n\n{items_text}"
        try:
            resp = await client.post(
                f"{base_url}/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3
                },
                headers={"Authorization": f"Bearer {token}"},
                timeout=memory_core.LLM_TIMEOUT
            )
            resp.raise_for_status()
            data = resp.json()
            if "choices" not in data or not data["choices"]:
                results.append(f"Error consolidating {g_type}/{g_agent}: LLM returned no choices")
                continue
            summary_text = data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            results.append(f"Error consolidating {g_type}/{g_agent}: {type(e).__name__}: {e}")
            continue

        # 5. Store summary
        summary_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        # Embed the summary so it's searchable
        s_vec, s_model = await _embed(summary_text)

        _label = "belief" if target_type == "belief" else "summary"
        _title = (f"Belief from {len(rows)} {g_type} memories ({g_agent})"
                  if target_type == "belief"
                  else f"Consolidated {g_type} memories for {g_agent}")
        with _db() as db:
            db.execute(
                "INSERT INTO memory_items (id, type, title, content, agent_id, user_id, created_at, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (summary_id, target_type, _title, summary_text, g_agent, g_user, now, _content_hash(summary_text))
            )
            # A belief is a high-order, multi-source abstraction — give it a high
            # first-class confidence (knowledge-maintenance Phase 4). Guarded so a
            # pre-035 DB (no confidence column) simply skips it.
            if target_type == "belief":
                try:
                    db.execute("UPDATE memory_items SET confidence = ? WHERE id = ?", (0.85, summary_id))
                except Exception as ce:  # noqa: BLE001 — pre-035 DB lacks confidence
                    if "no such column" not in str(ce).lower() and "no column named" not in str(ce).lower():
                        raise

            if s_vec:
                db.execute(
                    "INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim, created_at, content_hash) VALUES (?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), summary_id, _pack(s_vec), s_model, len(s_vec), now, _content_hash(summary_text))
                )

            # 6. Link to sources and 7. Soft-delete
            for r in rows:
                memory_link_impl(summary_id, r["id"], "consolidates", db=db)
                db.execute("UPDATE memory_items SET is_deleted = 1 WHERE id = ?", (r["id"],))

        results.append(f"Consolidated {len(rows)} {g_type} items into {_label} {summary_id}")

    return "\n".join(results)


# ── Procedural distillation (tasks → reusable `procedure` memories) ──────────
#
# Sibling of memory_consolidate_impl: where consolidation rolls up N episodic
# `observation` memories into a `belief`, distillation rolls up successful task
# runs (a task + its step/result memories) into a reusable `procedure` memory
# via a `distills_from` edge. KEY DIFFERENCE from consolidation: sources are
# PRESERVED (never soft-deleted) — the completed tasks stay queryable; a
# procedure augments history, it doesn't replace it.
#
# BACKEND-AGNOSTIC throughout: all SQL goes through dialect() (param()/
# now_minus_days()), and the write reuses the already-agnostic
# memory_write_impl (which embeds + inserts via the seam) and memory_link_impl.
# No raw SQLite INSERTs, no per-backend branch — runs on SQLite, PostgreSQL, and
# a future MariaDB unchanged.

# Valid procedure sub-kinds. Soft-validated: an unknown kind is allowed (users
# extend), but a model reply outside this set defaults to "skill".
VALID_PROCEDURE_KINDS = frozenset({"skill", "runbook", "how_to", "checklist"})


def _resolve_distill_model() -> str:
    """Return the M3_DISTILL_MODEL selector (local-first default).

    - unset / "slm"  → the local `procedure_local` SLM profile (sovereign default)
    - "llm"          → largest local model via get_best_llm failover
    - any other value → treated as a profile NAME (another local model, or a
                        cloud endpoint via a `backend: anthropic|openai` profile)
    """
    return (os.environ.get("M3_DISTILL_MODEL", "") or "slm").strip()


async def _distill_call_model(prompt: str) -> "str | None":
    """Run the distillation prompt through the resolved model. Returns the raw
    reply text, or None if no model is available / the call fails. Local-first,
    cloud-capable — the resolution is config, never a forced cloud dependency."""
    selector = _resolve_distill_model()

    if selector == "llm":
        # Largest local model (the belief-consolidation path).
        token = ctx.get_secret("LM_API_TOKEN") or "lm-studio"
        client = _get_embed_client()
        result = await get_best_llm(client, token)
        if not result:
            return None
        base_url, model = result
        try:
            resp = await client.post(
                f"{base_url}/chat/completions",
                json={"model": model,
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.2},
                headers={"Authorization": f"Bearer {token}"},
                timeout=memory_core.LLM_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("choices"):
                return None
            return data["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"distill llm call failed: {type(e).__name__}: {e}")
            return None

    # "slm" (default) or a named profile → the shared Profile loader + _call_model
    # (which already dispatches openai|anthropic, so cloud is config-only).
    import httpx
    from slm_intent import _call_model, load_profile

    prof_name = "procedure_local" if selector in ("", "slm") else selector
    prof = load_profile(prof_name)
    if prof is None:
        logger.warning(f"distill profile {prof_name!r} not found; skipping distillation")
        return None
    try:
        async with httpx.AsyncClient(timeout=prof.timeout_s) as client:
            return await _call_model(prof, prompt, client)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"distill slm call failed (profile={prof_name!r}): {type(e).__name__}: {e}")
        return None


def _parse_procedure(text: str) -> "dict | None":
    """Parse the model's JSON procedure. Mirrors run_reflector's parse pattern
    (strip fences + JSON_RE + json.loads). Returns None on malformed / empty
    (no steps) output."""
    from agent_protocol import strip_code_fences
    from run_reflector import JSON_RE

    m = JSON_RE.search(strip_code_fences(text or ""))
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    steps = obj.get("steps")
    if not isinstance(steps, list) or not steps:
        return None
    kind = str(obj.get("procedure_kind", "skill")).strip().lower()
    if kind not in VALID_PROCEDURE_KINDS:
        kind = "skill"
    obj["procedure_kind"] = kind
    return obj


def _render_procedure_markdown(proc: dict) -> str:
    """Human-readable markdown body for a distilled procedure (also what gets
    embedded, so it's searchable)."""
    lines = [f"# {proc.get('name') or 'Procedure'}", ""]
    pre = proc.get("preconditions") or []
    if isinstance(pre, list) and pre:
        lines.append("## Preconditions")
        lines.extend(f"- {p}" for p in pre)
        lines.append("")
    lines.append("## Steps")
    for i, step in enumerate(proc.get("steps") or [], 1):
        lines.append(f"{i}. {step}")
    lines.append("")
    got = proc.get("gotchas") or []
    if isinstance(got, list) and got:
        lines.append("## Gotchas")
        lines.extend(f"- {g}" for g in got)
    return "\n".join(lines).strip()


async def memory_distill_procedures_impl(
    stale_days: int = 3,
    threshold: int = 1,
    max_procedures: int = 20,
    dry_run: bool = False,
):
    """Distill successful (completed) task runs into reusable `procedure` memories.

    Backend-agnostic via dialect() + memory_write_impl + memory_link_impl.

    Args:
      stale_days: only consider tasks completed more than N days ago (lets a
        just-finished task settle before it's distilled). 0 = no age filter.
      threshold: minimum number of candidate completed tasks required before any
        distillation runs (anti-noise). Default 1.
      max_procedures: cap procedures written per run (anti-runaway).
      dry_run: preview candidates without any LLM call or write.

    A source task must be state='completed', not deleted, and carry a
    result_memory_id (so there is a distillable result). Sources are PRESERVED
    (linked via 'distills_from', never soft-deleted).
    """
    from memory.backends import dialect
    from memory_core import memory_write_impl

    _d = dialect()
    p = _d.param()

    # 1. Select completed, non-deleted tasks with a result, aged past stale_days.
    #    Built through the dialect so the same SQL runs on SQLite / PG / MariaDB.
    where = ["state = 'completed'", "deleted_at IS NULL", "result_memory_id IS NOT NULL"]
    params: list = []
    if stale_days > 0:
        where.append(f"completed_at IS NOT NULL AND completed_at < {_d.now_minus_days(p)}")
        params.append(stale_days)
    # NOTE: the tasks table carries no conversation_id/user_id/agent_id — those
    # live on the memories. We derive conversation_id + user_id from the task's
    # RESULT memory below, and use owner_agent/created_by for attribution.
    sql = (
        "SELECT id, title, description, result_memory_id, owner_agent, "
        "created_by FROM tasks WHERE "
        + " AND ".join(where)
        + " ORDER BY completed_at ASC"
    )

    with _db() as db:
        tasks = db.execute(sql, params).fetchall()

    if len(tasks) < max(threshold, 1):
        return (f"No procedural distillation: {len(tasks)} completed task(s) "
                f"with results (threshold {threshold}).")

    tasks = tasks[:max_procedures]

    if dry_run:
        preview = [f"- task {t['id']}: {t['title'] or '(untitled)'}" for t in tasks]
        return (f"DRY RUN — {len(tasks)} task(s) would distill into procedures:\n"
                + "\n".join(preview))

    results: list[str] = []
    for t in tasks:
        # 2. Gather the task's result + step memories. The result memory is the
        #    anchor; sibling step memories share its conversation_id (if any).
        #    conversation_id + user_id are read from the RESULT memory (the tasks
        #    table carries neither), so the procedure lands under the same tenant.
        src_ids: list[str] = []
        step_texts: list[str] = []
        conv = None
        res_user_id = ""
        with _db() as db:
            res = db.execute(
                f"SELECT id, title, content, conversation_id, user_id "
                f"FROM memory_items WHERE id = {p} AND is_deleted = 0",
                (t["result_memory_id"],),
            ).fetchone()
            if res:
                src_ids.append(res["id"])
                step_texts.append(f"- RESULT: {res['title'] or ''}: {res['content']}")
                conv = res["conversation_id"]
                res_user_id = res["user_id"] or ""
            if conv:
                steps = db.execute(
                    f"SELECT id, title, content FROM memory_items "
                    f"WHERE conversation_id = {p} AND is_deleted = 0 "
                    f"AND id <> {p} ORDER BY created_at ASC LIMIT 50",
                    (conv, t["result_memory_id"]),
                ).fetchall()
                for s in steps:
                    src_ids.append(s["id"])
                    step_texts.append(f"- STEP: {s['title'] or ''}: {s['content']}")

        if not src_ids:
            continue

        # 3. Distill via the pluggable local-first/cloud model.
        prompt = (
            "Distill this successful task run into a reusable procedure.\n\n"
            f"TASK: {t['title'] or '(untitled)'}\n"
            f"DESCRIPTION: {t['description'] or ''}\n\n"
            "STEPS AND RESULT:\n" + "\n".join(step_texts)
        )
        reply = await _distill_call_model(prompt)
        if not reply:
            results.append(f"Skipped task {t['id']}: no distillation model output.")
            continue
        proc = _parse_procedure(reply)
        if proc is None:
            results.append(f"Skipped task {t['id']}: model returned no coherent procedure.")
            continue

        # 4. Write the procedure via the backend-agnostic impl (embeds + inserts
        #    through the seam). procedure_kind + steps ride metadata_json.
        body = _render_procedure_markdown(proc)
        meta = json.dumps({
            "procedure_kind": proc["procedure_kind"],
            "steps": proc.get("steps") or [],
            "preconditions": proc.get("preconditions") or [],
            "gotchas": proc.get("gotchas") or [],
            "distilled_from_task": t["id"],
        })
        proc_id = await memory_write_impl(
            type="procedure",
            content=body,
            title=(proc.get("name") or (t["title"] or "Procedure"))[:200],
            metadata=meta,
            agent_id=t["owner_agent"] or t["created_by"] or "",
            importance=0.8,
            source="distillation",
            user_id=res_user_id,
            confidence=0.85,
        )
        # memory_write_impl's success string is "Created: <uuid>[ (…)]". Parse
        # the uuid defensively (same contract supersede relies on); skip the
        # provenance link if the write was rejected / the format changed.
        if not (isinstance(proc_id, str) and proc_id.startswith("Created:")):
            results.append(f"Skipped task {t['id']}: procedure write failed: {proc_id}")
            continue
        new_id = proc_id.split("Created:", 1)[1].strip().split()[0]

        # 5. Provenance: link the procedure to each source (sources PRESERVED).
        with _db() as db:
            for sid in src_ids:
                memory_link_impl(new_id, sid, "distills_from", db=db)

        results.append(
            f"Distilled task {t['id']} → procedure {new_id} "
            f"(kind={proc['procedure_kind']}, {len(src_ids)} source(s))"
        )

    return "\n".join(results) if results else "No procedures distilled."


if __name__ == "__main__":
    # Scheduled-task entrypoint. Previously invoked via
    #   python -c "import memory_maintenance; memory_maintenance.memory_maintenance_impl()"
    # which never reached this block. install_schedules.py / crontab.template
    # now invoke this file as a script so logging + single-instance locking
    # apply. The helper call lives here (not in memory_maintenance_impl) so
    # MCP-server imports of this module are unaffected.
    import argparse
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _task_runtime import add_log_file_arg, setup_task_runtime

    parser = argparse.ArgumentParser(
        description="Daily memory maintenance (decay, prune orphans, retention)."
    )
    add_log_file_arg(parser)
    args = parser.parse_args()

    setup_task_runtime(
        args.log_file,
        lock_name="memory_maintenance",
        logger_name="memory_maintenance",
    )

    import time as _time
    _MAX_RETRIES = 3
    _RETRY_DELAY = 30  # seconds — wait for any concurrent MCP write transaction to finish
    for _attempt in range(1, _MAX_RETRIES + 1):
        try:
            print(memory_maintenance_impl())
            break
        except sqlite3.OperationalError as _e:
            if "database is locked" not in str(_e) or _attempt == _MAX_RETRIES:
                raise
            logger.warning(
                f"database is locked (attempt {_attempt}/{_MAX_RETRIES}), "
                f"retrying in {_RETRY_DELAY}s — MCP server may be mid-write"
            )
            _time.sleep(_RETRY_DELAY)
