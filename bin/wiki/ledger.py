"""Wiki compile ledger — `cluster_run` (provenance) + `cluster_members` (cache).

Plan G1 (~/.m3-private/plans/WIKI_SYNTHESIS_MEMORY_TYPE.md). Two tables with
different natures (do not conflate):

  * ``cluster_run`` is PROVENANCE — a compile decision made at a moment. NOT
    rebuildable, pruned only when nothing references it (a synthesis's
    ``compiler.run_id`` would dangle). Readers resolve ONLY completed runs
    (``completed_at IS NOT NULL``); a crashed compile leaves an invisible
    in-progress row — atomicity, not deletion, solves the partial-table concern.
  * ``cluster_members`` is a CACHE — ``(run_id, cluster_key, memory_id)`` plus the
    cluster's compile-time ``cluster_hash`` and ``head_synthesis_id``. Rebuildable,
    prunable generationally. Stores UUIDs ONLY, never content.

Why frozen membership is a CORRECTNESS fix (not just a cache): a synthesis's own
``consolidates`` edges perturb modularity, so recomputing a past run's clustering
can shift SOURCE membership and re-supersede an unchanged topic (the residual 2/13
idempotence churn). ``frozen_membership`` returns what the membership WAS, so the
cross-run skip-check compares against the frozen set, not a freshly (differently)
recomputed one.

Backend rules (§1): every statement goes through the caller's live seam
connection + ``dialect`` (placeholders / now() / json). No direct sqlite3 /
psycopg import, no ``if backend ==`` branch. Mirrors ``wiki.erasure``.

Fail-safe (§3): the ledger is an OPTIMIZATION + audit surface, never on the
critical path of a correct single compile. Every write is best-effort — a ledger
failure logs and returns a falsy/empty result, it never aborts a compile. The one
exception is the GDPR scan, whose failure the caller must surface (a cache holding
an erased id that is not flushed is a compliance gap), so that function returns a
structured summary and lets the caller decide.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

_log = logging.getLogger("m3.wiki.ledger")


# ── run lifecycle (provenance) ───────────────────────────────────────────────

def next_seq(db, dialect) -> int:
    """The next monotonic run sequence number. MAX(seq)+1, 1 on an empty table."""
    row = db.execute("SELECT MAX(seq) AS m FROM cluster_run").fetchone()
    cur = (row["m"] if row is not None else None) or 0
    return int(cur) + 1


def open_run(db, dialect, *, scope: str = "", user_id: str = "",
             importance_threshold: "float | None" = None,
             entity_comention: bool = False,
             entity_max_degree: "int | None" = None,
             as_of: "str | None" = None) -> str:
    """Insert an IN-PROGRESS run row (``completed_at`` NULL) and return its id.

    The row is invisible to readers until ``complete_run`` stamps ``completed_at``
    — so a compile that dies mid-pass never exposes a partial membership set.
    """
    run_id = str(uuid.uuid4())
    p = dialect.param()
    seq = next_seq(db, dialect)
    db.execute(
        f"INSERT INTO cluster_run "
        f"(id, seq, completed_at, as_of, scope, user_id, importance_threshold, "
        f" entity_comention, entity_max_degree, cluster_count) "
        f"VALUES ({p}, {p}, NULL, {p}, {p}, {p}, {p}, {p}, {p}, 0)",
        (run_id, seq, as_of, scope or "", user_id or "",
         importance_threshold, 1 if entity_comention else 0, entity_max_degree),
    )
    return run_id


def record_members(db, dialect, run_id: str, members: "list[tuple]") -> int:
    """Freeze this run's cluster membership. ``members`` is an iterable of
    ``(cluster_key, memory_id, cluster_hash, head_synthesis_id)`` tuples.

    UUIDs only — never content. Idempotent per (run_id, cluster_key, memory_id)
    via the PK + on-conflict-ignore, so a resumed pass re-recording a cluster does
    not error. Returns the number of member rows recorded.
    """
    members = list(members)
    if not members:
        return 0
    p = dialect.param()
    # "insert, skip on PK conflict" spelled per backend: SQLite puts OR IGNORE in
    # the INSERT verb (prefix) and an empty trailing clause; Postgres uses a plain
    # INSERT + an ON CONFLICT (...) DO NOTHING trailing clause. Compose both from
    # the dialect so this one statement is backend-agnostic (§1).
    verb = dialect.insert_or_ignore()  # "INSERT OR IGNORE INTO" | "INSERT INTO"
    conflict = dialect.on_conflict_ignore(
        conflict_target="(run_id, cluster_key, memory_id)")  # "" on sqlite
    n = 0
    for cluster_key, memory_id, cluster_hash, head_id in members:
        db.execute(
            f"{verb} cluster_members "
            f"(run_id, cluster_key, memory_id, cluster_hash, head_synthesis_id) "
            f"VALUES ({p}, {p}, {p}, {p}, {p}) {conflict}",
            (run_id, cluster_key, memory_id, cluster_hash, head_id),
        )
        n += 1
    return n


def complete_run(db, dialect, run_id: str, *, cluster_count: "int | None" = None,
                 page_hash_digest: "dict | None" = None) -> None:
    """Stamp ``completed_at`` (making the run visible to readers) and optionally
    record the cluster count + a per-cluster page-hash digest.

    ``page_hash_digest`` is ``{cluster_key: page_hash}`` — a hash is not personal
    data and survives erasure, so a later rebuild can say precisely WHICH topics
    differ after a GDPR erasure (G2), not merely that some do.
    """
    p = dialect.param()
    sets = ["completed_at = " + dialect.now()]
    args: list = []
    if cluster_count is not None:
        sets.append(f"cluster_count = {p}")
        args.append(int(cluster_count))
    if page_hash_digest is not None:
        sets.append(f"page_hash_digest = {p}")
        args.append(json.dumps(page_hash_digest, sort_keys=True))
    args.append(run_id)
    db.execute(
        f"UPDATE cluster_run SET {', '.join(sets)} WHERE id = {p}", args)


def prune_superseded_members(db, dialect, keep_run_id: str) -> None:
    """Generational flush: drop the cached membership of every COMPLETED run
    except ``keep_run_id``. Membership is rebuildable, so only the latest run's
    cache is kept warm; provenance rows (cluster_run) are NEVER pruned here.

    A run whose membership referenced an erased id keeps ``membership_flushed=1``
    already (set by the GDPR scan); this is the ordinary generational cleanup.
    """
    p = dialect.param()
    # Only prune members belonging to completed runs, never an in-progress one.
    db.execute(
        f"DELETE FROM cluster_members WHERE run_id IN ("
        f"  SELECT id FROM cluster_run "
        f"  WHERE completed_at IS NOT NULL AND id <> {p})",
        (keep_run_id,),
    )


# ── read side (the skip-check) ───────────────────────────────────────────────

def latest_completed_run(db, dialect) -> "str | None":
    """Id of the most recent COMPLETED run, or None. Readers resolve only these —
    an in-progress (crashed) run is invisible."""
    row = db.execute(
        "SELECT id FROM cluster_run "
        "WHERE completed_at IS NOT NULL "
        "ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    try:
        return row["id"]
    except (KeyError, IndexError, TypeError):
        return row[0]


def frozen_membership(db, dialect, run_id: str) -> "dict[str, dict]":
    """The membership a run froze: ``{cluster_key: {"members": set[str],
    "cluster_hash": str, "head_synthesis_id": str}}``.

    This is what the cross-run skip-check compares a freshly-clustered set
    against, so an unchanged topic is recognized WITHOUT recomputing (and thus
    without the synthesis-perturbs-modularity drift that recompute reintroduces).
    """
    p = dialect.param()
    rows = db.execute(
        f"SELECT cluster_key, memory_id, cluster_hash, head_synthesis_id "
        f"FROM cluster_members WHERE run_id = {p}",
        (run_id,),
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        try:
            ck, mid = r["cluster_key"], r["memory_id"]
            ch, hid = r["cluster_hash"], r["head_synthesis_id"]
        except (KeyError, IndexError, TypeError):
            ck, mid, ch, hid = r[0], r[1], r[2], r[3]
        slot = out.setdefault(ck, {"members": set(), "cluster_hash": ch,
                                   "head_synthesis_id": hid})
        slot["members"].add(mid)
    return out


def hash_is_unchanged(db, dialect, cluster_key: str, cluster_hash: str) -> bool:
    """Cheap skip index (plan line 742): is there a completed-run member row for
    this cluster_key already carrying this exact cluster_hash? If so the cluster is
    unchanged and can be skipped WITHOUT loading its head synthesis row.
    """
    if not cluster_hash:
        return False
    p = dialect.param()
    row = db.execute(
        f"SELECT 1 FROM cluster_members cm "
        f"JOIN cluster_run cr ON cr.id = cm.run_id "
        f"WHERE cr.completed_at IS NOT NULL "
        f"AND cm.cluster_key = {p} AND cm.cluster_hash = {p} LIMIT 1",
        (cluster_key, cluster_hash),
    ).fetchone()
    return row is not None


# ── GDPR (G2): scan for an erased id, record on the run, flush the cache ──────

def scan_and_flush_on_erasure(db, dialect, erased_member_ids: "list[str]", *,
                              timestamp: str) -> "dict":
    """Find every run whose cached membership references an erased UUID, record the
    erasure on those run rows (accumulating id + count + timestamps), and FLUSH the
    affected runs' cached membership.

    The run row SURVIVES the flush — so *why* the data is gone outlasts the data
    (G2). Retaining the erased UUID matches documented m3 posture (``gdpr_requests``
    keeps ``subject_id`` to demonstrate Art. 5(2) compliance).

    Returns a summary the caller folds into the erasure's audit record:
    ``{"runs_flagged": N, "members_flushed": M, "run_ids": [...]}``. A failure
    raises to the caller — an un-flushed cache holding an erased id is a compliance
    gap the caller must surface, not swallow.
    """
    summary: dict[str, Any] = {"runs_flagged": 0, "members_flushed": 0, "run_ids": []}
    if not erased_member_ids:
        return summary
    ph = dialect.placeholder(len(erased_member_ids))
    # Which completed runs cached any of the erased ids?
    rows = db.execute(
        f"SELECT DISTINCT run_id FROM cluster_members "
        f"WHERE memory_id IN ({ph})",
        list(erased_member_ids),
    ).fetchall()
    run_ids: list[str] = []
    for r in rows:
        try:
            run_ids.append(r["run_id"])
        except (KeyError, IndexError, TypeError):
            run_ids.append(r[0])
    if not run_ids:
        return summary

    p = dialect.param()
    for rid in run_ids:
        # Accumulate the erased ids on the run row (union with any prior record).
        cur = db.execute(
            f"SELECT erased_member_ids, erased_count FROM cluster_run WHERE id = {p}",
            (rid,),
        ).fetchone()
        prior_ids: list[str] = []
        if cur is not None:
            try:
                raw = cur["erased_member_ids"]
            except (KeyError, IndexError, TypeError):
                raw = cur[0]
            if raw:
                try:
                    prior_ids = list(json.loads(raw))
                except Exception:
                    prior_ids = []
        merged = sorted(set(prior_ids) | set(erased_member_ids))
        # first_erased_at is set once (COALESCE keeps an existing value);
        # last_erased_at always advances to this erasure.
        db.execute(
            f"UPDATE cluster_run SET "
            f"  erased_member_ids = {p}, "
            f"  erased_count = {p}, "
            f"  first_erased_at = COALESCE(first_erased_at, {p}), "
            f"  last_erased_at = {p}, "
            f"  membership_flushed = 1 "
            f"WHERE id = {p}",
            (json.dumps(merged), len(merged), timestamp, timestamp, rid),
        )
        # Flush the cache for this run (membership is rebuildable; the record isn't).
        db.execute(
            f"DELETE FROM cluster_members WHERE run_id = {p}", (rid,))
        summary["members_flushed"] += 1
    summary["runs_flagged"] = len(run_ids)
    summary["run_ids"] = run_ids
    _log.info("wiki ledger GDPR flush: %d run(s) flagged, membership flushed",
              len(run_ids))
    return summary
