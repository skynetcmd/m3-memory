import base64
import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

DEFAULT_PROTECTED_TYPES = ("preference", "user_fact", "task", "plan")

import memory_core
from llm_failover import apply_thinking_suppression
from memory_core import (
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

def _row_get(row, key, default=None):
    """Read a column from a seam row whether it exposes mapping access (both
    backends do via the seam) or only positional — tolerating a column the row's
    SELECT * didn't include on an older schema. Never raises."""
    try:
        if hasattr(row, "keys"):
            return row[key] if key in row.keys() else default
        return row[key]
    except Exception:
        return default

def _transfer_to_archive(item_id, reason, db):
    """Copy a memory into the archive tombstone table BEFORE the live row is
    soft-deleted/deleted by the maintenance pass. Routed entirely through the seam
    (`db`), so it writes to memory_archive in the PRIMARY store on both SQLite and
    PostgreSQL — replacing the old separate SQLite sidecar file whose table was
    never created (so every archive write silently no-opped). Idempotent: a
    re-archive of the same id upserts on the id PK.

    Wrapped in savepoint() so a failure here (e.g. pre-042 DB without the table)
    is isolated and does NOT abort the maintenance transaction on PG — the caller
    treats a False return as "not archived" and proceeds to delete anyway, matching
    the historical best-effort contract."""
    from memory.backends import dialect as _dialect
    from memory.db import savepoint as _savepoint
    _d = _dialect()
    _p = _d.param()
    now = datetime.now(timezone.utc).isoformat()
    row = db.execute(f"SELECT * FROM memory_items WHERE id = {_p}", (item_id,)).fetchone()
    if not row:
        return False
    cols = ["id", "type", "title", "content", "agent_id", "user_id", "archive_reason", "archived_at"]
    vals = (
        _row_get(row, "id"), _row_get(row, "type"), _row_get(row, "title"),
        _row_get(row, "content"), _row_get(row, "agent_id"), _row_get(row, "user_id"),
        reason, now,
    )
    # Upsert on the id PK: re-archiving the same memory overwrites its tombstone
    # rather than raising, so the pass is safe to re-run.
    upsert = _d.on_conflict_update("(id)", [c for c in cols if c != "id"])
    sql = (f"INSERT INTO memory_archive ({', '.join(cols)}) "
           f"VALUES ({_d.placeholder(len(cols))}) {upsert}")
    try:
        with _savepoint(db):
            db.execute(sql, vals)
        return True
    except Exception as e:  # noqa: BLE001 — best-effort tombstone; never block the purge
        logger.debug(f"archive write skipped for {item_id}: {e}")
        return False

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
        from memory.backends import dialect as _dialect
        _p = _dialect().param()
        with _db() as db:
            for _, mid_b, _, _, _ in duplicates:
                db.execute(f"UPDATE memory_items SET is_deleted = 1 WHERE id = {_p}", (mid_b,))
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
    from memory.backends import dialect as _dialect
    _d = _dialect()
    _p = _d.param()
    with _db() as db:
        if fb == "useful":
            db.execute(f"UPDATE memory_items SET importance = {_d.least('1.0', 'importance + 0.1')} WHERE id = {_p}", (memory_id,))
        elif fb == "wrong":
            db.execute(f"UPDATE memory_items SET is_deleted = 1 WHERE id = {_p}", (memory_id,))
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
    from memory.db import savepoint as _savepoint
    active_ids: set = set()
    try:
        # (1) Re-aggregate the memories with ledger activity. savepoint keeps the
        # txn usable on PG if memory_corroborations is absent (pre-036 DB).
        with _savepoint(db):
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
        from memory.backends import dialect as _dialect
        _d = _dialect()
        _p = _d.param()
        # Exclude the ledger-active ids via NOT IN. When the set is EMPTY, OMIT the
        # clause entirely — `id NOT IN (NULL)` evaluates to NULL/unknown for every
        # row (SQL three-valued logic), matching NOTHING, which silently disabled
        # the whole decay pass (bug 2026-07-24). An empty set means "exclude
        # nothing", i.e. no clause.
        _not_in = f" AND id NOT IN ({_d.placeholder(len(active_ids))})" if active_ids else ""
        _age7 = _d.age_days_gt("last_accessed_at", "7")
        params = [_conf.NEUTRAL, _conf.DECAY_RATE, *active_ids]
        try:
            with _savepoint(db):
                res = db.execute(
                    f"""
                    UPDATE memory_items
                       SET confidence = {_d.greatest('0.0', _d.least('1.0', f'confidence + ({_p} - confidence) * {_p}'))}
                     WHERE is_deleted = 0
                       AND confidence IS NOT NULL{_not_in}
                       AND (last_accessed_at IS NULL OR {_age7})
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
                   SET confidence = {_d.greatest('0.0', _d.least('1.0', f'confidence + ({_p} - confidence) * {_p}'))}
                 WHERE is_deleted = 0
                   AND confidence IS NOT NULL{_not_in}
                   AND (last_accessed_at IS NULL OR {_age7})
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
    pass tolerates (degrades to a no-op rather than failing maintenance).

    Delegates to the seam: SQLite raises OperationalError('no such column') while
    PostgreSQL raises UndefinedColumn/UndefinedTable with SQLSTATE 42703/42P01.
    dialect().is_undefined_object_error() classifies both from one call so this
    tolerance fires correctly on either backend (not just SQLite's message text)."""
    try:
        from memory.backends import dialect
        return dialect().is_undefined_object_error(exc)
    except Exception:
        # Seam unavailable (raw sqlite fixture) → fall back to the SQLite text match.
        msg = str(exc).lower()
        return ("no such column" in msg or "no such table" in msg
                or "no column named" in msg)


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
    from memory.backends import dialect as _dialect
    _d = _dialect()
    _p = _d.param()
    # window cutoff is now bound as an INT number of days via now_minus_days
    # (portable), not the SQLite-only "-N days" modifier string.
    _since = _d.now_minus_days(_p)  # e.g. "datetime('now','-'||?||' days')" / PG interval
    out: dict = {
        "window_days": window_days,
        "events": {"create": 0, "update": 0, "delete": 0, "supersede": 0},
        "corroboration": {"corroborated": 0, "contradicted": 0},
        "top_contradicted": [],
        "most_revised": [],
    }
    from memory.db import tolerant_schema as _tolerant
    with _db() as db:
        # NOTE: no `db.row_factory = sqlite3.Row` here — that is SQLite-only and the
        # PG compat connection already yields name-addressable rows. The seam gives
        # row["col"] access on both backends.
        #
        # Each block below queries a table that may not exist on an un-migrated DB
        # (memory_history pre-009, memory_corroborations pre-036). tolerant_schema
        # wraps the block in a SAVEPOINT so a missing-schema error is swallowed AND
        # the outer transaction stays usable on PG — the bare try/except-missing
        # idiom left the PG txn aborted for every later block. Real errors still
        # raise (reraise_real=True default).
        #
        # Lifecycle events by type in the window.
        with _tolerant(db):
            for row in db.execute(
                f"SELECT event, COUNT(*) AS n FROM memory_history "
                f"WHERE created_at >= {_since} GROUP BY event",
                (window_days,),
            ):
                if row["event"] in out["events"]:
                    out["events"][row["event"]] = row["n"]

        # Most-revised memories (update + supersede events per memory_id).
        if top_n:
            with _tolerant(db):
                out["most_revised"] = [
                    {"memory_id": r["memory_id"], "revisions": r["n"], "title": r["title"]}
                    for r in db.execute(
                        f"SELECT h.memory_id AS memory_id, COUNT(*) AS n, "
                        f"       COALESCE(m.title, '') AS title "
                        f"FROM memory_history h "
                        f"LEFT JOIN memory_items m ON m.id = h.memory_id "
                        f"WHERE h.created_at >= {_since} "
                        f"  AND h.event IN ('update', 'supersede') "
                        f"GROUP BY h.memory_id ORDER BY n DESC LIMIT {_p}",
                        (window_days, top_n),
                    )
                ]

        # Corroboration vs contradiction in the window (post-036 table).
        with _tolerant(db):
            for row in db.execute(
                f"SELECT CASE WHEN delta > 0 THEN 'corroborated' ELSE 'contradicted' END AS kind, "
                f"       COUNT(*) AS n FROM memory_corroborations "
                f"WHERE created_at >= {_since} GROUP BY kind",
                (window_days,),
            ):
                out["corroboration"][row["kind"]] = row["n"]

        # Most-contradicted memories (post-036 table).
        if top_n:
            with _tolerant(db):
                out["top_contradicted"] = [
                    {"memory_id": r["memory_id"], "contradiction_count": r["n"], "title": r["title"]}
                    for r in db.execute(
                        f"SELECT c.memory_id AS memory_id, COUNT(*) AS n, "
                        f"       COALESCE(m.title, '') AS title "
                        f"FROM memory_corroborations c "
                        f"LEFT JOIN memory_items m ON m.id = c.memory_id "
                        f"WHERE c.created_at >= {_since} AND c.delta < 0 "
                        f"GROUP BY c.memory_id ORDER BY n DESC LIMIT {_p}",
                        (window_days, top_n),
                    )
                ]
    return out


def _enforce_retention_policies(db):
    """Enforce per-agent memory limits and TTLs from agent_retention_policies table."""
    from memory.backends import dialect as _dialect
    from memory.db import savepoint as _savepoint
    _d = _dialect()
    _p = _d.param()
    try:
        policies = db.execute("SELECT * FROM agent_retention_policies").fetchall()
    except Exception:
        return 0  # Table may not exist yet
    purged = 0
    for p in policies:
        agent_id = p["agent_id"]
        # TTL enforcement
        if p["ttl_days"] and p["ttl_days"] > 0:
            # Dual path: prefer the pinned-aware form; fall back without `pinned`
            # on a pre-pinned-column DB. The first attempt is wrapped in a
            # SAVEPOINT so its failure on PG (missing column aborts the txn)
            # doesn't poison the connection before the fallback runs.
            try:
                with _savepoint(db):
                    res = db.execute(
                        f"UPDATE memory_items SET is_deleted = 1 WHERE agent_id = {_p} AND is_deleted = 0 "
                        f"AND {_d.age_days_gt('created_at', _p)} "
                        f"AND COALESCE(pinned, 0) = 0",
                        (agent_id, p["ttl_days"])
                    )
            except Exception as e:  # noqa: BLE001 — pre-pinned-column DB
                if not _is_missing_schema(e):
                    raise
                res = db.execute(
                    f"UPDATE memory_items SET is_deleted = 1 WHERE agent_id = {_p} AND is_deleted = 0 "
                    f"AND {_d.age_days_gt('created_at', _p)}",
                    (agent_id, p["ttl_days"])
                )
            purged += res.rowcount
        # Max count enforcement (keep newest, soft-delete oldest excess)
        if p["max_memories"] and p["max_memories"] > 0:
            try:
                with _savepoint(db):
                    excess = db.execute(
                        f"SELECT id FROM memory_items WHERE agent_id = {_p} AND is_deleted = 0 "
                        f"AND COALESCE(pinned, 0) = 0 "
                        f"ORDER BY created_at DESC {_d.all_rows_after_offset(_p)}",
                        (agent_id, p["max_memories"])
                    ).fetchall()
            except Exception as e:  # noqa: BLE001 — pre-pinned-column DB
                if not _is_missing_schema(e):
                    raise
                excess = db.execute(
                    f"SELECT id FROM memory_items WHERE agent_id = {_p} AND is_deleted = 0 "
                    f"ORDER BY created_at DESC {_d.all_rows_after_offset(_p)}",
                    (agent_id, p["max_memories"])
                ).fetchall()
            for row in excess:
                if p["auto_archive"]:
                    _transfer_to_archive(row["id"], "retention_limit", db)
                db.execute(f"UPDATE memory_items SET is_deleted = 1 WHERE id = {_p}", (row["id"],))
                purged += 1
    return purged

def memory_maintenance_impl(decay=True, purge_expired=True, prune_orphan_embeddings=True,
                            reinforce=True, prune_orphan_queues=True):
    import time

    from m3_sdk import _LAST_USER_INTERACTION
    if time.time() - _LAST_USER_INTERACTION < 15.0:
        logger.info("Active query session detected. Suspending curation pass to yield resources.")
        time.sleep(5.0)

    # The decay/retention/archive/refresh SQL below is now routed through the
    # dialect seam (param(), age_days_gt(), all_rows_after_offset(), group_concat(),
    # greatest/least(), is_undefined_object_error()) and every tolerant block uses a
    # savepoint (savepoint()/tolerant_schema()) so a caught missing-schema error no
    # longer aborts the PG txn. The pass is validated end-to-end on live PG by
    # test_memory_maintenance_pg_live — so the non-sqlite SKIP guard is gone. (The
    # earlier "can't adapt type dict" issue lived in memory_import_impl, a SEPARATE
    # function this pass never calls; it is fixed via the json_bind_value seam
    # primitive and covered by its own round-trip test.) VACUUM below is still
    # skipped on PG — that is genuinely SQLite-only, unlike decay/purge/retention.
    now = datetime.now(timezone.utc).isoformat()
    from memory.backends import dialect as _dialect
    _d = _dialect()
    _p = _d.param()
    _age7 = _d.age_days_gt("created_at", "7")   # literal-day form (no bind)
    from memory.db import savepoint as _savepoint
    report = []
    with _db() as db:
        if decay:
            try:
                # savepoint isolates the pinned-column attempt so its failure on a
                # pre-pinned SQLite DB doesn't abort the txn before the fallback.
                with _savepoint(db):
                    res = db.execute(
                        f"UPDATE memory_items SET importance = {_d.greatest('0.0', 'importance * 0.995')} "
                        f"WHERE is_deleted = 0 AND {_age7} "
                        f"AND COALESCE(pinned, 0) = 0"
                    )
            except Exception as e:  # noqa: BLE001 — pre-pinned-column DB
                if not _is_missing_schema(e):
                    raise
                res = db.execute(
                    f"UPDATE memory_items SET importance = {_d.greatest('0.0', 'importance * 0.995')} "
                    f"WHERE is_deleted = 0 AND {_age7}"
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
                with _savepoint(db):
                    expired = db.execute(
                        f"SELECT id FROM memory_items WHERE expires_at < {_p} AND COALESCE(pinned, 0) = 0",
                        (now,),
                    ).fetchall()
            except Exception as e:  # noqa: BLE001 — pre-pinned-column DB
                if not _is_missing_schema(e):
                    raise
                expired = db.execute(f"SELECT id FROM memory_items WHERE expires_at < {_p}", (now,)).fetchall()
            for row in expired: _transfer_to_archive(row[0], "expired", db)
            try:
                with _savepoint(db):
                    res = db.execute(
                        f"DELETE FROM memory_items WHERE expires_at < {_p} AND COALESCE(pinned, 0) = 0",
                        (now,),
                    )
            except Exception as e:  # noqa: BLE001 — pre-pinned-column DB
                if not _is_missing_schema(e):
                    raise
                res = db.execute(f"DELETE FROM memory_items WHERE expires_at < {_p}", (now,))
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
                with _savepoint(db):
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
            f"SELECT id FROM memory_items WHERE is_deleted = 0 AND importance < 0.05 "
            f"AND {_d.age_days_gt('created_at', '30')}"
        ).fetchall()
        archived = 0
        for row in archivable:
            if _transfer_to_archive(row["id"], "low_importance", db):
                db.execute(f"UPDATE memory_items SET is_deleted = 1 WHERE id = {_p}", (row["id"],))
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
        # savepoint: on PG a failure anywhere in this block (missing refresh_on
        # column on a very old DB, or the notifications insert) would otherwise
        # abort the whole maintenance txn — the broad except below logs it but
        # cannot un-poison the connection. The savepoint re-raises into the except,
        # leaving the txn alive so ANALYZE below still runs.
        try:
            with _savepoint(db):
                refresh_due = db.execute(
                    f"SELECT COUNT(*) FROM memory_items "
                    f"WHERE is_deleted = 0 AND refresh_on IS NOT NULL AND refresh_on <= {_p}",
                    (now,)
                ).fetchone()[0]
                if refresh_due:
                    report.append(f"Refresh queue: {refresh_due} memor{'y' if refresh_due == 1 else 'ies'} due for review")

                    # Fan-out notifications by agent_id. NULL/empty agent_ids are
                    # skipped — notifications require a real agent_id.
                    agent_rows = db.execute(
                        f"SELECT agent_id, COUNT(*) as n, {_d.group_concat('id')} as ids "
                        f"FROM memory_items "
                        f"WHERE is_deleted = 0 AND refresh_on IS NOT NULL AND refresh_on <= {_p} "
                        f"  AND agent_id IS NOT NULL AND agent_id != '' "
                        f"GROUP BY agent_id",
                        (now,)
                    ).fetchall()

                    notified = 0
                    for ar in agent_rows:
                        aid = ar["agent_id"]
                        # Dedup: skip if this agent already has an unacked refresh_due notif
                        existing = db.execute(
                            f"SELECT 1 FROM notifications "
                            f"WHERE agent_id = {_p} AND kind = 'refresh_due' AND read_at IS NULL LIMIT 1",
                            (aid,)
                        ).fetchone()
                        if existing:
                            continue
                        sample = (ar["ids"] or "").split(",")[:3]
                        payload = json.dumps({"count": ar["n"], "sample_ids": sample})
                        db.execute(
                            f"INSERT INTO notifications (agent_id, kind, payload_json, created_at) "
                            f"VALUES ({_p}, 'refresh_due', {_p}, {_p})",
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

    # Storage compaction is delegated to the dialect via compact_storage() — NOT
    # gated on `resolve_backend_name() != "sqlite"` here. That call-site branch was
    # a latent bug: a third backend with a real client-issued compaction (MariaDB
    # OPTIMIZE TABLE, or a future store) would fall into the PG no-op side and never
    # compact. The seam owns the decision: SQLite VACUUMs (size-gated, out of txn),
    # PG returns its autovacuum no-op line, any future backend does its own thing.
    # The active SQLite path (may differ from a constant when a caller switched DBs
    # via active_database()/M3_DATABASE) is passed for the SQLite path; other
    # backends ignore it.
    try:
        active_path = memory_core._current_ctx().db_path
    except Exception:
        active_path = None
    report.append(_d.compact_storage(sqlite_path=active_path))

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

def gdpr_forget_impl(user_id: str, compliance: "dict | str | None" = None) -> str:
    """Right to be forgotten (GDPR Article 17). Hard-deletes all data for a user_id.

    Backend-aware: seam (_db()) + dialected placeholders / now() so the cascade
    delete runs on PostgreSQL as well as SQLite. The bypass_surface guard catches
    the backend-specific "table absent" error (sqlite3.OperationalError /
    psycopg2 UndefinedTable) rather than only the SQLite one.

    `compliance` is an OPTIONAL operator-supplied record of the erasure's
    program-layer context — the fields a DPO/auditor expects to see under the
    accountability principle (Art. 5(2)) but that only the operator knows:
    `legal_basis` (Art. 17(1) ground), `reason`, `verified_by` /
    `verification_method` (who confirmed the requester's identity + how),
    `authorized_by`, `external_ref` (case/ticket #), `retained_note` (what was
    kept under an Art. 17(3) exemption). Stored verbatim in the tamper-evident
    audit trail entry for this erasure. It does NOT make m3 a DSAR platform or a
    compliance program — it just captures more when the operator provides it. See
    docs/COMPLIANCE.md: program-level record-keeping is the operator's
    responsibility. Accepts a dict or a JSON string; unknown keys are kept as-is.
    NOTE: these fields are logged ONLY to the audit trail (the compliance record);
    they are never surfaced elsewhere (wiki, export)."""
    import json as _json
    import uuid
    if not user_id or not user_id.strip():
        return "Error: user_id is required"
    # Normalize compliance to a dict; tolerate a JSON string (CLI/HTTP callers).
    if isinstance(compliance, str):
        try:
            compliance = _json.loads(compliance) if compliance.strip() else None
        except ValueError:
            compliance = {"note": compliance}   # keep raw text rather than lose it
    if compliance is not None and not isinstance(compliance, dict):
        compliance = None
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

        # WIKI erasure hook (Art. 17 derived-content): before the cascade removes
        # the `consolidates` edges, mark every synthesis derived from an erased
        # member `restricted` so it stops rendering immediately. The prose is NOT
        # deleted — the erased member may have been redundant; a later review
        # (follow-on d4ab42c9) decides. Must run BEFORE the relationship delete
        # below, which would otherwise sever the synthesis→member link the scan
        # reads. Best-effort import (wiki is an optional extra), but a scan failure
        # is logged, never silently swallowed — a synthesis left rendering after
        # its source is erased is a compliance breach.
        wiki_erasure_summary = None
        if item_ids:
            try:
                import os as _os
                import sys as _sys
                _bin = _os.path.dirname(_os.path.abspath(__file__))
                if _bin not in _sys.path:
                    _sys.path.insert(0, _bin)
                from wiki.erasure import restrict_derived_on_erasure
                _erased_at = datetime.now(timezone.utc).isoformat()
                wiki_erasure_summary = restrict_derived_on_erasure(
                    db, _d, item_ids, timestamp=_erased_at)
            except Exception as _e:  # pragma: no cover - defensive
                import logging
                logging.getLogger("m3.gdpr").warning(
                    "wiki erasure hook failed for user %s: %r — derived syntheses "
                    "may still render; review manually", user_id, _e)
                wiki_erasure_summary = {"error": repr(_e)}

        # WIKI LEDGER erasure scan (Art. 17, plan G2): the compile ledger's
        # `cluster_members` cache may hold an erased UUID. Scan it, record the
        # erasure on the affected `cluster_run` rows (accumulating id + count +
        # timestamps), and FLUSH those runs' cached membership. The run row
        # survives — so WHY the data is gone outlasts the data. `cluster_members`
        # stores UUIDs only (never content), so the table itself holds no personal
        # data; this closes the cache. A failure is surfaced, never swallowed: an
        # un-flushed cache holding an erased id is a compliance gap. Guarded so an
        # un-migrated DB (tables absent) degrades gracefully.
        wiki_ledger_summary = None
        if item_ids:
            try:
                from wiki.ledger import scan_and_flush_on_erasure
                _led_at = datetime.now(timezone.utc).isoformat()
                wiki_ledger_summary = scan_and_flush_on_erasure(
                    db, _d, item_ids, timestamp=_led_at)
            except Exception as _e:  # pragma: no cover - defensive
                import logging
                logging.getLogger("m3.gdpr").warning(
                    "wiki ledger erasure scan failed for user %s: %r — a cluster "
                    "membership cache may still hold the erased id; review manually",
                    user_id, _e)
                wiki_ledger_summary = {"error": repr(_e)}

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
            from memory.db import savepoint as _savepoint
            try:
                with _savepoint(db):
                    db.execute(f"DELETE FROM bypass_surface WHERE memory_id IN ({placeholders})", item_ids)
                    db.execute(f"DELETE FROM bypass_surface WHERE user_id = {_p}", (user_id,))
            except Exception:
                # table absent (pre-v033 SQLite, or not-yet-migrated PG) — nothing
                # to purge. Broadened from sqlite3.OperationalError so a PG
                # UndefinedTable doesn't abort the forget. savepoint keeps the outer
                # forget transaction usable on PG after the caught miss.
                pass
            # Erase archived tombstones too (migration 042). The archive keeps
            # content + user_id, so an erased user's data would SURVIVE in the
            # tombstone unless purged here — a compliance breach. By memory_id (the
            # archived id, == the original) AND user_id. Guarded for a pre-042 DB.
            try:
                with _savepoint(db):
                    db.execute(f"DELETE FROM memory_archive WHERE id IN ({placeholders})", item_ids)
                    db.execute(f"DELETE FROM memory_archive WHERE user_id = {_p}", (user_id,))
            except Exception:
                pass  # memory_archive absent (pre-042) — nothing archived to erase
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
        audit_meta = {"request_id": req_id, "items_affected": total_deleted}
        if compliance:
            # Operator-supplied program-layer record (legal_basis, reason,
            # verified_by, authorized_by, external_ref, …) — captured verbatim.
            audit_meta["compliance"] = compliance
        if wiki_erasure_summary:
            # Derived-content record: how many wiki syntheses were restricted
            # because they quoted an erased member (Art. 17 derived-content trail).
            audit_meta["wiki_restricted"] = wiki_erasure_summary
        if wiki_ledger_summary:
            # Cache-invalidation record: which cluster_run rows were flagged and had
            # their membership cache flushed because they cached the erased id (G2).
            audit_meta["wiki_ledger_flush"] = wiki_ledger_summary
        write_audit_entry(
            action="gdpr_forget",
            target_id=user_id,
            metadata=audit_meta,
        )
    except Exception as e:
        logger.warning(f"Failed to write audit trail entry for gdpr_forget: {e}")

    return f"GDPR forget completed: {total_deleted} items hard-deleted for user_id={user_id} (request: {req_id})"

def memory_set_retention_impl(agent_id: str, max_memories: int = 1000, ttl_days: int = 0, auto_archive: int = 1) -> str:
    """Set or update agent retention policy."""
    if not agent_id or not agent_id.strip():
        return "Error: agent_id is required"
    try:
        from memory.backends import dialect as _dialect
        _d = _dialect()
        _p = _d.param()
        with _db() as db:
            db.execute(
                f"INSERT INTO agent_retention_policies (agent_id, max_memories, ttl_days, auto_archive, updated_at) "
                f"VALUES ({_p}, {_p}, {_p}, {_p}, {_d.now()}) "
                f"ON CONFLICT(agent_id) DO UPDATE SET max_memories=excluded.max_memories, ttl_days=excluded.ttl_days, "
                f"auto_archive=excluded.auto_archive, updated_at=excluded.updated_at",
                (agent_id, max_memories, ttl_days, auto_archive)
            )
        return f"Retention policy set for agent '{agent_id}': max={max_memories}, ttl={ttl_days}d, auto_archive={bool(auto_archive)}"
    except Exception as e:
        return f"Error setting retention policy: {e}"

def memory_export_impl(agent_filter="", type_filter="", since="", output_format="json"):
    """Export memories as portable JSON. Filter by agent, type, or date."""
    from memory.backends import dialect as _dialect
    _p = _dialect().param()
    where = ["mi.is_deleted = 0"]
    params = []
    if agent_filter:
        where.append(f"mi.agent_id = {_p}")
        params.append(agent_filter)
    if type_filter:
        where.append(f"mi.type = {_p}")
        params.append(type_filter)
    if since:
        where.append(f"mi.created_at >= {_p}")
        params.append(since)

    where_sql = " AND ".join(where)

    with _db() as db:
        rows = db.execute(f"SELECT * FROM memory_items mi WHERE {where_sql}", params).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            mid = item["id"]
            # Fetch embeddings
            embs = db.execute(f"SELECT embedding, embed_model, dim, created_at, content_hash FROM memory_embeddings WHERE memory_id = {_p}", (mid,)).fetchall()
            item["embeddings"] = []
            for e in embs:
                edata = dict(e)
                if edata["embedding"]:
                    edata["embedding"] = base64.b64encode(edata["embedding"]).decode("utf-8")
                item["embeddings"].append(edata)

            # Fetch relationships
            rels = db.execute(f"SELECT to_id, relationship_type, created_at FROM memory_relationships WHERE from_id = {_p}", (mid,)).fetchall()
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

    from memory.backends import dialect as _dialect
    _d = _dialect()
    _p = _d.param()
    i_count, e_count, r_count = 0, 0, 0
    with _db() as db:
        for item in items:
            # 1. UPSERT memory_items
            fields = ["id", "type", "title", "content", "metadata_json", "agent_id", "model_id", "change_agent", "importance", "source", "origin_device", "user_id", "scope", "expires_at", "created_at", "updated_at", "valid_from", "valid_to", "content_hash", "is_deleted"]
            # Filter item to only include known fields
            clean_item = {k: item.get(k) for k in fields if k in item}
            # metadata_json rides back as a dict on a PG export (JSONB whole-column
            # read → dict); binding a bare dict raises "can't adapt type dict". The
            # seam serializes it to a string, which binds on both backends.
            if "metadata_json" in clean_item:
                clean_item["metadata_json"] = _d.json_bind_value(clean_item["metadata_json"])
            placeholders = _d.placeholder(len(clean_item))
            columns = ", ".join(clean_item.keys())
            update_stmt = ", ".join([f"{k}=excluded.{k}" for k in clean_item.keys() if k != "id"])

            sql = f"INSERT INTO memory_items ({columns}) VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {update_stmt}"
            db.execute(sql, list(clean_item.values()))
            i_count += 1

            # 2. Re-insert embeddings. INSERT OR REPLACE is SQLite-only → dialect
            #    upsert on the id PK (overwrite the row's mutable columns).
            mid = clean_item["id"]
            _emb_cols = ["embedding", "embed_model", "dim", "created_at", "content_hash"]
            _emb_upsert = _d.on_conflict_update("(id)", _emb_cols)
            for edata in item.get("embeddings", []):
                eblob = base64.b64decode(edata["embedding"]) if edata.get("embedding") else None
                db.execute(
                    f"{_d.insert_or_ignore()} memory_embeddings (id, memory_id, embedding, embed_model, dim, created_at, content_hash) "
                    f"VALUES ({_d.placeholder(7)}) {_emb_upsert}",
                    (str(uuid.uuid4()), mid, eblob, edata.get("embed_model"), edata.get("dim"), edata.get("created_at"), edata.get("content_hash"))
                )
                e_count += 1

            # 3. Re-insert relationships. Idempotent on the (from,to,type) unique
            #    edge (migration 039) — re-import is a no-op, not a dup.
            _rel_suffix = _d.on_conflict_ignore(
                conflict_target="(from_id, to_id, relationship_type)")
            for rdata in item.get("relationships", []):
                db.execute(
                    f"{_d.insert_or_ignore()} memory_relationships (id, from_id, to_id, relationship_type, created_at) "
                    f"VALUES ({_d.placeholder(5)}) {_rel_suffix}",
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

    from memory.backends import dialect as _dialect
    _d = _dialect()
    _p = _d.param()
    # 1. Query groups exceeding threshold
    sql = "SELECT type, agent_id, user_id, COUNT(*) as cnt FROM memory_items WHERE is_deleted = 0"
    params = []
    if type_filter:
        sql += f" AND type = {_p}"
        params.append(type_filter)
    if agent_filter:
        sql += f" AND agent_id = {_p}"
        params.append(agent_filter)
    if protected_types:
        placeholders = _d.placeholder(len(protected_types))
        sql += f" AND type NOT IN ({placeholders})"
        params.extend(protected_types)
    # HAVING can't reference the SELECT alias `cnt` on PG → repeat COUNT(*).
    sql += f" GROUP BY type, agent_id, user_id HAVING COUNT(*) > {_p}"
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
            f"SELECT id, title, content FROM memory_items "
            f"WHERE type = {_p} AND agent_id = {_p} AND user_id = {_p} AND is_deleted = 0 "
            f"AND COALESCE(importance, 0) <= {_p}"
        )
        fetch_params = [g_type, g_agent, g_user, max_importance]
        if stale_cutoff:
            fetch_sql += f" AND created_at < {_p}"
            fetch_params.append(stale_cutoff)
        fetch_sql += f" ORDER BY created_at ASC LIMIT {_p}"
        fetch_params.append(n_to_consolidate)

        with _db() as db:
            rows = db.execute(fetch_sql, fetch_params).fetchall()

        if not rows: continue

        # 3. Concatenate content
        items_text = "\n".join(f"- {r['title'] or '(untitled)'}: {r['content']}" for r in rows)

        # 4. Call LLM
        prompt = f"Consolidate these {len(rows)} memory items into a single comprehensive summary. Preserve all facts, decisions, and key details.\n\n{items_text}"
        try:
            chat_url = f"{base_url}/chat/completions"
            resp = await client.post(
                chat_url,
                # Reasoning models leave `content` empty; without this the
                # consolidation silently returns nothing.
                json=apply_thinking_suppression({
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3
                }, chat_url),
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
                f"INSERT INTO memory_items (id, type, title, content, agent_id, user_id, created_at, content_hash) "
                f"VALUES ({_d.placeholder(8)})",
                (summary_id, target_type, _title, summary_text, g_agent, g_user, now, _content_hash(summary_text))
            )
            # A belief is a high-order, multi-source abstraction — give it a high
            # first-class confidence (knowledge-maintenance Phase 4). Guarded so a
            # pre-035 DB (no confidence column) simply skips it. savepoint keeps the
            # txn usable on PG: a missing-column error here would otherwise abort the
            # whole consolidation write (embedding + links + soft-deletes below).
            if target_type == "belief":
                from memory.db import savepoint as _savepoint
                try:
                    with _savepoint(db):
                        db.execute(f"UPDATE memory_items SET confidence = {_p} WHERE id = {_p}", (0.85, summary_id))
                except Exception as ce:  # noqa: BLE001 — pre-035 DB lacks confidence
                    if not _is_missing_schema(ce):
                        raise

            if s_vec:
                db.execute(
                    f"INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim, created_at, content_hash) VALUES ({_d.placeholder(7)})",
                    (str(uuid.uuid4()), summary_id, _pack(s_vec), s_model, len(s_vec), now, _content_hash(summary_text))
                )

            # 6. Link to sources and 7. Soft-delete
            for r in rows:
                memory_link_impl(summary_id, r["id"], "consolidates", db=db)
                db.execute(f"UPDATE memory_items SET is_deleted = 1 WHERE id = {_p}", (r["id"],))

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
            chat_url = f"{base_url}/chat/completions"
            resp = await client.post(
                chat_url,
                json=apply_thinking_suppression(
                    {"model": model,
                     "messages": [{"role": "user", "content": prompt}],
                     "temperature": 0.2}, chat_url),
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
