"""Embedding integration for files.db.

Wraps `memory.embed._embed_many` (the existing cascade: in-process Rust
→ CPU HTTP fallback → primary HTTP). Vectors come back to us and we
write them to files.db's `leaf_embeddings` / `file_embeddings` tables.

The content-hash cache in memory.db is reused — same model_tag, same
hash → same vector. So even though the storage is in two DBs, the
expensive compute is shared.

Public API:
    embed_texts(texts) -> list[tuple[vec, model] | (None, model)]
    write_leaf_embeddings(leaf_uuid, kind, vec, model, conn)
    write_file_embedding(file_node_uuid, vec, model, conn)
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Optional

from embedding_utils import pack

logger = logging.getLogger("files_memory.embed")


def embed_texts(texts: list[str]) -> list[tuple[Optional[list[float]], str]]:
    """Embed a batch of texts via the m3-memory cascade.

    Synchronous wrapper around the async _embed_many. Runs in a private
    event loop if no loop is active (typical for CLI calls); reuses the
    current loop if invoked from async context.
    """
    if not texts:
        return []

    from memory.embed import _embed_many

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None or loop.is_closed():
        return asyncio.run(_embed_many(texts))

    # In an async context — run a nested loop. This is rare for the
    # ingester (which is its own CLI) but safe.
    fut = asyncio.run_coroutine_threadsafe(_embed_many(texts), loop)
    return fut.result()


def write_leaf_embedding(
    conn: sqlite3.Connection,
    leaf_uuid: str,
    kind: str,
    vec: Optional[list[float]],
    model: str,
) -> bool:
    """Insert (or update) one row in leaf_embeddings. Returns True on success."""
    if vec is None:
        return False
    try:
        conn.execute(
            "INSERT OR REPLACE INTO leaf_embeddings(leaf_uuid, kind, embedding, embed_model, dim) "
            "VALUES (?, ?, ?, ?, ?)",
            (leaf_uuid, kind, pack(vec), model, len(vec)),
        )
        return True
    except sqlite3.Error as e:
        logger.warning("write_leaf_embedding failed for %s/%s: %s", leaf_uuid, kind, e)
        return False


def write_file_embedding(
    conn: sqlite3.Connection,
    file_node_uuid: str,
    vec: Optional[list[float]],
    model: str,
    kind: str = "summary",
) -> bool:
    if vec is None:
        return False
    try:
        conn.execute(
            "INSERT OR REPLACE INTO file_embeddings(file_node_uuid, kind, embedding, embed_model, dim) "
            "VALUES (?, ?, ?, ?, ?)",
            (file_node_uuid, kind, pack(vec), model, len(vec)),
        )
        return True
    except sqlite3.Error as e:
        logger.warning("write_file_embedding failed for %s: %s", file_node_uuid, e)
        return False


def mark_leaves_embedded(conn: sqlite3.Connection, leaf_uuids: list[str]) -> None:
    """Flip `embedded=1` on all leaves in `leaf_uuids` that have a text embedding."""
    if not leaf_uuids:
        return
    # Chunk to stay under SQLITE_MAX_VARIABLE_NUMBER
    CHUNK = 500
    for start in range(0, len(leaf_uuids), CHUNK):
        chunk = leaf_uuids[start:start + CHUNK]
        placeholders = ",".join("?" * len(chunk))
        conn.execute(
            f"UPDATE leaves SET embedded = 1 "
            f"WHERE uuid IN ({placeholders}) "
            f"  AND uuid IN (SELECT leaf_uuid FROM leaf_embeddings WHERE kind = 'text')",
            chunk,
        )
