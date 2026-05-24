from __future__ import annotations

import logging
from collections.abc import Collection

from .config import EMBED_DIM, ENTITY_SEED_STOPLIST
from .db import _db
from .embed import _embed
from .fts import _ENTITY_MENTION_RE
from .util import _cosine_batch_packed

# Sentence-initial Title-Case common words that the entity-mention regex
# captures as if they were proper nouns. Skip these as graph-seed candidates
# to prevent spurious entity lookups on conversational query starters.
# Kept here (graph.py) rather than config.py because it's specific to the
# entity-graph BFS seed-extraction and has no other readers.
_QUERY_STARTER_STOPWORDS: frozenset[str] = frozenset({
    "Can", "Could", "Would", "Will", "Should", "Shall",
    "What", "Which", "Where", "When", "Why", "Who", "Whom", "Whose", "How",
    "Did", "Do", "Does", "Is", "Are", "Was", "Were", "Am",
    "I", "My", "Me", "Mine", "We", "Our", "Us", "You", "Your", "Yours",
    "Tell", "Show", "Give", "Help", "Please", "Hi", "Hello", "Hey", "Yes", "No",
})

logger = logging.getLogger("memory.graph")


def _graph_neighbor_ids(seed_ids: list, depth: int) -> set:
    """Return the set of memory_item ids reachable within `depth` hops from any
    item in `seed_ids` via memory_relationships, excluding the seeds themselves.

    Used by memory_search_routed_impl when graph_depth > 0. Returns set[str].

    SQL note: `WHERE from_id IN (...) OR to_id IN (...)` defeats SQLite's
    per-column indexes (idx_mr_from / idx_mr_to in migration 001) and forces a
    table scan. The UNION form below lets the planner use each index
    independently, which scales with `len(frontier)` rather than with table
    size.
    """
    if depth <= 0 or not seed_ids:
        return set()
    depth = min(int(depth), 3)
    seen: set = set(seed_ids)
    frontier: set = set(seed_ids)
    with _db() as db:
        for _ in range(depth):
            if not frontier:
                break
            frontier_list = list(frontier)
            placeholders = ",".join("?" * len(frontier_list))
            rows = db.execute(
                f"SELECT to_id AS nid FROM memory_relationships "
                f"WHERE from_id IN ({placeholders}) "
                f"UNION "
                f"SELECT from_id AS nid FROM memory_relationships "
                f"WHERE to_id IN ({placeholders})",
                frontier_list + frontier_list,
            ).fetchall()
            next_frontier: set = set()
            for r in rows:
                nid = r["nid"]
                if nid not in seen:
                    seen.add(nid)
                    next_frontier.add(nid)
            frontier = next_frontier
    seen.difference_update(seed_ids)
    return seen


def _session_neighbor_ids(seed_ids: list, session_cap: int = 12) -> dict:
    """For each conversation_id present in `seed_ids`' rows, return up to
    session_cap turns from that conversation (excluding seeds themselves).

    Returns dict[memory_id -> row_dict]. Used by memory_search_routed_impl
    when expand_sessions=True. The session_cap is applied per session.
    """
    if not seed_ids:
        return {}
    out: dict = {}
    with _db() as db:
        placeholders = ",".join(["?"] * len(seed_ids))
        seed_rows = db.execute(
            f"SELECT id, conversation_id FROM memory_items WHERE id IN ({placeholders})",
            seed_ids,
        ).fetchall()
        seed_set = set(seed_ids)
        seen_conv: set = set()
        for sr in seed_rows:
            cid = sr["conversation_id"]
            if not cid or cid in seen_conv:
                continue
            seen_conv.add(cid)
            cap = max(1, int(session_cap))
            rows = db.execute(
                "SELECT id, type, title, content, metadata_json, conversation_id, "
                "valid_from, user_id FROM memory_items "
                "WHERE conversation_id = ? AND COALESCE(is_deleted, 0) = 0 "
                "ORDER BY valid_from LIMIT ?",
                (cid, cap),
            ).fetchall()
            for r in rows:
                if r["id"] in seed_set or r["id"] in out:
                    continue
                out[r["id"]] = dict(r)
    return out


