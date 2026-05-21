"""Hybrid search over leaves in files.db.

FTS5 full-text channel + vector cosine channel + Reciprocal Rank Fusion
(RRF). Default filter is non-superseded (current versions only); pass
include_history=True for time-travel queries.

Phase-1 ranking is intentionally simple: RRF over the top-K from each
channel, then return up to `limit` results. MMR diversity rerank is a
phase-2 upgrade (we can crib it from bin/memory/search.py once the
straightforward path proves it earns the complexity).

Public API:
    files_search(query, **opts) -> list[SearchHit]
    SearchHit — dataclass
"""
from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from embedding_utils import unpack

from .db import _db
from .embed import embed_texts

logger = logging.getLogger("files_memory.search")


@dataclass
class SearchHit:
    """One ranked result from files_search.

    `path` is the ingested path (what we mined). `original_path` is the
    user-facing source path when set via sidecar or --original-path;
    None means the ingested file is its own original. UIs should prefer
    `original_path` for citations and fall back to `path` when None.
    """
    leaf_uuid: str
    file_node_uuid: str
    filename: str
    path: str
    division_type: str
    division_id: str
    division_label: Optional[str]
    text: str
    score: float
    fts_rank: Optional[int] = None
    vec_rank: Optional[int] = None
    original_path: Optional[str] = None
    corpus_id: Optional[str] = None
    metadata: dict = field(default_factory=dict)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _corpus_filter_clause(
    corpus_id: Optional[str],
    corpora: Optional[list[str]],
) -> tuple[str, list]:
    """Build the SQL fragment + params for corpus filtering.

    - corpora (list) takes precedence: emits `fn.corpus_id IN (?,?,...)`.
    - else corpus_id (single string): emits `fn.corpus_id = ?`.
    - else: empty filter.
    Returns ("", []) when no filter applies.
    """
    if corpora:
        clean = [c for c in corpora if c]
        if not clean:
            return ("", [])
        placeholders = ",".join("?" * len(clean))
        return (f" AND fn.corpus_id IN ({placeholders})", list(clean))
    if corpus_id:
        return (" AND fn.corpus_id = ?", [corpus_id])
    return ("", [])


def _fts_query(conn: sqlite3.Connection, query: str, limit: int,
               current_only: bool, corpus_id: Optional[str],
               filetype: Optional[str],
               corpora: Optional[list[str]] = None) -> list[tuple[str, float]]:
    """FTS5 search. Returns [(leaf_uuid, bm25_score)] in rank order.

    Note: bm25() returns LOWER = better; we negate so higher = better
    for consistent scoring with the vector channel.
    """
    # Sanitize query for FTS5: quote any column-name-like tokens. FTS5
    # syntax is permissive but we avoid edge cases by treating the query
    # as a prefix match phrase.
    safe = " ".join(t for t in query.split() if t.replace("-", "").replace("_", "").isalnum())
    if not safe:
        return []

    sql = (
        "SELECT l.uuid, bm25(leaves_fts) AS rank "
        "FROM leaves_fts "
        "JOIN leaves l ON l.rowid = leaves_fts.rowid "
        "JOIN file_nodes fn ON fn.uuid = l.file_node "
        "WHERE leaves_fts MATCH ? "
    )
    params: list = [safe]
    if current_only:
        sql += " AND l.superseded_by IS NULL AND fn.superseded_by IS NULL"
    corpus_clause, corpus_params = _corpus_filter_clause(corpus_id, corpora)
    sql += corpus_clause
    params.extend(corpus_params)
    if filetype:
        sql += " AND fn.filetype = ?"
        params.append(filetype)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        # FTS5 syntax error: fall back to no FTS hits.
        logger.debug("FTS5 query failed (%s); skipping channel", e)
        return []
    return [(r["uuid"], -r["rank"]) for r in rows]


def _vec_query(conn: sqlite3.Connection, query: str, limit: int,
               current_only: bool, corpus_id: Optional[str],
               filetype: Optional[str],
               corpora: Optional[list[str]] = None) -> list[tuple[str, float]]:
    """Vector cosine search. Returns [(leaf_uuid, cosine_score)].

    Embeds the query then scans the text-kind embeddings, computing
    cosine for each. SQLite has no native vector index in this DB — we
    scan. Phase-2 upgrade: sqlite-vec or a precomputed IVF index.
    """
    vecs = embed_texts([query])
    if not vecs or vecs[0][0] is None:
        return []
    qvec, qmodel = vecs[0]

    sql = (
        "SELECT le.leaf_uuid, le.embedding "
        "FROM leaf_embeddings le "
        "JOIN leaves l ON l.uuid = le.leaf_uuid "
        "JOIN file_nodes fn ON fn.uuid = l.file_node "
        "WHERE le.kind = 'text' AND le.embed_model = ?"
    )
    params: list = [qmodel]
    if current_only:
        sql += " AND l.superseded_by IS NULL AND fn.superseded_by IS NULL"
    corpus_clause, corpus_params = _corpus_filter_clause(corpus_id, corpora)
    sql += corpus_clause
    params.extend(corpus_params)
    if filetype:
        sql += " AND fn.filetype = ?"
        params.append(filetype)

    rows = conn.execute(sql, params).fetchall()
    scored: list[tuple[str, float]] = []
    for r in rows:
        try:
            v = unpack(r["embedding"])
        except Exception:
            continue
        scored.append((r["leaf_uuid"], _cosine(qvec, v)))
    scored.sort(key=lambda x: -x[1])
    return scored[:limit]


