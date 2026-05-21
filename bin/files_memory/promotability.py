"""Heuristic promotion suggestions.

Tracks fact usage (how often files_search hits return a given fact's
leaf) and computes a `promotability_score` so the assistant can surface
"this fact looks promotable" hints. Hard rule: heuristics never
auto-promote. They suggest only — files_promote remains explicit.

The score combines three signals:
  - hit_count    : how often the fact's leaf surfaced in recent searches
  - confidence   : extractor's confidence in the fact (0.0 - 1.0)
  - recency      : exp-decay toward last_hit_at

Score formula (tunable; defaults below):
    score = log(1 + hit_count) * confidence * exp(-age_days / half_life_days)

Surfaces:
  - files_search results carry an inline `promotable` hint when score
    exceeds threshold and the fact is unpromoted.
  - files_promotable() lists top candidates corpus-wide.

Public API:
    record_leaf_hits(conn, leaf_uuids) -> int
    promotability_score(hit_count, confidence, last_hit_at) -> float
    files_promotable(limit, min_score) -> list[dict]
"""
from __future__ import annotations

import logging
import math
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from .db import _db

logger = logging.getLogger("files_memory.promotability")


# Tunables (env-overridable so deployments can dial them without code).
DEFAULT_HALF_LIFE_DAYS: float = float(os.environ.get("M3_FILES_PROMO_HALF_LIFE_DAYS", "30"))
DEFAULT_SUGGEST_THRESHOLD: float = float(os.environ.get("M3_FILES_PROMO_SUGGEST_THRESHOLD", "0.30"))


def record_leaf_hits(
    conn: sqlite3.Connection,
    leaf_uuids: list[str],
) -> int:
    """Increment hit_count for every fact whose leaf is in `leaf_uuids`.

    Returns the number of fact rows touched. No-op when leaf_uuids is empty.
    Used by files_search to register hits transactionally.
    """
    if not leaf_uuids:
        return 0
    now = datetime.now(timezone.utc).isoformat()

    # Find every fact whose leaf is in the hit set.
    CHUNK = 500
    total_touched = 0
    for start in range(0, len(leaf_uuids), CHUNK):
        chunk = leaf_uuids[start:start + CHUNK]
        placeholders = ",".join("?" * len(chunk))
        fact_uuids = [
            r[0] for r in conn.execute(
                f"SELECT uuid FROM facts WHERE leaf IN ({placeholders})", chunk,
            ).fetchall()
        ]
        if not fact_uuids:
            continue

        # Upsert into fact_hit_stats: increment hit_count, set last_hit_at,
        # set first_hit_at if it was NULL.
        for fuuid in fact_uuids:
            conn.execute(
                "INSERT INTO fact_hit_stats(fact_uuid, hit_count, first_hit_at, last_hit_at) "
                "VALUES (?, 1, ?, ?) "
                "ON CONFLICT(fact_uuid) DO UPDATE SET "
                "  hit_count = hit_count + 1, "
                "  last_hit_at = excluded.last_hit_at",
                (fuuid, now, now),
            )
            total_touched += 1
    return total_touched


def promotability_score(
    hit_count: int,
    confidence: float,
    last_hit_at: Optional[str],
    *,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    now: Optional[datetime] = None,
) -> float:
    """Combined heuristic score in roughly [0, 2].

    Confidence weight clamped to [0, 1]; hit_count log-scaled so a
    fact hit 100 times doesn't dominate the ranking by 100×.
    """
    if hit_count <= 0 or confidence <= 0:
        return 0.0

    base = math.log(1 + hit_count) * max(0.0, min(1.0, confidence))

    if not last_hit_at:
        return base

    try:
        last = datetime.fromisoformat(last_hit_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return base
    cur = now or datetime.now(timezone.utc)
    age_days = max(0.0, (cur - last).total_seconds() / 86400.0)
    decay = math.exp(-age_days / max(1.0, half_life_days))
    return base * decay


def files_promotable(
    *,
    limit: int = 20,
    min_score: float = DEFAULT_SUGGEST_THRESHOLD,
    corpus_id: Optional[str] = None,
    include_already_promoted: bool = False,
    db_path: Optional[str] = None,
) -> list[dict]:
    """Top promotion candidates by promotability_score.

    Args:
        limit: max rows returned.
        min_score: floor below which candidates aren't surfaced.
        corpus_id: scope filter.
        include_already_promoted: by default we exclude facts that
            already have a promotion_marker — re-suggesting promoted
            items is noise.
        db_path: target files.db.
    """
    with _db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        sql_parts = [
            "SELECT f.uuid AS fact_uuid, f.statement, f.confidence, "
            "       f.leaf, f.file_node, "
            "       fhs.hit_count, fhs.last_hit_at, "
            "       fn.filename, fn.path_absolute, fn.corpus_id, "
            "       (SELECT 1 FROM promotion_markers pm "
            "        WHERE pm.source_memory = f.uuid LIMIT 1) AS already_promoted "
            "FROM facts f "
            "JOIN fact_hit_stats fhs ON fhs.fact_uuid = f.uuid "
            "JOIN file_nodes fn ON fn.uuid = f.file_node "
            "WHERE fn.superseded_by IS NULL "
            "  AND fhs.hit_count > 0"
        ]
        params: list = []
        if corpus_id:
            sql_parts.append("AND fn.corpus_id = ?")
            params.append(corpus_id)
        if not include_already_promoted:
            sql_parts.append("AND NOT EXISTS (SELECT 1 FROM promotion_markers "
                             "WHERE source_memory = f.uuid)")
        sql_parts.append("ORDER BY fhs.hit_count * f.confidence DESC LIMIT ?")
        params.append(max(limit * 4, 50))  # over-fetch; we'll re-rank with full score

        rows = conn.execute(" ".join(sql_parts), params).fetchall()

    now = datetime.now(timezone.utc)
    scored = []
    for r in rows:
        s = promotability_score(
            hit_count=r["hit_count"],
            confidence=r["confidence"],
            last_hit_at=r["last_hit_at"],
            now=now,
        )
        if s < min_score:
            continue
        scored.append({
            "fact_uuid": r["fact_uuid"],
            "statement": r["statement"],
            "confidence": r["confidence"],
            "hit_count": r["hit_count"],
            "last_hit_at": r["last_hit_at"],
            "filename": r["filename"],
            "path": r["path_absolute"],
            "corpus_id": r["corpus_id"],
            "promotability_score": round(s, 4),
            "already_promoted": bool(r["already_promoted"]),
        })

    scored.sort(key=lambda d: -d["promotability_score"])
    return scored[:limit]
