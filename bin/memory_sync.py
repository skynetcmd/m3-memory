from __future__ import annotations

import json
import logging

from memory_core import (
    CHROMA_BASE_URL,
    CHROMA_COLLECTION,
    CHROMA_CONTENT_MAX,
    CHROMA_V2_PREFIX,
    EMBED_DIM,
    _db,
    _pack,
    _unpack,
)

logger = logging.getLogger("memory_sync")

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


async def _push_to_chroma(client, col_id, col_path, max_items):
    with _db() as db:
        queue = db.execute("SELECT id, memory_id, operation FROM chroma_sync_queue WHERE attempts < 3 ORDER BY queued_at ASC LIMIT ?", (max_items,)).fetchall()
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
                        logger.warning(f"Dimension mismatch for {mid}: got {len(vec)}, collection expects {col_dim} — skipping")
                        skip_qids.append(qid)
                        continue
                    batch_ids.append(mid)
                    batch_vecs.append(vec)
                    batch_docs.append((item["content"] or "")[:CHROMA_CONTENT_MAX])
                    batch_metas.append({
                        "type": item["type"], "title": item["title"] or "",
                        "origin_device": item["origin_device"] or "",
                        "importance": str(item["importance"] or 0.5),
                        "created_at": item["created_at"] or ""
                    })
                    batch_qids.append(qid)
                else: skip_qids.append(qid)
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
                logger.exception(f"ChromaDB upsert failed for {len(batch_ids)} items: {e}")
                failed += len(batch_ids)

        if delete_ids:
            try:
                await client.post(f"{col_path}/delete", json={"ids": delete_ids}, timeout=30.0)
                synced += len(delete_ids)
                if delete_qids:
                    db.execute(f"DELETE FROM chroma_sync_queue WHERE id IN ({','.join(['?']*len(delete_qids))})", delete_qids)
            except Exception as e:
                logger.exception(f"ChromaDB delete failed for {len(delete_ids)} items: {e}")
                failed += len(delete_ids)

        if skip_qids:
            db.execute(f"DELETE FROM chroma_sync_queue WHERE id IN ({','.join(['?']*len(skip_qids))})", skip_qids)
        return synced, failed, ""

async def _pull_from_chroma(client, col_id, col_path, max_items):
    """Pulls new items from ChromaDB and stores them in chroma_mirror."""
    from datetime import datetime, timezone

    # 1. Get last pull timestamp
    last_pull = "1970-01-01T00:00:00Z"
    with _db() as db:
        row = db.execute("SELECT last_pull_at FROM sync_state WHERE collection_name = ?", (CHROMA_COLLECTION,)).fetchone()
        if row: last_pull = row[0]

    # 2. Query Chroma for items updated since last_pull
    # Chroma doesn't have a direct 'greater than' filter for metadatas in some versions,
    # but we'll attempt a metadata filter. If it fails, we fetch and filter locally.
    pulled, failed = 0, 0
    try:
        # Note: Chroma's python client/API handles filters differently across versions.
        # We'll fetch the most recent items and filter.
        resp = await client.post(f"{col_path}/get", json={
            "limit": max_items,
            "include": ["documents", "metadatas", "embeddings"]
        }, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()

        if not data.get("ids"): return 0, 0, ""

        # Validate parallel arrays have consistent lengths
        ids = data["ids"]
        documents = data.get("documents") or []
        metadatas = data.get("metadatas") or []
        embeddings_list = data.get("embeddings") or []
        if not (len(ids) == len(documents) == len(metadatas) == len(embeddings_list)):
            logger.error(f"ChromaDB pull: array length mismatch — ids={len(ids)}, docs={len(documents)}, metas={len(metadatas)}, embs={len(embeddings_list)}")
            return 0, 0, ""

        with _db() as db:
            for i in range(len(ids)):
                mid = ids[i]
                meta = metadatas[i]
                content = documents[i]
                emb_vec = embeddings_list[i]

                # Validate embedding dimension before storing
                if emb_vec and len(emb_vec) != EMBED_DIM:
                    logger.warning(f"ChromaDB pull: dimension mismatch for {mid}: got {len(emb_vec)}, expected {EMBED_DIM} — skipping")
                    continue

                # Skip if it's our own local item (optional, depending on architecture)
                # if meta.get("origin_device") == platform.node(): continue

                # Insert into mirror
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

                # Insert embedding
                db.execute("""
                    INSERT INTO chroma_mirror_embeddings (id, mirror_id, embedding, dim, pulled_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        embedding = excluded.embedding,
                        pulled_at = excluded.pulled_at
                """, (mid, mid, _pack(emb_vec), len(emb_vec), datetime.now(timezone.utc).isoformat()))

                pulled += 1

            # Update sync state
            db.execute("INSERT OR REPLACE INTO sync_state (collection_name, last_pull_at) VALUES (?, ?)",
                      (CHROMA_COLLECTION, datetime.now(timezone.utc).isoformat()))

    except Exception as e:
        logger.exception(f"ChromaDB pull failed: {e}")
        failed = max_items # approximation

    return pulled, failed, ""

async def chroma_sync_impl(max_items=50, direction="both", reset_stalled=True):
    if direction not in ("push", "pull", "both"):
        return f"Error: invalid direction '{direction}'. Must be push, pull, or both."
    from memory_core import ctx
    if reset_stalled:
        with _db() as db:
            db.execute("UPDATE chroma_sync_queue SET attempts = 0, stalled_since = NULL WHERE attempts >= 3")

    if not CHROMA_BASE_URL:
        logger.debug("ChromaDB sync skipped: CHROMA_BASE_URL not set.")
        return "ChromaDB sync skipped: CHROMA_BASE_URL not set."

    client = ctx.get_async_client()
    try:
        url = f"{CHROMA_BASE_URL}{CHROMA_V2_PREFIX}/{CHROMA_COLLECTION}"
        if not url.startswith("http"):
            logger.error(f"Invalid CHROMA_BASE_URL: {CHROMA_BASE_URL} (missing protocol)")
            return "ChromaDB unreachable: Invalid URL"

        resp = await client.get(url, timeout=10.0)
        resp.raise_for_status()
        col_id = resp.json()["id"]
    except Exception as e:
        logger.error(f"ChromaDB unreachable: {e}")
        return "ChromaDB unreachable"

    col_path = f"{CHROMA_BASE_URL}{CHROMA_V2_PREFIX}/{col_id}"

    pushed, p_failed = 0, 0
    pulled, l_failed = 0, 0

    if direction in ("push", "both"):
        pushed, p_failed, _ = await _push_to_chroma(client, col_id, col_path, max_items)

    if direction in ("pull", "both"):
        pulled, l_failed, _ = await _pull_from_chroma(client, col_id, col_path, max_items)

    return f"Synced: {pushed} pushed, {pulled} pulled, {p_failed + l_failed} failed"
