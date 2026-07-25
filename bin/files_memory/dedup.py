"""Semantic near-duplicate detection — phase 3.3.

Scans leaf embeddings and finds pairs whose cosine similarity exceeds
a configurable threshold. Surfaces these as `semantic_dedup_candidates`
rows for human review. Does NOT auto-merge — phase 3.3 is detection
only; merging is a deliberate user action.

Two-phase dedup pipeline (per FILE_INGESTION_PLAN.md §5):
  1. Exact-hash dedup at ingest — already implemented (carry-forward,
     `text_sha256` uniqueness short-circuits identical leaves).
  2. Semantic near-duplicate detection — this module. Run as a
     maintenance pass (files_dedup tool). Pairs surface with cosine
     scores; the user (or a future review UI) picks merge/keep/ignore.

Scope:
  - Scans within a single corpus by default (cross-corpus dedup would
    create different semantics — "shared knowledge" — out of scope).
  - Skips superseded leaves (they're already history).
  - Skips carry-forward pairs (evolved_from already records the link).
  - Filters by embed_model so we don't compare across embedder upgrades.

Algorithm:
  Phase-3 ships an O(n^2) pairwise scan with an MMR-style early-prune
  threshold. Adequate for ≤10k leaves; phase 4 can plug in LSH/HNSW
  via sqlite-vec when corpora grow.

Public API:
    files_dedup(threshold=0.92, limit=200, corpus_id=None) -> dict
    list_dedup_candidates(reviewed=False, limit=100) -> list[dict]
    review_dedup_candidate(uuid, action, note='') -> dict
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
import uuid as _uuid
from typing import Optional

from .config import files_table
from .db import _db

logger = logging.getLogger("files_memory.dedup")


# ──────────────────────────────────────────────────────────────────────────────
# Tunables
# ──────────────────────────────────────────────────────────────────────────────
import os as _os

DEFAULT_THRESHOLD: float = float(_os.environ.get("M3_FILES_DEDUP_THRESHOLD", "0.92"))
DEFAULT_MAX_PAIRS: int = int(_os.environ.get("M3_FILES_DEDUP_MAX_PAIRS", "500"))
DEFAULT_LEAF_LIMIT: int = int(_os.environ.get("M3_FILES_DEDUP_LEAF_LIMIT", "10000"))


def _cosine_packed(a_bytes: bytes, b_bytes: bytes, dim: int) -> float:
    """Cosine over two packed float32 blobs. Pure numpy where available."""
    try:
        import numpy as _np
        a = _np.frombuffer(a_bytes, dtype=_np.float32, count=dim)
        b = _np.frombuffer(b_bytes, dtype=_np.float32, count=dim)
        na = float(_np.linalg.norm(a))
        nb = float(_np.linalg.norm(b))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return float(_np.dot(a, b) / (na * nb))
    except ImportError:
        # Pure-Python fallback (slow but works without numpy).
        from embedding_utils import unpack
        va = unpack(a_bytes)
        vb = unpack(b_bytes)
        if not va or not vb:
            return 0.0
        dot = sum(x * y for x, y in zip(va, vb))
        na = math.sqrt(sum(x * x for x in va))
        nb = math.sqrt(sum(y * y for y in vb))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return dot / (na * nb)


def files_dedup(
    *,
    threshold: float = DEFAULT_THRESHOLD,
    max_pairs: int = DEFAULT_MAX_PAIRS,
    leaf_limit: int = DEFAULT_LEAF_LIMIT,
    corpus_id: Optional[str] = None,
    db_path: Optional[str] = None,
    include_already_detected: bool = False,
) -> dict:
    """Run a pairwise semantic dedup scan over current leaf embeddings.

    Args:
        threshold: cosine floor for surfacing a pair (0.0-1.0).
        max_pairs: stop after this many new candidate pairs are recorded
                   to keep the scan bounded.
        leaf_limit: cap on the number of leaves scanned (newest first).
                    Phase-3 limit; phase 4 will lift via LSH/HNSW.
        corpus_id: scope filter.
        db_path: target files.db.
        include_already_detected: by default, skip pairs that already
                    have an unreviewed candidate row (avoid duplicate
                    surfacing). Set True to force a full re-scan.

    Returns counts: {scanned, pairs_found, pairs_recorded, threshold,
                     duration_ms, skipped_existing}.
    """
    t0 = time.perf_counter()
    out = {
        "scanned": 0,
        "pairs_found": 0,
        "pairs_recorded": 0,
        "skipped_existing": 0,
        "threshold": threshold,
        "max_pairs": max_pairs,
    }

    from memory.backends import dialect as _dialect
    _d = _dialect()
    _p = _d.param()
    with _db(db_path) as conn:
        # NOTE: no `conn.row_factory = sqlite3.Row` — SQLite-only, and the seam
        # already yields name-addressable rows on both backends.

        # Pull current leaves with text embeddings, scoped + capped.
        sql = (
            f"SELECT le.leaf_uuid, le.embedding, le.dim, le.embed_model, "
            f"       l.file_node, l.evolved_from "
            f"FROM {files_table('leaf_embeddings')} le "
            f"JOIN {files_table('leaves')} l ON l.uuid = le.leaf_uuid "
            f"JOIN {files_table('file_nodes')} fn ON fn.uuid = l.file_node "
            f"WHERE le.kind = 'text' "
            f"  AND l.superseded_by IS NULL "
            f"  AND fn.superseded_by IS NULL"
        )
        params: list = []
        if corpus_id:
            sql += f" AND fn.corpus_id = {_p}"
            params.append(corpus_id)
        sql += f" ORDER BY l.created_at DESC LIMIT {_p}"
        params.append(leaf_limit)

        rows = conn.execute(sql, params).fetchall()
        out["scanned"] = len(rows)
        if len(rows) < 2:
            out["duration_ms"] = int((time.perf_counter() - t0) * 1000)
            return out

        # Group by embed_model — never compare across models (different
        # vector spaces, scores are not comparable).
        by_model: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            by_model.setdefault(r["embed_model"], []).append(r)

        # Pre-load existing candidate pairs so we don't double-insert.
        existing_pairs: set[tuple[str, str]] = set()
        if not include_already_detected:
            for r in conn.execute(
                f"SELECT leaf_a, leaf_b FROM {files_table('semantic_dedup_candidates')} "
                f"WHERE reviewed_at IS NULL",
            ).fetchall():
                a, b = r[0], r[1]
                existing_pairs.add((a, b))
                existing_pairs.add((b, a))

        # Pairwise scan per model bucket.
        for model, bucket in by_model.items():
            if len(bucket) < 2:
                continue
            dim = bucket[0]["dim"]
            for i in range(len(bucket)):
                if out["pairs_recorded"] >= max_pairs:
                    break
                a = bucket[i]
                a_emb = a["embedding"]
                for j in range(i + 1, len(bucket)):
                    b = bucket[j]
                    # Skip pairs already linked by evolved_from
                    # (carry-forward / cross-version edge — not a dup).
                    if a["evolved_from"] == b["leaf_uuid"] or b["evolved_from"] == a["leaf_uuid"]:
                        continue
                    # Skip same-file pairs — those are intra-document
                    # repetition, not corpus-level duplicates. (Phase 4
                    # could add an opt-in intra-doc mode.)
                    if a["file_node"] == b["file_node"]:
                        continue
                    pair = (a["leaf_uuid"], b["leaf_uuid"])
                    if pair in existing_pairs:
                        out["skipped_existing"] += 1
                        continue

                    cos = _cosine_packed(a_emb, b["embedding"], dim)
                    if cos < threshold:
                        continue
                    out["pairs_found"] += 1

                    cand_uuid = str(_uuid.uuid4())
                    conn.execute(
                        f"INSERT INTO {files_table('semantic_dedup_candidates')}"
                        f"(uuid, leaf_a, leaf_b, cosine) VALUES ({_d.placeholder(4)})",
                        (cand_uuid, a["leaf_uuid"], b["leaf_uuid"], cos),
                    )
                    out["pairs_recorded"] += 1
                    existing_pairs.add(pair)
                    existing_pairs.add((b["leaf_uuid"], a["leaf_uuid"]))
                    if out["pairs_recorded"] >= max_pairs:
                        break

    out["duration_ms"] = int((time.perf_counter() - t0) * 1000)
    return out


def list_dedup_candidates(
    *,
    reviewed: Optional[bool] = False,
    limit: int = 100,
    min_cosine: Optional[float] = None,
    db_path: Optional[str] = None,
) -> list[dict]:
    """List candidate near-duplicate pairs.

    Args:
        reviewed: None = both; False (default) = only unreviewed;
                  True = only reviewed.
        limit: cap on results.
        min_cosine: filter floor for the candidate's stored cosine.
        db_path: target files.db.

    Returns one dict per candidate with leaf text snippets + file paths
    for context.
    """
    from memory.backends import dialect as _dialect
    _p = _dialect().param()
    sql_parts = [
        f"SELECT c.uuid, c.leaf_a, c.leaf_b, c.cosine, c.reviewed_at, "
        f"       c.review_action, c.detected_at, "
        f"       la.text AS text_a, lb.text AS text_b, "
        f"       fa.path_absolute AS path_a, fb.path_absolute AS path_b, "
        f"       fa.filename AS file_a, fb.filename AS file_b "
        f"FROM {files_table('semantic_dedup_candidates')} c "
        f"JOIN {files_table('leaves')} la ON la.uuid = c.leaf_a "
        f"JOIN {files_table('leaves')} lb ON lb.uuid = c.leaf_b "
        f"JOIN {files_table('file_nodes')} fa ON fa.uuid = la.file_node "
        f"JOIN {files_table('file_nodes')} fb ON fb.uuid = lb.file_node "
        f"WHERE 1 = 1"
    ]
    params: list = []
    if reviewed is False:
        sql_parts.append("AND c.reviewed_at IS NULL")
    elif reviewed is True:
        sql_parts.append("AND c.reviewed_at IS NOT NULL")
    if min_cosine is not None:
        sql_parts.append(f"AND c.cosine >= {_p}")
        params.append(min_cosine)
    sql_parts.append(f"ORDER BY c.cosine DESC LIMIT {_p}")
    params.append(limit)

    with _db(db_path) as conn:
        # seam yields name-addressable rows on both backends (no sqlite3.Row).
        rows = conn.execute(" ".join(sql_parts), params).fetchall()

    return [
        {
            "uuid": r["uuid"],
            "cosine": round(r["cosine"], 4),
            "detected_at": r["detected_at"],
            "reviewed_at": r["reviewed_at"],
            "review_action": r["review_action"],
            "leaf_a": {
                "uuid": r["leaf_a"],
                "file": r["file_a"],
                "path": r["path_a"],
                "text_snippet": (r["text_a"] or "")[:200],
            },
            "leaf_b": {
                "uuid": r["leaf_b"],
                "file": r["file_b"],
                "path": r["path_b"],
                "text_snippet": (r["text_b"] or "")[:200],
            },
        }
        for r in rows
    ]


def review_dedup_candidate(
    candidate_uuid: str,
    action: str,
    note: str = "",
    *,
    db_path: Optional[str] = None,
) -> dict:
    """Mark a candidate pair as reviewed.

    Args:
        candidate_uuid: which pair.
        action: 'kept' | 'merged' | 'ignored' — the user's decision.
                'merged' currently just records intent; actual leaf
                merging is a future phase (would require careful
                rewiring of evolved_from chains).
        note: free-text rationale; saved into metadata JSON.
        db_path: target files.db.
    """
    valid_actions = {"kept", "merged", "ignored"}
    if action not in valid_actions:
        raise ValueError(f"action must be one of {sorted(valid_actions)}; got {action!r}")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    from memory.backends import dialect as _dialect
    _p = _dialect().param()
    with _db(db_path) as conn:
        # The metadata note is merged in PYTHON (read → set → write back) rather
        # than via SQLite's json_set(): json_set is SQLite-only, and `metadata` is
        # a TEXT column on both backends, so decode/re-encode is backend-uniform.
        cur_row = conn.execute(
            f"SELECT metadata FROM {files_table('semantic_dedup_candidates')} WHERE uuid = {_p}",
            (candidate_uuid,),
        ).fetchone()
        if cur_row is None:
            raise ValueError(f"no candidate found for uuid {candidate_uuid!r}")
        _raw = cur_row["metadata"] if hasattr(cur_row, "keys") else cur_row[0]
        try:
            meta = json.loads(_raw) if _raw else {}
            if not isinstance(meta, dict):
                meta = {}
        except (TypeError, ValueError):
            meta = {}
        meta["note"] = note
        conn.execute(
            f"UPDATE {files_table('semantic_dedup_candidates')} "
            f"SET reviewed_at = {_p}, review_action = {_p}, metadata = {_p} "
            f"WHERE uuid = {_p}",
            (now, action, json.dumps(meta), candidate_uuid),
        )
    return {"uuid": candidate_uuid, "action": action, "reviewed_at": now}
