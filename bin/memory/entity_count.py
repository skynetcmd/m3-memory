"""Entity-count first-class queries — aggregation without LLM/embedding.

Three impls expose direct-SQL aggregation over the entity-graph tables:

  * `count_entities_impl`     — distinct entity count, optionally type-filtered
  * `count_mentions_impl`     — per-entity mention frequency, sorted desc
  * `list_mentions_impl`      — memory_ids that mention a specific entity

All three are scoped to a single `conversation_id` (mandatory).
Cross-conversation scans are not supported — every query is bottom-anchored
to a `WHERE mi.conversation_id = <bind>` clause at the SQL level (the bind
placeholder is backend-dialected). This is the privacy /
multi-tenancy invariant the rest of m3-memory relies on.

## Why these exist

Embedding retrieval (`memory_search_scored_impl`) answers "what's similar
to this query." It cannot answer "how many distinct X are mentioned in this
conversation" without retrieving top-k and hoping it covers all instances.
For counting / inventory / audit / aggregation queries, top-k is the wrong
abstraction — these impls walk the index directly.

The LongMemEval-S multi-session benchmark exposed this gap concretely:
"how many fitness classes do I attend in a typical week" needs to enumerate
every fitness-class entity in the conversation, not the top-10 most similar
turns. Embedding retrieval at top-k=50 still hit only ~10% gold-turn recall
on aggregation questions because the gold turns are spread thinly across
many sessions. Direct index access bypasses that limitation entirely.

## Design contract (all three impls)

- Returns a plain dict / list — never a message string. (MCP perf lesson #1)
- Empty result is a zero count or an empty list, never None.
- Result-size capped at `limit` (default 1000) — defends against pathological
  inputs and keeps response shapes predictable.
- All SQL parameterized — no string interpolation of user inputs.
- Result-size and pattern-length capped at module boundary; oversized inputs
  raise ValueError (caller treats as a 400-class error).
- Read-only by construction — no write paths, no schema mutations.
- Honors active database via `_db()`; reuses existing connection pool.
- Indexed paths only — every query has been EXPLAIN-checked to use:
    * `ix_memory_items_conv` on `memory_items(conversation_id)`
    * `idx_mie_entity`         on `memory_item_entities(entity_id)`
    * PK on `entities(id)`

## Module-state ownership

None. This module is stateless. All cycles into memory_core are avoided
by virtue of the read-only API surface — no need for the
`_resolve_mc_callbacks` shim or lazy-imports.

## See also

- `memory.entity` — entity CRUD, vocab, extraction queue (where the
  entities + memory_item_entities tables are populated)
- `memory.search` — embedding-based retrieval impls
- `docs/MCP_TOOL_PERF_LESSONS.md` — structured returns over strings
- `docs/MEMORY_CORE_MODULARIZATION_LESSONS.md` — extraction discipline
"""
from __future__ import annotations

from typing import Any, Optional

from .backends import dialect
from .db import _db

# ── Module limits ────────────────────────────────────────────────────────────
# Caps applied at function entry. Exposed as module constants so tests and
# callers can introspect them; not re-imported via shim because they are not
# part of the public m3-memory API surface (internal contract).

MAX_LIMIT = 10_000          # absolute ceiling on result-set size
DEFAULT_LIMIT = 1_000        # used when caller passes 0 or omits
MAX_PATTERN_LEN = 256        # reject overlong LIKE patterns


# ── Validation helpers ───────────────────────────────────────────────────────


def _require_conversation_id(conversation_id: str) -> str:
    """Conversation_id is mandatory. Empty / None / whitespace-only is rejected.

    Per the m3-memory multi-tenancy invariant: every query is per-conversation.
    A missing conversation_id would trigger a global scan; we refuse rather
    than risk a cross-tenant leak by accident.
    """
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise ValueError(
            "conversation_id is required and must be a non-empty string"
        )
    return conversation_id


def _validate_limit(limit: Optional[int]) -> int:
    """Coerce limit into [1, MAX_LIMIT]. None / 0 / negative → DEFAULT_LIMIT."""
    if limit is None or limit <= 0:
        return DEFAULT_LIMIT
    return min(int(limit), MAX_LIMIT)


def _validate_pattern(pattern: Optional[str]) -> Optional[str]:
    """Return the trimmed pattern, or None if empty. Reject overlong patterns."""
    if pattern is None:
        return None
    pattern = pattern.strip()
    if not pattern:
        return None
    if len(pattern) > MAX_PATTERN_LEN:
        raise ValueError(
            f"pattern length {len(pattern)} exceeds MAX_PATTERN_LEN={MAX_PATTERN_LEN}"
        )
    return pattern


