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

from .config import files_table

logger = logging.getLogger("files_memory.embed")


def embed_texts(texts: list[str]) -> list[tuple[Optional[list[float]], str]]:
    """Embed a batch of texts via the m3-memory cascade.

    Synchronous wrapper around the async ``_embed_many``. Runs in a private event
    loop when no loop is active (the direct-call / standalone-CLI case); bridges
    through a worker thread when a loop is already running on THIS thread.
    """
    if not texts:
        return []

    from memory.embed import _embed_many

    try:
        asyncio.get_running_loop()
        loop_running = True
    except RuntimeError:
        loop_running = False

    if not loop_running:
        return asyncio.run(_embed_many(texts))

    # A loop is already running ON THIS THREAD — asyncio.get_running_loop() only
    # ever returns the current thread's loop. We were called synchronously from
    # inside it: the human CLI runs the SYNC files_ingest impl directly on the
    # event-loop thread (asyncio.run(execute_tool_structured(...)) -> dispatch
    # calls spec.impl() inline), so that loop is BLOCKED on us right now.
    # run_coroutine_threadsafe(coro, loop).result() would deadlock — the loop
    # cannot step the coroutine while it is frozen here (this was the "files_ingest
    # hangs / commits nothing" bug). Bridge through a private loop on a worker
    # thread instead; the shared embed client rebinds per-loop (_get_embed_client).
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(_embed_many(texts))).result()


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
    from memory.backends import dialect as _dialect
    _d = _dialect()
    try:
        # INSERT OR REPLACE → dialect upsert on the (leaf_uuid, kind) PK: overwrite
        # the row's mutable columns when the pair already has an embedding.
        _cols = ["embedding", "embed_model", "dim"]
        conn.execute(
            f"INSERT INTO {files_table('leaf_embeddings')}(leaf_uuid, kind, embedding, embed_model, dim) "
            f"VALUES ({_d.placeholder(5)}) "
            f"{_d.on_conflict_update('(leaf_uuid, kind)', _cols)}",
            (leaf_uuid, kind, pack(vec), model, len(vec)),
        )
        return True
    except Exception as e:  # noqa: BLE001 — best-effort embed write
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
    from memory.backends import dialect as _dialect
    _d = _dialect()
    try:
        _cols = ["embedding", "embed_model", "dim"]
        conn.execute(
            f"INSERT INTO {files_table('file_embeddings')}(file_node_uuid, kind, embedding, embed_model, dim) "
            f"VALUES ({_d.placeholder(5)}) "
            f"{_d.on_conflict_update('(file_node_uuid, kind)', _cols)}",
            (file_node_uuid, kind, pack(vec), model, len(vec)),
        )
        return True
    except Exception as e:  # noqa: BLE001 — best-effort embed write
        logger.warning("write_file_embedding failed for %s: %s", file_node_uuid, e)
        return False


def mark_leaves_embedded(conn: sqlite3.Connection, leaf_uuids: list[str]) -> None:
    """Flip `embedded=1` on all leaves in `leaf_uuids` that have a text embedding."""
    if not leaf_uuids:
        return
    # Chunk to stay under SQLITE_MAX_VARIABLE_NUMBER
    from memory.backends import dialect as _dialect
    _d = _dialect()
    CHUNK = 500
    for start in range(0, len(leaf_uuids), CHUNK):
        chunk = leaf_uuids[start:start + CHUNK]
        placeholders = _d.placeholder(len(chunk))
        conn.execute(
            f"UPDATE {files_table('leaves')} SET embedded = 1 "
            f"WHERE uuid IN ({placeholders}) "
            f"  AND uuid IN (SELECT leaf_uuid FROM {files_table('leaf_embeddings')} WHERE kind = 'text')",
            chunk,
        )