def _rrf_fuse(
    fts: list[tuple[str, float]],
    vec: list[tuple[str, float]],
    k: int = 60,
) -> dict[str, dict]:
    """Reciprocal Rank Fusion. Returns {uuid: {'score', 'fts_rank', 'vec_rank'}}.

    RRF formula: score(d) = sum over channels of 1 / (k + rank_in_channel).
    k=60 is the canonical default — robust, doesn't need tuning per corpus.
    """
    fused: dict[str, dict] = {}
    for rank, (uid, _s) in enumerate(fts, start=1):
        fused.setdefault(uid, {"score": 0.0, "fts_rank": None, "vec_rank": None})
        fused[uid]["score"] += 1.0 / (k + rank)
        fused[uid]["fts_rank"] = rank
    for rank, (uid, _s) in enumerate(vec, start=1):
        fused.setdefault(uid, {"score": 0.0, "fts_rank": None, "vec_rank": None})
        fused[uid]["score"] += 1.0 / (k + rank)
        fused[uid]["vec_rank"] = rank
    return fused


def files_search(
    query: str,
    *,
    limit: int = 10,
    corpus_id: Optional[str] = None,
    corpora: Optional[list[str]] = None,
    filetype: Optional[str] = None,
    include_history: bool = False,
    channel_limit: int = 50,
    db_path: Optional[str] = None,
) -> list[SearchHit]:
    """Hybrid search over leaves.

    Args:
        query: free text.
        limit: number of hits to return.
        corpus_id: single-corpus scope filter.
        corpora: list of corpus IDs to fan out across. When set, overrides
            corpus_id; results from all listed corpora are fused into one
            ranked list.
        filetype: filter by file_nodes.filetype.
        include_history: if False (default), filter superseded leaves
            and file_nodes out.
        channel_limit: per-channel top-K before fusion. Larger = better
            recall, slower. 50 is a sane default for corpora ≤ 100k leaves.
        db_path: target files.db.
    """
    if not query or not query.strip():
        return []

    current_only = not include_history
    with _db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        fts = _fts_query(conn, query, channel_limit, current_only, corpus_id, filetype, corpora=corpora)
        vec = _vec_query(conn, query, channel_limit, current_only, corpus_id, filetype, corpora=corpora)
        fused = _rrf_fuse(fts, vec)

        if not fused:
            return []

        # Pull leaf + file_node metadata for the top `limit` by score.
        top = sorted(fused.items(), key=lambda kv: -kv[1]["score"])[:limit]
        if not top:
            return []
        uuids = [u for u, _ in top]
        placeholders = ",".join("?" * len(uuids))
        rows = conn.execute(
            f"SELECT l.uuid AS leaf_uuid, l.file_node, l.division_type, "
            f"  l.division_id, l.division_label, l.text, "
            f"  fn.filename, fn.path_absolute AS path, fn.metadata AS fn_metadata, "
            f"  fn.corpus_id AS corpus_id "
            f"FROM leaves l "
            f"JOIN file_nodes fn ON fn.uuid = l.file_node "
            f"WHERE l.uuid IN ({placeholders})",
            uuids,
        ).fetchall()
        row_map = {r["leaf_uuid"]: r for r in rows}

        from .provenance import original_path_for_metadata
        hits: list[SearchHit] = []
        for uid, meta in top:
            row = row_map.get(uid)
            if row is None:
                continue
            hits.append(SearchHit(
                leaf_uuid=row["leaf_uuid"],
                file_node_uuid=row["file_node"],
                filename=row["filename"],
                path=row["path"],
                division_type=row["division_type"],
                division_id=row["division_id"],
                division_label=row["division_label"],
                text=row["text"],
                score=meta["score"],
                fts_rank=meta["fts_rank"],
                vec_rank=meta["vec_rank"],
                original_path=original_path_for_metadata(row["fn_metadata"]),
                corpus_id=row["corpus_id"],
            ))

        # Promotion-suggestion bookkeeping: record a hit for every fact
        # whose leaf surfaced. Skipped in include_history mode (those
        # are explicit history queries, not "what's promotable now").
        if hits and not include_history:
            try:
                from .promotability import record_leaf_hits
                record_leaf_hits(conn, [h.leaf_uuid for h in hits])
            except Exception as e:
                logger.debug("record_leaf_hits failed (non-fatal): %s", e)

        return hits