async def _entity_graph_neighbor_ids(
    query: str, depth: int, max_neighbors: int, db,
    valid_types: list = None,
    valid_predicates: list = None,
    entity_stoplist: list = None,
    _capture_dict: dict = None,
) -> set:
    """Parse query for entity mentions, traverse entity_relationships up to `depth`
    hops, and return a set of memory_id values linked to the discovered entities.

    Algorithm (Phase 6, regex-only — no SLM):
      1. Extract candidate mentions from query via _ENTITY_MENTION_RE.
      2. Lookup each candidate in `entities` table (exact then LIKE, cap 5/candidate).
         If valid_types is given, restrict entity lookup to those entity_type values.
         Stoplisted canonical_names (case-insensitive) are excluded.
      3. BFS over `entity_relationships` up to min(depth, 3) hops,
         capped at min(max_neighbors, 100) total entity nodes.
         If valid_predicates is given, only traverse edges with matching predicate.
         Stoplisted entities are dropped from the frontier.
      4. Fetch memory_ids from `memory_item_entities` for all discovered entities.

    valid_types: list of allowed entity_type strings; None = use VALID_ENTITY_TYPES defaults.
    valid_predicates: list of allowed predicate strings; None = use VALID_ENTITY_PREDICATES defaults.
    entity_stoplist: list of canonical_name strings (case-insensitive) to never seed
      from or expand to. None = use M3_ENTITY_SEED_STOPLIST env default.
      Pass [] to explicitly disable filtering.

    Returns set[str] of memory_ids. Returns empty set on any early-exit condition.
    """
    if not query or not query.strip():
        return set()

    # Clamp to safe limits (mirrors memory_graph_impl clamp for depth)
    depth = min(int(depth), 3)
    max_neighbors = min(int(max_neighbors), 100)

    # Step 1 — extract candidate mention strings.
    # Filter out sentence-initial Title-Case common words that the regex
    # picks up as if they were proper nouns ("Can you recommend...",
    # "What should I serve...", "How do I..."). These leak into entity
    # lookups and pull unrelated turns into the top-k via spurious overlap;
    # see LME-S KG-overlap report 2026-05-23 for the -13.3pp single-session-
    # preference @k=5 regression this caused.
    candidates: list[str] = []
    seen_cands: set[str] = set()
    for m in _ENTITY_MENTION_RE.finditer(query):
        text = m.group(0).strip("\"'")
        if text and text not in seen_cands and text not in _QUERY_STARTER_STOPWORDS:
            seen_cands.add(text)
            candidates.append(text)

    if not candidates:
        return set()

    # Step 2 — entity lookup: collect matched entity_ids
    try:
        # Quick check: is the entities table populated at all?
        count_row = db.execute("SELECT COUNT(*) AS cnt FROM entities").fetchone()
        if count_row["cnt"] == 0:
            return set()
    except Exception:  # noqa: BLE001
        return set()

    # Resolve entity stoplist: caller list (incl. explicit []) > env default.
    # Frozenset (env default) or tuple (caller-derived) — only iterated and
    # membership-tested downstream, so the common Collection type is enough.
    _stoplist_lower: "Collection[str]" = ()
    if entity_stoplist is None:
        _stoplist_lower = ENTITY_SEED_STOPLIST
    else:
        _stoplist_lower = tuple(s.strip().lower() for s in entity_stoplist if s and s.strip())
    _stop_clause = ""
    _stop_params: list = []
    if _stoplist_lower:
        _stop_ph = ",".join(["?"] * len(_stoplist_lower))
        _stop_clause = f" AND LOWER(canonical_name) NOT IN ({_stop_ph})"
        _stop_params = list(_stoplist_lower)

    # Pre-compute stoplisted entity IDs so we can drop them from the BFS
    # frontier even if a non-stoplisted seed has them as a 1-hop neighbor.
    _stoplisted_eids: set[str] = set()
    if _stoplist_lower:
        try:
            sl_rows = db.execute(
                f"SELECT id FROM entities WHERE LOWER(canonical_name) IN ({','.join(['?']*len(_stoplist_lower))})",
                list(_stoplist_lower),
            ).fetchall()
            _stoplisted_eids = {r["id"] for r in sl_rows}
        except Exception:  # noqa: BLE001
            _stoplisted_eids = set()

    # Build optional entity_type filter clause (caller-provided list overrides core defaults)
    _type_clause = ""
    _type_params: list = []
    if valid_types:
        _type_ph = ",".join(["?"] * len(valid_types))
        _type_clause = f" AND entity_type IN ({_type_ph})"
        _type_params = list(valid_types)

    # Pre-compute stoplisted-candidate count for telemetry. A candidate is
    # "dropped at seed" if its lowercased form matches the stoplist exactly —
    # that's the case the LIKE-tier filter wouldn't redeem either, so it's a
    # true seed-rejection rather than a "no exact match, fell through to LIKE"
    # event. Cheap O(N) set check; no extra SQL.
    seeds_dropped = (
        sum(1 for c in candidates if c.lower() in _stoplist_lower)
        if _stoplist_lower else 0
    )

    matched_entity_ids: set[str] = set()
    # Tier 1 (batched): one query for all candidate exact-matches.
    # idx_entities_canonical_type covers the equality predicate. We learn which
    # candidates resolved so we know which need the Tier-2 LIKE fallback.
    resolved_cands: set[str] = set()
    try:
        cand_ph = ",".join("?" * len(candidates))
        tier1_rows = db.execute(
            f"SELECT id, canonical_name FROM entities "
            f"WHERE canonical_name IN ({cand_ph}){_type_clause}{_stop_clause}",
            list(candidates) + _type_params + _stop_params,
        ).fetchall()
        for r in tier1_rows:
            matched_entity_ids.add(r["id"])
            resolved_cands.add(r["canonical_name"])
    except Exception:  # noqa: BLE001
        pass

    # Tier 2 (per-candidate LIKE): only run for candidates that didn't resolve
    # in Tier 1, capped at 5 hits each — matches the legacy LIMIT 5.
    for candidate in candidates:
        if candidate in resolved_cands:
            continue
        try:
            rows = db.execute(
                f"SELECT id FROM entities WHERE LOWER(canonical_name) LIKE LOWER(?){_type_clause}{_stop_clause} LIMIT 5",
                [f"%{candidate}%"] + _type_params + _stop_params,
            ).fetchall()
            for r in rows:
                matched_entity_ids.add(r["id"])
        except Exception:  # noqa: BLE001
            continue

    if _capture_dict is not None:
        _capture_dict["entity_seeds_dropped"] = seeds_dropped
        _capture_dict["entity_stoplist_size"] = len(_stoplist_lower)

    if not matched_entity_ids:
        return set()

    # Build optional predicate filter clause for BFS (caller-provided list overrides core defaults)
    _pred_clause = ""
    _pred_params: list = []
    if valid_predicates:
        _pred_ph = ",".join(["?"] * len(valid_predicates))
        _pred_clause = f" AND predicate IN ({_pred_ph})"
        _pred_params = list(valid_predicates)

    # Step 3 — BFS over entity_relationships up to `depth` hops.
    # SQL note: same OR-of-IN antipattern fix as `_graph_neighbor_ids`. The
    # idx_er_from / idx_er_to indexes are (from_entity, predicate) and
    # (to_entity, predicate); the UNION form lets each index serve its half.
    seen_entities: set[str] = set(matched_entity_ids)
    frontier: set[str] = set(matched_entity_ids)
    frontier_dropped = 0
    for _ in range(depth):
        if not frontier or len(seen_entities) >= max_neighbors:
            break
        frontier_list = list(frontier)
        placeholders = ",".join("?" * len(frontier_list))
        try:
            rel_rows = db.execute(
                f"SELECT to_entity AS neighbor FROM entity_relationships "
                f"WHERE from_entity IN ({placeholders}){_pred_clause} "
                f"UNION "
                f"SELECT from_entity AS neighbor FROM entity_relationships "
                f"WHERE to_entity IN ({placeholders}){_pred_clause}",
                frontier_list + _pred_params + frontier_list + _pred_params,
            ).fetchall()
        except Exception:  # noqa: BLE001
            break
        next_frontier: set[str] = set()
        for r in rel_rows:
            eid = r["neighbor"]
            if eid in _stoplisted_eids:
                if eid not in seen_entities:
                    frontier_dropped += 1
                continue
            if eid not in seen_entities:
                seen_entities.add(eid)
                next_frontier.add(eid)
                if len(seen_entities) >= max_neighbors:
                    break
        frontier = next_frontier

    if _capture_dict is not None:
        _capture_dict["entity_frontier_dropped"] = frontier_dropped

    # Step 4 — memory_item lookup
    if not seen_entities:
        return set()
    try:
        placeholders = ",".join(["?"] * len(seen_entities))
        mie_rows = db.execute(
            f"SELECT DISTINCT memory_id FROM memory_item_entities "
            f"WHERE entity_id IN ({placeholders})",
            list(seen_entities),
        ).fetchall()
        return {r["memory_id"] for r in mie_rows}
    except Exception:  # noqa: BLE001
        return set()