# ── Public impls ─────────────────────────────────────────────────────────────


def count_entities_impl(
    conversation_id: str,
    entity_type: str = "",
    pattern: str = "",
) -> dict[str, Any]:
    """Count distinct entities mentioned in `conversation_id`.

    Args:
        conversation_id: Required, non-empty. Per-conversation scope is enforced.
        entity_type: Optional. If provided, restrict to this type
            (e.g. "product", "place", "person"). Empty string = all types.
        pattern: Optional. If provided, case-insensitive LIKE %pattern% on
            entities.canonical_name. Useful for "how many distinct python
            libraries did I mention" → pattern="python".

    Returns:
        {
            "conversation_id": str,
            "entity_type": str (echoes input or "*" for all),
            "pattern": str (echoes input or "" for unfiltered),
            "count": int,
        }

    Raises:
        ValueError: if conversation_id is missing / empty, or if pattern
        exceeds MAX_PATTERN_LEN.
    """
    cid = _require_conversation_id(conversation_id)
    pat = _validate_pattern(pattern)
    etype = (entity_type or "").strip()
    _d = dialect()
    p = _d.param()

    where_parts = [f"mi.conversation_id = {p}"]
    params: list[Any] = [cid]

    if etype:
        where_parts.append(f"e.entity_type = {p}")
        params.append(etype)
    if pat:
        where_parts.append(f"e.canonical_name LIKE {p}")
        params.append(f"%{pat}%")

    sql = (
        "SELECT COUNT(DISTINCT e.id) "
        "FROM memory_item_entities mie "
        "JOIN memory_items mi ON mie.memory_id = mi.id "
        "JOIN entities       e  ON mie.entity_id  = e.id "
        "WHERE " + " AND ".join(where_parts)
    )

    with _db() as db:
        row = db.execute(sql, params).fetchone()
        count = int(row[0]) if row and row[0] is not None else 0

    return {
        "conversation_id": cid,
        "entity_type": etype or "*",
        "pattern": pat or "",
        "count": count,
    }


def count_mentions_impl(
    conversation_id: str,
    entity_type: str = "",
    pattern: str = "",
    limit: int = 0,
) -> dict[str, Any]:
    """Return per-entity mention counts in `conversation_id`, sorted desc.

    Use this when you need to know WHICH entities, not just HOW MANY —
    e.g. "what are the most-mentioned places in my Tokyo trip conversation."

    Args:
        conversation_id: Required, non-empty.
        entity_type: Optional type filter.
        pattern: Optional case-insensitive substring filter on canonical_name.
        limit: Max rows to return. Default `DEFAULT_LIMIT`. Capped at MAX_LIMIT.

    Returns:
        {
            "conversation_id": str,
            "entity_type": str ("*" for all),
            "pattern": str,
            "limit": int (the resolved limit, post-clamp),
            "total": int (total distinct entities matching — may exceed
                          len(rows) if `limit` truncated),
            "rows": [{
                "entity_id": str,
                "canonical_name": str,
                "entity_type": str,
                "mention_count": int,
            }, ...],   # sorted by mention_count DESC, then canonical_name ASC
        }
    """
    cid = _require_conversation_id(conversation_id)
    pat = _validate_pattern(pattern)
    etype = (entity_type or "").strip()
    lim = _validate_limit(limit)
    _d = dialect()
    p = _d.param()

    where_parts = [f"mi.conversation_id = {p}"]
    params: list[Any] = [cid]

    if etype:
        where_parts.append(f"e.entity_type = {p}")
        params.append(etype)
    if pat:
        where_parts.append(f"e.canonical_name LIKE {p}")
        params.append(f"%{pat}%")

    where_clause = " AND ".join(where_parts)

    rows_sql = (
        "SELECT e.id, e.canonical_name, e.entity_type, COUNT(mie.memory_id) AS mc "
        "FROM memory_item_entities mie "
        "JOIN memory_items mi ON mie.memory_id = mi.id "
        "JOIN entities       e  ON mie.entity_id  = e.id "
        f"WHERE {where_clause} "
        "GROUP BY e.id, e.canonical_name, e.entity_type "
        "ORDER BY mc DESC, e.canonical_name ASC "
        f"LIMIT {p}"
    )
    count_sql = (
        "SELECT COUNT(DISTINCT e.id) "
        "FROM memory_item_entities mie "
        "JOIN memory_items mi ON mie.memory_id = mi.id "
        "JOIN entities       e  ON mie.entity_id  = e.id "
        f"WHERE {where_clause}"
    )

    with _db() as db:
        total_row = db.execute(count_sql, params).fetchone()
        total = int(total_row[0]) if total_row and total_row[0] is not None else 0
        rows = list(db.execute(rows_sql, [*params, lim]).fetchall())

    return {
        "conversation_id": cid,
        "entity_type": etype or "*",
        "pattern": pat or "",
        "limit": lim,
        "total": total,
        "rows": [
            {
                "entity_id": eid,
                "canonical_name": cname,
                "entity_type": etype_v,
                "mention_count": int(mc),
            }
            for eid, cname, etype_v, mc in rows
        ],
    }


