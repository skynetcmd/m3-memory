"""The single source of mapping truth: CrewAI ``MemoryRecord`` ⇄ m3 row.

DESIGN_PHILOSOPHIES §3 (structured returns): all record↔row conversion lives
HERE — pure functions of their inputs, no I/O — so the whole thing is hermetically
testable with no live CrewAI and no live m3 (the ``test_provider_logic.py``
pattern; §3 "a test that passes only because a live service is reachable is not
hermetic"). Four CrewAI realities are absorbed here so ``backend.py`` never has to
know them:

  1. **Scope is a ``/``-path with prefix semantics.** CrewAI passes
     ``scope_prefix="/crew/research/facts"`` and expects descendant matches
     (``LIKE 'prefix%'``). m3's ``scope`` column is a bounded category
     ({agent,user,session,org}); the CrewAI path is orthogonal, so it rides
     ``metadata_json`` under a reserved key and is matched with a prefix filter.
  2. **Embedder identity.** CrewAI embeds with its OWN embedder (dim varies:
     3072 for the default text-embedding-3-large, 768/1024 for local models). The
     stored vector is tagged with a per-dim ``embed_model`` identity so m3's
     identity guard keeps it separate from m3's native bge-m3 space — and so two
     CrewAI deployments on different embedders don't collide. Derived from the
     vector, never user-configured (there is no phantom ``M3_CREWAI_EMBEDDER_URL``
     — m3 receives vectors, it never calls a CrewAI embedder).
  3. **Score direction.** CrewAI's ``compute_composite_score`` expects a
     similarity where HIGHER = better in (0, 1]. m3's ``VectorHit.score`` is
     cosine (higher better) — already the right direction; ``KeywordHit`` is not
     used on this path.
  4. **First-class fields.** ``importance``/``created_at``/``last_accessed``/
     ``private``/``source`` on a ``MemoryRecord`` map onto m3's own columns —
     CrewAI re-scores with recency+importance, so these must round-trip.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

# Reserved metadata_json keys the adapter owns. Namespaced so they can't collide
# with a user's own metadata coming down from a CrewAI record.
SCOPE_PATH_KEY = "_crewai_scope"       # the /-delimited CrewAI scope path
CATEGORIES_KEY = "_crewai_categories"  # CrewAI's category list
PRIVATE_KEY = "_crewai_private"        # CrewAI's private flag
CREWAI_SOURCE_KEY = "_crewai_source"   # CrewAI's per-record source tag

# m3 columns surfaced back into each search-result item so a MemoryRecord can be
# reconstructed with accurate scoring fields (§3 round-trip).
EXTRA_COLUMNS = ["metadata_json", "importance", "created_at", "user_id"]


def crewai_embed_model(dim: int) -> str:
    """The ``embed_model`` identity tag for a CrewAI-supplied vector of ``dim``.

    Derived from the dimension alone — NOT user-configured — so:
      * m3's ``vector_search`` identity guard keeps CrewAI vectors in their own
        space, distinct from m3's native bge-m3 (1024) vectors on the same item
        (the dual-embed model), and
      * two CrewAI deployments with different embedders (e.g. 3072 default vs a
        local 768) land in distinct spaces automatically, no config.

    ``crewai_embed_model(3072) -> "crewai-3072"``. The stable ``crewai-`` prefix
    lets the search path select exactly the CrewAI space via ``embed_models=``.
    """
    if dim < 1:
        raise ValueError(f"embedding dim must be >= 1, got {dim}")
    return f"crewai-{dim}"


def normalize_scope_prefix(scope_prefix: "str | None") -> str:
    """Normalize a CrewAI scope path to the canonical form m3 stores/matches.

    CrewAI paths are ``/``-delimited absolute paths; a bare ``/`` or empty means
    "no scope filter / everything" (matches CrewAI's LanceDB backend, which treats
    ``prefix.strip('/')`` falsy as unscoped). Returns ``""`` for the match-all
    case, else a leading-slash path with no trailing slash.
    """
    if not scope_prefix:
        return ""
    s = scope_prefix.strip()
    if not s or s == "/":
        return ""
    if not s.startswith("/"):
        s = "/" + s
    return s.rstrip("/")


def scope_matches(record_scope_path: str, query_prefix: str) -> bool:
    """True iff a record's stored scope path is at/under ``query_prefix``.

    Prefix/descendant semantics: ``/crew/research`` matches ``/crew/research``,
    ``/crew/research/facts``, etc. — but NOT ``/crew/researchers`` (a path-segment
    boundary, so ``/crew/research`` does not match ``/crew/research2``). An empty
    ``query_prefix`` matches everything (the unscoped case).
    """
    if not query_prefix:
        return True
    rp = record_scope_path or "/"
    if rp == query_prefix:
        return True
    return rp.startswith(query_prefix + "/")


def _loads_metadata(raw: Any) -> dict:
    """metadata_json may arrive as a dict (already parsed) or a JSON string."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _parse_dt(raw: Any) -> datetime:
    """m3 timestamps are ISO-8601 strings; coerce to an aware datetime (now on a
    miss — the MemoryRecord contract wants a datetime, never None)."""
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str) and raw.strip():
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass
    return datetime.now(timezone.utc)