async def _score_extra_rows(query: str, rows_by_id: dict, base_score: float = 0.0) -> list:
    """Score additional rows (from graph or session expansion) against the query.

    Reuses the standard embedding path. Each returned tuple is (score, item_dict)
    matching memory_search_scored_impl's shape. Items are scored by cosine vs
    query embedding. If embedding lookup fails for a row, it gets `base_score`.
    """
    if not rows_by_id:
        return []
    out: list = []
    qvec, _ = await _embed(query)
    if qvec is None:
        # No embedding model available — fall back to base_score for all
        for rid, item in rows_by_id.items():
            out.append((base_score, item))
        return out
    with _db() as db:
        ids = list(rows_by_id.keys())
        placeholders = ",".join("?" * len(ids))
        emb_rows = db.execute(
            f"SELECT memory_id, embedding FROM memory_embeddings "
            f"WHERE memory_id IN ({placeholders})",
            ids,
        ).fetchall()
    # Batched packed-cosine: aligned by id so scoring is one parallel pass.
    fetched_ids: list = [er["memory_id"] for er in emb_rows]
    fetched_blobs: list = [er["embedding"] for er in emb_rows]
    fetched_scores = _cosine_batch_packed(qvec, fetched_blobs, EMBED_DIM) if fetched_blobs else []
    score_by_id: dict = dict(zip(fetched_ids, fetched_scores))
    for rid, item in rows_by_id.items():
        s = score_by_id.get(rid)
        if s is None:
            out.append((base_score, item))
        else:
            out.append((float(s), item))
    return out