def list_mentions_impl(
    conversation_id: str,
    entity_id: str = "",
    canonical_name: str = "",
    entity_type: str = "",
    limit: int = 0,
) -> dict[str, Any]:
    """Return memory_ids that mention a specific entity in `conversation_id`.

    Specify the entity by EITHER `entity_id` (preferred — exact match) OR
    `canonical_name` (case-insensitive exact match; optionally narrowed by
    `entity_type` if the name is ambiguous across types).

    Args:
        conversation_id: Required, non-empty.
        entity_id: Preferred — the entities.id. If supplied, canonical_name
            and entity_type are ignored.
        canonical_name: Alternative lookup. Case-insensitive exact match.
        entity_type: Optional disambiguator when using canonical_name.
        limit: Max memory_ids to return. Default DEFAULT_LIMIT, cap MAX_LIMIT.

    Returns:
        {
            "conversation_id": str,
            "entity_id": str (the resolved id, or "" if name didn't resolve),
            "canonical_name": str,
            "entity_type": str,
            "limit": int,
            "total": int (total distinct memory_ids matching),
            "memory_ids": [str, ...],
        }

    Raises:
        ValueError: if neither entity_id nor canonical_name is provided, or
            if conversation_id is missing.
    """
    cid = _require_conversation_id(conversation_id)
    lim = _validate_limit(limit)
    eid_in = (entity_id or "").strip()
    cname_in = (canonical_name or "").strip()
    etype_in = (entity_type or "").strip()

    if not eid_in and not cname_in:
        raise ValueError(
            "must provide either entity_id or canonical_name"
        )

    resolved_id = ""
    resolved_cname = ""
    resolved_etype = ""

    _d = dialect()
    p = _d.param()
    ci_name = _d.ci_equals("canonical_name", p)

    with _db() as db:
        # Resolve to a concrete entity row first; cheaper than joining
        # canonical_name against every mention row.
        if eid_in:
            row = db.execute(
                f"SELECT id, canonical_name, entity_type FROM entities WHERE id = {p}",
                (eid_in,),
            ).fetchone()
        else:
            if etype_in:
                row = db.execute(
                    "SELECT id, canonical_name, entity_type FROM entities "
                    f"WHERE {ci_name} AND entity_type = {p} "
                    "ORDER BY canonical_name LIMIT 1",
                    (cname_in, etype_in),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT id, canonical_name, entity_type FROM entities "
                    f"WHERE {ci_name} "
                    "ORDER BY canonical_name LIMIT 1",
                    (cname_in,),
                ).fetchone()

        if row is None:
            return {
                "conversation_id": cid,
                "entity_id": "",
                "canonical_name": cname_in,
                "entity_type": etype_in,
                "limit": lim,
                "total": 0,
                "memory_ids": [],
            }

        resolved_id, resolved_cname, resolved_etype = row

        # Now the per-conversation mentions.
        total_row = db.execute(
            "SELECT COUNT(DISTINCT mie.memory_id) "
            "FROM memory_item_entities mie "
            "JOIN memory_items mi ON mie.memory_id = mi.id "
            f"WHERE mie.entity_id = {p} AND mi.conversation_id = {p}",
            (resolved_id, cid),
        ).fetchone()
        total = int(total_row[0]) if total_row and total_row[0] is not None else 0

        mem_rows = db.execute(
            "SELECT DISTINCT mie.memory_id "
            "FROM memory_item_entities mie "
            "JOIN memory_items mi ON mie.memory_id = mi.id "
            f"WHERE mie.entity_id = {p} AND mi.conversation_id = {p} "
            "ORDER BY mie.memory_id "
            f"LIMIT {p}",
            (resolved_id, cid, lim),
        ).fetchall()
        memory_ids = [r[0] for r in mem_rows]

    return {
        "conversation_id": cid,
        "entity_id": resolved_id,
        "canonical_name": resolved_cname,
        "entity_type": resolved_etype,
        "limit": lim,
        "total": total,
        "memory_ids": memory_ids,
    }
