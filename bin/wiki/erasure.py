"""GDPR erasure ↔ wiki: restrict syntheses derived from an erased memory.

A synthesis is *derived content that outlives its source* — hard-deleting a member
(Art. 17) removes the source row but NOT the compiled prose that quoted it, and the
synthesis may carry a different user_id than the member (wiki-compile authors under
its own scope), so the erasure's cascade never touches it. This module closes that
gap: before `gdpr_forget` deletes a member, it marks every synthesis that
`consolidates` that member as `restricted`.

`restricted` is a SEPARATE metadata field that OVERRIDES `authority` in the render
gate (wiki.authority.renders_as_body / is_restricted) — so a restricted synthesis
stops rendering from the instant of erasure, bounding exposure at ~zero. This is
NOT auto-revocation (§6: destructive ops are never automatic; the erased member may
have been REDUNDANT, so the prose may need no change): the row, its edges, and its
history are all RETAINED for a later human/LLM derivability review (follow-on
`d4ab42c9`). This module only sets the flag durably and records why.

Design invariants:
  * Runs BEFORE the erasure's relationship-delete — once the `consolidates` edges
    are gone, the synthesis→member link is unrecoverable, so the scan must read
    them first.
  * Never deletes or rewrites prose — sets metadata only. Fully reversible by a
    reviewer (clear the flag) or superseded by a clean recompile.
  * Backend-agnostic: uses the caller's live seam connection + dialect, no direct
    sqlite3 / psycopg import.
"""
from __future__ import annotations

import json


def find_derived_syntheses(db, dialect, erased_member_ids: "list[str]") -> "list[str]":
    """Synthesis ids that `consolidates` at least one erased member. Reads the
    edge table, so MUST be called before the erasure deletes relationships."""
    if not erased_member_ids:
        return []
    ph = dialect.placeholder(len(erased_member_ids))
    rows = db.execute(
        f"SELECT DISTINCT r.from_id FROM memory_relationships r "
        f"JOIN memory_items m ON m.id = r.from_id "
        f"WHERE r.relationship_type = 'consolidates' "
        f"AND m.type = 'synthesis' AND m.valid_to IS NULL "
        f"AND r.to_id IN ({ph})",
        list(erased_member_ids),
    ).fetchall()
    return [r["from_id"] for r in rows]


def restrict_syntheses(db, dialect, synthesis_ids: "list[str]", *,
                       erased_count: int, timestamp: str) -> int:
    """Set `restricted` on each synthesis's metadata, with an erasure record.

    Accumulates: a synthesis restricted by an earlier erasure keeps that history
    and gains the new entry, so a page derived from several erased members carries
    the full trail. Returns the number of syntheses newly touched. Idempotent per
    (synthesis, timestamp): re-running the same erasure does not duplicate a record.
    """
    if not synthesis_ids:
        return 0
    p = dialect.param()
    touched = 0
    for sid in synthesis_ids:
        row = db.execute(
            f"SELECT metadata_json FROM memory_items WHERE id = {p}", (sid,)
        ).fetchone()
        if row is None:
            continue
        meta = dialect.json_column_to_dict(row["metadata_json"])
        # The render gate reads a top-level `restricted` truthy flag OR review.restricted;
        # set the top-level flag (fail-closed) and record the erasure trail under review.
        meta["restricted"] = True
        review = meta.get("review")
        if not isinstance(review, dict):
            review = {}
        record = {"erased_members": erased_count, "erased_at": timestamp,
                  "reason": "gdpr_forget (Art. 17)"}
        history = review.get("erasure_records")
        if not isinstance(history, list):
            history = []
        # Idempotence: don't append an identical (count, timestamp) record twice.
        if not any(r.get("erased_at") == timestamp
                   and r.get("erased_members") == erased_count for r in history):
            history.append(record)
        review["erasure_records"] = history
        review["restricted"] = True  # also under review, so either read path fires
        meta["review"] = review
        db.execute(
            f"UPDATE memory_items SET metadata_json = {p} WHERE id = {p}",
            (json.dumps(meta), sid),
        )
        touched += 1
    return touched


def restrict_derived_on_erasure(db, dialect, erased_member_ids: "list[str]", *,
                                timestamp: str) -> "dict":
    """The full hook: find syntheses derived from the erased members and restrict
    them. Call from gdpr_forget BEFORE the cascade delete. Returns a summary
    {'restricted': N, 'synthesis_ids': [...]} for the erasure's audit record.

    Best-effort at the row level via the caller's transaction — but a failure here
    MUST NOT silently pass: a synthesis left rendering after its source was erased
    is a compliance breach. The caller logs the summary; an empty result on a
    corpus with syntheses derived from the erased id is the alarm condition.
    """
    ids = find_derived_syntheses(db, dialect, erased_member_ids)
    n = restrict_syntheses(db, dialect, ids,
                           erased_count=len(erased_member_ids), timestamp=timestamp)
    return {"restricted": n, "synthesis_ids": ids}
