from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from memory_core import (
    ctx, _pack, _unpack, CHROMA_BASE_URL, CHROMA_COLLECTION, CHROMA_V2_PREFIX, CHROMA_CONTENT_MAX, EMBED_DIM
)
import migrate_memory

logger = logging.getLogger("memory_sync")

@contextmanager
def _get_db(db_path: str):
    """Simple context manager for any SQLite DB."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def _table_exists(db, table_name: str) -> bool:
    """Check if a table exists in the SQLite DB."""
    res = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)).fetchone()
    return res is not None

def _check_queue_health(db) -> tuple[bool, int, str]:
    """Check ChromaDB sync queue health and enforce size caps.

    Returns (should_proceed, queue_size, message) where:
    - should_proceed=False if skip threshold exceeded
    - message is empty string if no action needed, otherwise contains warning/info
    """
    queue_size = db.execute("SELECT COUNT(*) FROM chroma_sync_queue").fetchone()[0]

    skip_at = int(os.environ.get("M3_CHROMA_SYNC_QUEUE_SKIP_AT", "0"))
    cap_max = int(os.environ.get("M3_CHROMA_SYNC_QUEUE_MAX", "500000"))
    warn_at = int(os.environ.get("M3_CHROMA_SYNC_QUEUE_WARN", "100000"))

    # Check skip-cycle threshold first (hard stop)
    if skip_at > 0 and queue_size > skip_at:
        msg = f"Queue size {queue_size} exceeds M3_CHROMA_SYNC_QUEUE_SKIP_AT={skip_at}; skipping ChromaDB sync this cycle"
        return False, queue_size, msg

    # Check hard cap (drops oldest entries)
    if queue_size > cap_max:
        dropped = queue_size - cap_max
        db.execute("""
            DELETE FROM chroma_sync_queue
            WHERE rowid IN (
                SELECT rowid FROM chroma_sync_queue
                ORDER BY rowid ASC LIMIT ?
            )
        """, (dropped,))
        db.commit()
        msg = f"WARNING: Queue size {queue_size} exceeded M3_CHROMA_SYNC_QUEUE_MAX={cap_max}; dropped {dropped} oldest entries"
        return True, cap_max, msg

    # Check soft warning threshold
    if queue_size > warn_at:
        msg = f"Queue size {queue_size} exceeds warning threshold M3_CHROMA_SYNC_QUEUE_WARN={warn_at}"
        return True, queue_size, msg

    return True, queue_size, ""

async def _get_collection_dim(client, col_path) -> int | None:
    """Query ChromaDB collection to determine its expected embedding dimension."""
    try:
        resp = await client.post(f"{col_path}/get", json={"limit": 1, "include": ["embeddings"]}, timeout=10.0)
        data = resp.json()
        embeddings = data.get("embeddings") or []
        if embeddings and embeddings[0]:
            return len(embeddings[0])
    except Exception:
        pass
    return None


async def _push_to_chroma(client, col_id, col_path, max_items, target):
    with _get_db(target.db_path) as db:
        if not _table_exists(db, "chroma_sync_queue"):
            logger.debug(f"[{target.name}] Skipping push: chroma_sync_queue table not found.")
            return 0, 0, ""

        queue = db.execute(
            "SELECT q.id, q.memory_id, q.operation "
            "FROM chroma_sync_queue q "
            "LEFT JOIN memory_embeddings e ON e.memory_id = q.memory_id "
            "WHERE q.attempts < 3 "
            "ORDER BY (e.memory_id IS NULL), q.queued_at ASC "
            "LIMIT ?",
            (max_items,)
        ).fetchall()
        if not queue: return 0, 0, ""

        # Detect collection dimension to filter mismatched embeddings
        col_dim = await _get_collection_dim(client, col_path)

        batch_ids, batch_vecs, batch_docs, batch_metas, batch_qids = [], [], [], [], []
        delete_ids, delete_qids, skip_qids = [], [], []

        for qrow in queue:
            qid, mid, op = qrow["id"], qrow["memory_id"], qrow["operation"]
            if op == "upsert":
                item = db.execute("SELECT * FROM memory_items WHERE id = ?", (mid,)).fetchone()
                emb = db.execute("SELECT embedding FROM memory_embeddings WHERE memory_id = ? LIMIT 1", (mid,)).fetchone()
                if item and emb:
                    vec = _unpack(emb["embedding"])
                    # Skip vectors with wrong dimension for this collection
                    if col_dim and len(vec) != col_dim:
                        logger.warning(f"[{target.name}] Dimension mismatch for {mid}: got {len(vec)}, collection expects {col_dim} — skipping")
                        skip_qids.append(qid)
                        continue
                    batch_ids.append(mid)
                    batch_vecs.append(vec)
                    batch_docs.append((item["content"] or "")[:CHROMA_CONTENT_MAX])
                    batch_metas.append({
                        "type": item["type"], "title": item["title"] or "",
                        "origin_device": item["origin_device"] or "",
                        "importance": str(item["importance"] or 0.5),
                        "created_at": item["created_at"] or "",
                        "target_db": target.name
                    })
                    batch_qids.append(qid)
                else:
                    logger.info(f"[{target.name}] skip qid={qid} mid={mid} reason={'no_item' if not item else 'no_embedding'}")
                    skip_qids.append(qid)
            elif op == "delete":
                delete_ids.append(mid); delete_qids.append(qid)

        synced, failed = 0, 0
        if batch_ids:
            try:
                await client.post(f"{col_path}/upsert", json={"ids": batch_ids, "embeddings": batch_vecs, "documents": batch_docs, "metadatas": batch_metas}, timeout=30.0)
                synced += len(batch_ids)
                if batch_qids:
                    db.execute(f"DELETE FROM chroma_sync_queue WHERE id IN ({','.join(['?']*len(batch_qids))})", batch_qids)
            except Exception as e:
                logger.exception(f"[{target.name}] ChromaDB upsert failed for {len(batch_ids)} items: {e}")
                failed += len(batch_ids)

        if delete_ids:
            try:
                await client.post(f"{col_path}/delete", json={"ids": delete_ids}, timeout=30.0)
                synced += len(delete_ids)
                if delete_qids:
                    db.execute(f"DELETE FROM chroma_sync_queue WHERE id IN ({','.join(['?']*len(delete_qids))})", delete_qids)
            except Exception as e:
                logger.exception(f"[{target.name}] ChromaDB delete failed for {len(delete_ids)} items: {e}")
                failed += len(delete_ids)

        if skip_qids:
            db.execute(f"DELETE FROM chroma_sync_queue WHERE id IN ({','.join(['?']*len(skip_qids))})", skip_qids)
            logger.warning(f"[{target.name}] dropped {len(skip_qids)} orphan queue rows (no item or embedding)")
        return synced, failed, ""

async def _pull_from_chroma(client, col_id, col_path, max_items, target):
    """Pulls new items from ChromaDB and stores them in chroma_mirror.
    Currently only pulls into the 'main' target.
    """
    if target.name != "main":
        return 0, 0, ""

    from datetime import datetime, timezone
    
    # 1. Get last pull timestamp
    last_pull = "1970-01-01T00:00:00Z"
    with _get_db(target.db_path) as db:
        if not _table_exists(db, "sync_state"):
            return 0, 0, ""
        row = db.execute("SELECT last_pull_at FROM sync_state WHERE collection_name = ?", (CHROMA_COLLECTION,)).fetchone()
        if row: last_pull = row[0]

    # 2. Query Chroma for items
    pulled, failed = 0, 0
    try:
        resp = await client.post(f"{col_path}/get", json={
            "limit": max_items,
            "include": ["documents", "metadatas", "embeddings"]
        }, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
        
        if not data.get("ids"): return 0, 0, ""

        ids = data["ids"]
        documents = data.get("documents") or []
        metadatas = data.get("metadatas") or []
        embeddings_list = data.get("embeddings") or []
        if not (len(ids) == len(documents) == len(metadatas) == len(embeddings_list)):
            logger.error(f"[{target.name}] ChromaDB pull: array length mismatch")
            return 0, 0, ""

        with _get_db(target.db_path) as db:
            if not _table_exists(db, "chroma_mirror"):
                return 0, 0, ""
                
            for i in range(len(ids)):
                mid = ids[i]
                meta = metadatas[i]
                content = documents[i]
                emb_vec = embeddings_list[i]

                if emb_vec and len(emb_vec) != EMBED_DIM:
                    continue

                db.execute("""
                    INSERT INTO chroma_mirror 
                    (id, type, title, content, metadata_json, agent_id, model_id, origin_device, importance, remote_created_at, pulled_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        content = excluded.content,
                        title = excluded.title,
                        importance = excluded.importance,
                        pulled_at = excluded.pulled_at
                """, (
                    mid, meta.get("type", "note"), meta.get("title", ""), content, 
                    json.dumps(meta), meta.get("agent_id", ""), meta.get("model_id", ""),
                    meta.get("origin_device", ""), meta.get("importance", 0.5),
                    meta.get("created_at", last_pull), datetime.now(timezone.utc).isoformat()
                ))
                
                db.execute("""
                    INSERT INTO chroma_mirror_embeddings (id, mirror_id, embedding, dim, pulled_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        embedding = excluded.embedding,
                        pulled_at = excluded.pulled_at
                """, (mid, mid, _pack(emb_vec), len(emb_vec), datetime.now(timezone.utc).isoformat()))
                
                pulled += 1

            db.execute("INSERT OR REPLACE INTO sync_state (collection_name, last_pull_at) VALUES (?, ?)",
                      (CHROMA_COLLECTION, datetime.now(timezone.utc).isoformat()))
            
    except Exception as e:
        logger.exception(f"[{target.name}] ChromaDB pull failed: {e}")
        failed = max_items

    return pulled, failed, ""

async def chroma_sync_impl(max_items=50, direction="both", reset_stalled=True):
    if direction not in ("push", "pull", "both"):
        return f"Error: invalid direction '{direction}'."

    targets = migrate_memory.targets("all")

    if reset_stalled:
        for target in targets:
            try:
                with _get_db(target.db_path) as db:
                    if _table_exists(db, "chroma_sync_queue"):
                        db.execute("UPDATE chroma_sync_queue SET attempts = 0, stalled_since = NULL WHERE attempts >= 3")
            except Exception:
                pass

    # Check queue health before proceeding with sync
    skip_reason = ""
    for target in targets:
        try:
            with _get_db(target.db_path) as db:
                if _table_exists(db, "chroma_sync_queue"):
                    should_proceed, queue_size, msg = _check_queue_health(db)
                    if msg:
                        if "WARNING" in msg:
                            logger.warning(f"[{target.name}] {msg}")
                        else:
                            logger.info(f"[{target.name}] {msg}")
                    if not should_proceed:
                        skip_reason = f" (target {target.name}: {msg})"
                        return f"ChromaDB sync skipped{skip_reason}"
        except Exception as e:
            logger.debug(f"Queue health check failed for {target.name}: {e}")

    if not CHROMA_BASE_URL:
        return "ChromaDB sync skipped: CHROMA_BASE_URL not set."

    client = ctx.get_async_client()
    try:
        url = f"{CHROMA_BASE_URL}{CHROMA_V2_PREFIX}/{CHROMA_COLLECTION}"
        resp = await client.get(url, timeout=10.0)
        resp.raise_for_status()
        col_id = resp.json()["id"]
    except Exception as e:
        logger.error(f"ChromaDB unreachable: {e}")
        return "ChromaDB unreachable"

    col_path = f"{CHROMA_BASE_URL}{CHROMA_V2_PREFIX}/{col_id}"
    
    total_pushed, total_pulled, total_failed = 0, 0, 0
    
    for target in targets:
        logger.info(f"--- Chroma Sync target: {target.name} ---")
        pushed, p_failed = 0, 0
        pulled, l_failed = 0, 0
        
        if direction in ("push", "both"):
            pushed, p_failed, _ = await _push_to_chroma(client, col_id, col_path, max_items, target)
        
        if direction in ("pull", "both"):
            pulled, l_failed, _ = await _pull_from_chroma(client, col_id, col_path, max_items, target)
        
        total_pushed += pushed
        total_pulled += pulled
        total_failed += (p_failed + l_failed)
        
    return f"Synced: {total_pushed} pushed, {total_pulled} pulled, {total_failed} failed"
