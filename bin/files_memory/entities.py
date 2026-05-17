"""Entity linking — bridge from files.db facts to memory.db entities.

Entities are the shared connective tissue across stores (plan §8). They
live in memory.db; files.db only stores UUID references via
`fact_entity_refs`. This module is the ONLY place that crosses the
DB boundary for entity work.

Resolution policy per candidate entity name:
  1. Exact (case-insensitive) match on canonical_name → link to existing.
  2. No match → create provisional entity in memory.db with
     entity_type='unknown' and attributes_json={'provisional': true,
     'first_seen_in': 'files.db'}. Provisional entities surface in
     memory_dedup for human review later.

We deliberately DO NOT do semantic / fuzzy matching here. The existing
memory.entity._semantic_match is async and depends on the memory.db
embedder; calling it during a synchronous file-ingest transaction would
deadlock the DB or block on embed calls. Phase 3 can add a post-ingest
dedup pass that uses semantic matching to coalesce provisionals.

Public API:
    link_facts_to_entities(conn, fact_uuids, entities_per_fact) -> None
        Writes rows to fact_entity_refs (in files.db). Resolves each
        canonical name against memory.db, creating provisional entities
        as needed.

    resolve_entity_uuid(name) -> tuple[str, bool]
        Returns (entity_uuid, was_created). Used by ad-hoc callers.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid as _uuid
from contextlib import contextmanager
from typing import Iterable, Iterator, Optional

from . import config

logger = logging.getLogger("files_memory.entities")


# ──────────────────────────────────────────────────────────────────────────────
# memory.db connection (separate from files.db)
# ──────────────────────────────────────────────────────────────────────────────
@contextmanager
def _memory_db() -> Iterator[sqlite3.Connection]:
    """Yield a connection to memory.db.

    Resolves via the active M3Context — same path the rest of m3-memory
    uses. We open a fresh connection (not pooled) because entity writes
    are infrequent and we don't want to fight memory.db's connection
    pool for transaction ownership.
    """
    # Resolve via m3_sdk so we honor M3_DATABASE env + active_database().
    try:
        from m3_sdk import resolve_db_path
        path = config.MEMORY_DB_PATH or resolve_db_path(None)
    except ImportError:
        # Fallback for tests that don't have m3_sdk on path.
        path = config.MEMORY_DB_PATH or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "memory", "agent_memory.db",
        )

    if not os.path.isfile(path):
        # No memory.db — caller decides what to do (typically skip linking).
        raise FileNotFoundError(f"memory.db not found at {path}")

    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# Resolution
# ──────────────────────────────────────────────────────────────────────────────
def _normalize(name: str) -> str:
    return name.strip().casefold()


def _find_existing(conn: sqlite3.Connection, name: str) -> Optional[str]:
    """Case-insensitive exact match on canonical_name. Returns entity_id or None."""
    row = conn.execute(
        "SELECT id FROM entities "
        "WHERE LOWER(canonical_name) = ? "
        "LIMIT 1",
        (_normalize(name),),
    ).fetchone()
    return row[0] if row else None


def _create_provisional(conn: sqlite3.Connection, name: str) -> str:
    """Create a new entity flagged as provisional. Returns new entity_id."""
    eid = str(_uuid.uuid4())
    attrs = {
        "provisional": True,
        "first_seen_in": "files.db",
        "source": "files_memory.extract",
    }
    attrs_json = json.dumps(attrs)
    import hashlib as _h
    content_hash = _h.sha256(
        f"{name}|unknown|{attrs_json}".encode("utf-8")
    ).hexdigest()
    try:
        conn.execute(
            "INSERT INTO entities(id, canonical_name, entity_type, attributes_json, content_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            (eid, name.strip(), "unknown", attrs_json, content_hash),
        )
    except sqlite3.IntegrityError:
        # Unique-constraint race (canonical_name uniqueness on some
        # installs). Re-resolve.
        existing = _find_existing(conn, name)
        if existing:
            return existing
        raise
    return eid


def resolve_entity_uuid(name: str, *, autocreate: bool = True) -> tuple[Optional[str], bool]:
    """Resolve `name` to an entity UUID in memory.db.

    Returns (uuid_or_none, was_created). If autocreate=False and no
    existing entity matches, returns (None, False).

    Falls back to (None, False) — no exception — if memory.db is
    unavailable. Callers must handle the no-link case.
    """
    if not name or not name.strip():
        return (None, False)
    try:
        with _memory_db() as conn:
            existing = _find_existing(conn, name)
            if existing:
                return (existing, False)
            if not autocreate:
                return (None, False)
            new_id = _create_provisional(conn, name)
            return (new_id, True)
    except FileNotFoundError:
        logger.debug("memory.db unavailable; skipping entity link for %r", name)
        return (None, False)
    except sqlite3.Error as e:
        logger.warning("entity resolution failed for %r: %s", name, e)
        return (None, False)


# ──────────────────────────────────────────────────────────────────────────────
# Batched linking — used by extract.write_extraction_result
# ──────────────────────────────────────────────────────────────────────────────
def link_facts_to_entities(
    files_conn: sqlite3.Connection,
    fact_uuids: list[str],
    entities_per_fact: list[list[str]],
    *,
    confidence: float = 0.7,
) -> None:
    """Resolve each candidate entity and write fact_entity_refs rows.

    Done in a single memory.db connection (one INSERT per new entity,
    amortizing connection setup over the whole batch). Writes to files.db
    happen on the caller-provided connection so the entire extraction
    write stays atomic in files.db's transaction.

    Args:
        files_conn: the open files.db connection inside an active txn.
        fact_uuids: list of fact UUIDs (files.db).
        entities_per_fact: parallel list — entities_per_fact[i] is the
            candidate entity names for fact_uuids[i].
        confidence: link confidence stored on every row.
    """
    if not fact_uuids:
        return
    # Flatten unique names so we hit memory.db once per name, not once
    # per (fact, name) pair.
    name_to_uuid: dict[str, Optional[str]] = {}
    unique_names: list[str] = []
    for elist in entities_per_fact:
        for n in elist:
            key = _normalize(n)
            if key and key not in name_to_uuid:
                name_to_uuid[key] = None
                unique_names.append(n)

    if not unique_names:
        return

    try:
        with _memory_db() as mem:
            # Phase 1: lookup all unique names.
            CHUNK = 200
            for start in range(0, len(unique_names), CHUNK):
                chunk = unique_names[start:start + CHUNK]
                lowered = [_normalize(n) for n in chunk]
                placeholders = ",".join("?" * len(lowered))
                rows = mem.execute(
                    f"SELECT LOWER(canonical_name) AS lname, id FROM entities "
                    f"WHERE LOWER(canonical_name) IN ({placeholders})",
                    lowered,
                ).fetchall()
                for row in rows:
                    name_to_uuid[row["lname"]] = row["id"]
            # Phase 2: create provisional entities for the misses.
            for n in unique_names:
                key = _normalize(n)
                if name_to_uuid.get(key) is None:
                    try:
                        name_to_uuid[key] = _create_provisional(mem, n)
                    except sqlite3.Error as e:
                        logger.warning("provisional entity create failed for %r: %s", n, e)
                        name_to_uuid[key] = None
    except FileNotFoundError:
        # No memory.db → skip linking entirely. Facts still get written;
        # they just have no entity_refs.
        logger.debug("memory.db unavailable; skipping entity linking for %d facts",
                     len(fact_uuids))
        return

    # Write fact_entity_refs to files.db.
    for fact_uuid, elist in zip(fact_uuids, entities_per_fact):
        seen: set[str] = set()
        for n in elist:
            key = _normalize(n)
            ent_uuid = name_to_uuid.get(key)
            if not ent_uuid or ent_uuid in seen:
                continue
            seen.add(ent_uuid)
            try:
                files_conn.execute(
                    "INSERT OR IGNORE INTO fact_entity_refs(fact, entity_uuid, confidence) "
                    "VALUES (?, ?, ?)",
                    (fact_uuid, ent_uuid, confidence),
                )
            except sqlite3.Error as e:
                logger.warning("fact_entity_refs insert failed: %s", e)