def memory_graph_impl(memory_id: str, depth: int = 1) -> str:
    """Returns the local graph neighborhood of a memory item up to N hops."""
    depth = min(max(int(depth), 1), 3)  # Clamp to 1-3
    with _db() as db:
        # Verify item exists
        root = db.execute("SELECT id, title, type FROM memory_items WHERE id = ?", (memory_id,)).fetchone()
        if not root:
            return f"Error: memory {memory_id} not found"

        # Recursive CTE to traverse relationships up to `depth` hops
        rows = db.execute("""
            WITH RECURSIVE graph(node_id, hop) AS (
                SELECT ?, 0
                UNION ALL
                SELECT CASE WHEN mr.from_id = g.node_id THEN mr.to_id ELSE mr.from_id END, g.hop + 1
                FROM memory_relationships mr
                JOIN graph g ON (mr.from_id = g.node_id OR mr.to_id = g.node_id)
                WHERE g.hop < ?
            )
            SELECT DISTINCT mi.id, mi.title, mi.type, g.hop
            FROM graph g
            JOIN memory_items mi ON g.node_id = mi.id
            WHERE mi.is_deleted = 0
            ORDER BY g.hop, mi.type
        """, (memory_id, depth)).fetchall()

        # Also get the edges
        node_ids = [r["id"] for r in rows]
        if not node_ids:
            return f"No graph neighborhood for {memory_id}"
        placeholders = ",".join(["?"] * len(node_ids))
        edges = db.execute(
            f"SELECT from_id, to_id, relationship_type FROM memory_relationships "
            f"WHERE from_id IN ({placeholders}) OR to_id IN ({placeholders})",
            node_ids + node_ids
        ).fetchall()

    lines = [f"Graph for {root['title'] or root['id']} (type={root['type']}, depth={depth}):"]
    lines.append(f"\nNodes ({len(rows)}):")
    for r in rows:
        hop_label = "ROOT" if r["id"] == memory_id else f"hop {r['hop']}"
        lines.append(f"  [{r['id'][:8]}] {r['title'] or '(untitled)'} (type={r['type']}, {hop_label})")

    # Filter edges to only those connecting our nodes
    node_set = set(node_ids)
    relevant_edges = [e for e in edges if e["from_id"] in node_set and e["to_id"] in node_set]
    if relevant_edges:
        lines.append(f"\nEdges ({len(relevant_edges)}):")
        for e in relevant_edges:
            lines.append(f"  {e['from_id'][:8]} --[{e['relationship_type']}]--> {e['to_id'][:8]}")

    return "\n".join(lines)