def record_to_write_args(record: Any, *, user_id: str, scope: str) -> dict:
    """A CrewAI ``MemoryRecord`` → kwargs for m3 ``memory_write``.

    The record's ``scope`` path, ``categories``, ``private`` flag and ``source``
    ride ``metadata_json`` under reserved keys (so they round-trip and are
    prefix-matchable); the record's own ``metadata`` dict rides alongside.
    ``importance`` maps to m3's importance column. ``user_id``/``scope`` (m3
    category) are the tenancy stamp the backend enforces (§7).
    """
    md = dict(getattr(record, "metadata", None) or {})
    md[SCOPE_PATH_KEY] = normalize_scope_prefix(getattr(record, "scope", "") or "")
    md[CATEGORIES_KEY] = list(getattr(record, "categories", None) or [])
    md[PRIVATE_KEY] = bool(getattr(record, "private", False))
    src = getattr(record, "source", None)
    if src:
        md[CREWAI_SOURCE_KEY] = str(src)
    importance = getattr(record, "importance", 0.5)
    try:
        importance = float(importance)
    except (TypeError, ValueError):
        importance = 0.5
    return {
        "type": "conversation",
        "content": str(getattr(record, "content", "") or ""),
        "user_id": user_id,
        "scope": scope,
        "importance": importance,
        "metadata": md,
        "source": "crewai",
    }


def item_to_record(item: dict, *, record_cls: Any) -> Any:
    """An m3 result-item dict → a CrewAI ``MemoryRecord``.

    Reconstructs the CrewAI-facing fields from the reserved metadata_json keys +
    m3's own columns. ``record_cls`` is ``crewai.memory.types.MemoryRecord``,
    passed in so this module never imports crewai (keeps it hermetically testable).
    """
    md_all = _loads_metadata(item.get("metadata_json"))
    scope_path = md_all.pop(SCOPE_PATH_KEY, "/") or "/"
    categories = md_all.pop(CATEGORIES_KEY, []) or []
    private = bool(md_all.pop(PRIVATE_KEY, False))
    source = md_all.pop(CREWAI_SOURCE_KEY, None)
    created = _parse_dt(item.get("created_at"))
    last_acc = _parse_dt(item.get("last_accessed_at") or item.get("created_at"))
    try:
        importance = float(item.get("importance", 0.5))
    except (TypeError, ValueError):
        importance = 0.5
    return record_cls(
        id=str(item.get("id", "")),
        content=str(item.get("content", "") or ""),
        scope=scope_path,
        categories=list(categories),
        metadata=md_all,
        importance=importance,
        created_at=created,
        last_accessed=last_acc,
        embedding=None,  # excluded from CrewAI serialization anyway
        source=str(source) if source else None,
        private=private,
    )
