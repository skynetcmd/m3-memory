import sqlite3
import logging
import base64
import json
import uuid
from datetime import datetime, timezone
import memory_core
from memory_core import (
    _db, _conn, ARCHIVE_DB_PATH, DB_PATH, _cosine, _unpack,
    DEDUP_LIMIT, DEDUP_THRESHOLD, get_best_llm, memory_link_impl,
    _get_embed_client, ctx, _content_hash, _embed, _pack, LLM_TIMEOUT
)

logger = logging.getLogger("memory_maintenance")

def _archive_conn():
    conn = sqlite3.connect(ARCHIVE_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _transfer_to_archive(item_id, reason, db):
    now = datetime.now(timezone.utc).isoformat()
    row = db.execute("SELECT * FROM memory_items WHERE id = ?", (item_id,)).fetchone()
    if not row: return False
    adb = _archive_conn()
    try:
        adb.execute("INSERT OR REPLACE INTO archived_items (id, content, archive_reason, archived_at) VALUES (?,?,?,?)",
                    (row["id"], row["content"], reason, now))
        adb.commit()
        return True
    except Exception:
        adb.rollback()
        return False
    finally: adb.close()

def memory_dedup_impl(threshold=DEDUP_THRESHOLD, dry_run=True):
    with _db() as db:
        rows = db.execute(f"SELECT me.memory_id, me.embedding, mi.title FROM memory_embeddings me JOIN memory_items mi ON me.memory_id = mi.id WHERE mi.is_deleted = 0 ORDER BY mi.created_at DESC LIMIT {DEDUP_LIMIT}").fetchall()
    
    items = [(r["memory_id"], _unpack(r["embedding"]), r["title"]) for r in rows]
    duplicates = []
    seen = set()
    
    for i, (mid_a, vec_a, title_a) in enumerate(items):
        if mid_a in seen: continue
        for j in range(i + 1, len(items)):
            mid_b, vec_b, title_b = items[j]
            if mid_b in seen: continue
            if _cosine(vec_a, vec_b) >= threshold:
                duplicates.append((mid_a, mid_b, title_a, title_b))
                seen.add(mid_b)
    
    if not dry_run:
        with _db() as db:
            for _, mid_b, _, _ in duplicates:
                db.execute("UPDATE memory_items SET is_deleted = 1 WHERE id = ?", (mid_b,))
    
    return f"Found {len(duplicates)} duplicate groups."

def memory_feedback_impl(memory_id, feedback="useful"):
    fb = feedback.lower()
    with _db() as db:
        if fb == "useful":
            db.execute("UPDATE memory_items SET importance = MIN(1.0, importance + 0.1) WHERE id = ?", (memory_id,))
        elif fb == "wrong":
            db.execute("UPDATE memory_items SET is_deleted = 1 WHERE id = ?", (memory_id,))
    return f"Feedback '{fb}' applied to {memory_id}"

def _enforce_retention_policies(db):
    """Enforce per-agent memory limits and TTLs from agent_retention_policies table."""
    try:
        policies = db.execute("SELECT * FROM agent_retention_policies").fetchall()
    except Exception:
        return 0  # Table may not exist yet
    purged = 0
    for p in policies:
        agent_id = p["agent_id"]
        # TTL enforcement
        if p["ttl_days"] and p["ttl_days"] > 0:
            res = db.execute(
                "UPDATE memory_items SET is_deleted = 1 WHERE agent_id = ? AND is_deleted = 0 "
                "AND julianday('now') - julianday(created_at) > ?",
                (agent_id, p["ttl_days"])
            )
            purged += res.rowcount
        # Max count enforcement (keep newest, soft-delete oldest excess)
        if p["max_memories"] and p["max_memories"] > 0:
            excess = db.execute(
                "SELECT id FROM memory_items WHERE agent_id = ? AND is_deleted = 0 "
                "ORDER BY created_at DESC LIMIT -1 OFFSET ?",
                (agent_id, p["max_memories"])
            ).fetchall()
            for row in excess:
                if p["auto_archive"]:
                    _transfer_to_archive(row["id"], "retention_limit", db)
                db.execute("UPDATE memory_items SET is_deleted = 1 WHERE id = ?", (row["id"],))
                purged += 1
    return purged

def memory_maintenance_impl(decay=True, purge_expired=True, prune_orphan_embeddings=True):
    now = datetime.now(timezone.utc).isoformat()
    report = []
    with _db() as db:
        if decay:
            res = db.execute("UPDATE memory_items SET importance = MAX(0.0, importance * 0.995) WHERE is_deleted = 0 AND julianday('now') - julianday(created_at) > 7")
            report.append(f"Decayed {res.rowcount} items")
        if purge_expired:
            expired = db.execute("SELECT id FROM memory_items WHERE expires_at < ?", (now,)).fetchall()
            for row in expired: _transfer_to_archive(row[0], "expired", db)
            res = db.execute("DELETE FROM memory_items WHERE expires_at < ?", (now,))
            report.append(f"Purged {res.rowcount} expired")
        if prune_orphan_embeddings:
            res = db.execute("DELETE FROM memory_embeddings WHERE memory_id NOT IN (SELECT id FROM memory_items)")
            report.append(f"Pruned {res.rowcount} orphans")

        # Auto-archive low-importance memories older than 30 days
        archivable = db.execute(
            "SELECT id FROM memory_items WHERE is_deleted = 0 AND importance < 0.05 "
            "AND julianday('now') - julianday(created_at) > 30"
        ).fetchall()
        archived = 0
        for row in archivable:
            if _transfer_to_archive(row["id"], "low_importance", db):
                db.execute("UPDATE memory_items SET is_deleted = 1 WHERE id = ?", (row["id"],))
                archived += 1
        report.append(f"Archived {archived} low-importance items")

        # Enforce agent retention policies
        retention_purged = _enforce_retention_policies(db)
        if retention_purged:
            report.append(f"Retention policies: purged {retention_purged} items")

        # Refresh queue: count memories whose refresh_on has arrived, and emit
        # one push notification per distinct agent with newly-due memories.
        # - Maintenance never mutates refresh flags (that's memory_update's job).
        # - Dedup against existing unacked refresh_due notifications so repeated
        #   maintenance runs don't flood the channel with duplicates.
        try:
            refresh_due = db.execute(
                "SELECT COUNT(*) FROM memory_items "
                "WHERE is_deleted = 0 AND refresh_on IS NOT NULL AND refresh_on <= ?",
                (now,)
            ).fetchone()[0]
            if refresh_due:
                report.append(f"Refresh queue: {refresh_due} memor{'y' if refresh_due == 1 else 'ies'} due for review")

                # Fan-out notifications by agent_id. NULL/empty agent_ids are
                # grouped under a synthetic '(unassigned)' bucket and skipped —
                # notifications require a real agent_id.
                agent_rows = db.execute(
                    "SELECT agent_id, COUNT(*) as n, GROUP_CONCAT(id) as ids "
                    "FROM memory_items "
                    "WHERE is_deleted = 0 AND refresh_on IS NOT NULL AND refresh_on <= ? "
                    "  AND agent_id IS NOT NULL AND agent_id != '' "
                    "GROUP BY agent_id",
                    (now,)
                ).fetchall()

                notified = 0
                for ar in agent_rows:
                    aid = ar["agent_id"]
                    # Dedup: skip if this agent already has an unacked refresh_due notif
                    existing = db.execute(
                        "SELECT 1 FROM notifications "
                        "WHERE agent_id = ? AND kind = 'refresh_due' AND read_at IS NULL LIMIT 1",
                        (aid,)
                    ).fetchone()
                    if existing:
                        continue
                    sample = (ar["ids"] or "").split(",")[:3]
                    payload = json.dumps({"count": ar["n"], "sample_ids": sample})
                    db.execute(
                        "INSERT INTO notifications (agent_id, kind, payload_json, created_at) "
                        "VALUES (?, 'refresh_due', ?, ?)",
                        (aid, payload, now)
                    )
                    notified += 1
                if notified:
                    report.append(f"Refresh queue: notified {notified} agent(s)")
        except Exception as e:
            # refresh_on column may not exist on very old DBs that haven't run v014
            logger.debug(f"refresh queue check skipped: {e}")

        db.execute("ANALYZE")
        report.append("Statistics updated (ANALYZE)")

    # VACUUM must run outside any transaction
    try:
        vconn = sqlite3.connect(DB_PATH)
        vconn.execute("VACUUM")
        vconn.close()
        report.append("Space reclaimed (VACUUM)")
    except Exception as e:
        report.append(f"VACUUM skipped: {e}")

    return "Maintenance complete:\n" + "\n".join(report)

def gdpr_export_impl(user_id: str) -> str:
    """Export all memories for a data subject (GDPR Article 20 - Right to data portability)."""
    import json
    if not user_id or not user_id.strip():
        return "Error: user_id is required"
    with _db() as db:
        rows = db.execute(
            "SELECT id, type, title, content, metadata_json, agent_id, importance, created_at, updated_at "
            "FROM memory_items WHERE user_id = ? AND is_deleted = 0",
            (user_id,)
        ).fetchall()
        items = [dict(r) for r in rows]

        # Log the export request
        import uuid
        req_id = str(uuid.uuid4())
        try:
            db.execute(
                "INSERT INTO gdpr_requests (id, subject_id, request_type, status, items_affected, completed_at) "
                "VALUES (?, ?, 'export', 'completed', ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                (req_id, user_id, len(items))
            )
        except Exception:
            pass  # gdpr_requests table may not exist yet

    return json.dumps({"user_id": user_id, "request_id": req_id, "items_count": len(items), "items": items}, indent=2, default=str)

def gdpr_forget_impl(user_id: str) -> str:
    """Right to be forgotten (GDPR Article 17). Hard-deletes all data for a user_id."""
    import uuid
    if not user_id or not user_id.strip():
        return "Error: user_id is required"

    req_id = str(uuid.uuid4())
    total_deleted = 0

    with _db() as db:
        # Count items before deletion
        count_row = db.execute(
            "SELECT COUNT(*) as cnt FROM memory_items WHERE user_id = ?", (user_id,)
        ).fetchone()
        total_deleted = count_row["cnt"] if count_row else 0

        # Get all memory IDs for cascade deletion
        item_ids = [r["id"] for r in db.execute(
            "SELECT id FROM memory_items WHERE user_id = ?", (user_id,)
        ).fetchall()]

        if item_ids:
            placeholders = ",".join(["?"] * len(item_ids))
            # Delete embeddings
            db.execute(f"DELETE FROM memory_embeddings WHERE memory_id IN ({placeholders})", item_ids)
            # Delete relationships
            db.execute(f"DELETE FROM memory_relationships WHERE from_id IN ({placeholders}) OR to_id IN ({placeholders})", item_ids + item_ids)
            # Delete sync queue entries
            db.execute(f"DELETE FROM chroma_sync_queue WHERE memory_id IN ({placeholders})", item_ids)
            # Delete history
            db.execute(f"DELETE FROM memory_history WHERE memory_id IN ({placeholders})", item_ids)
            # Hard-delete the items themselves
            db.execute(f"DELETE FROM memory_items WHERE user_id = ?", (user_id,))

        # Log the forget request
        try:
            db.execute(
                "INSERT INTO gdpr_requests (id, subject_id, request_type, status, items_affected, completed_at) "
                "VALUES (?, ?, 'forget', 'completed', ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                (req_id, user_id, total_deleted)
            )
        except Exception:
            pass  # gdpr_requests table may not exist yet

    return f"GDPR forget completed: {total_deleted} items hard-deleted for user_id={user_id} (request: {req_id})"

def memory_set_retention_impl(agent_id: str, max_memories: int = 1000, ttl_days: int = 0, auto_archive: int = 1) -> str:
    """Set or update agent retention policy."""
    if not agent_id or not agent_id.strip():
        return "Error: agent_id is required"
    try:
        with _db() as db:
            db.execute(
                "INSERT INTO agent_retention_policies (agent_id, max_memories, ttl_days, auto_archive, updated_at) "
                "VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
                "ON CONFLICT(agent_id) DO UPDATE SET max_memories=excluded.max_memories, ttl_days=excluded.ttl_days, "
                "auto_archive=excluded.auto_archive, updated_at=excluded.updated_at",
                (agent_id, max_memories, ttl_days, auto_archive)
            )
        return f"Retention policy set for agent '{agent_id}': max={max_memories}, ttl={ttl_days}d, auto_archive={bool(auto_archive)}"
    except Exception as e:
        return f"Error setting retention policy: {e}"

def memory_export_impl(agent_filter="", type_filter="", since="", output_format="json"):
    """Export memories as portable JSON. Filter by agent, type, or date."""
    where = ["mi.is_deleted = 0"]
    params = []
    if agent_filter:
        where.append("mi.agent_id = ?")
        params.append(agent_filter)
    if type_filter:
        where.append("mi.type = ?")
        params.append(type_filter)
    if since:
        where.append("mi.created_at >= ?")
        params.append(since)
    
    where_sql = " AND ".join(where)
    
    with _db() as db:
        rows = db.execute(f"SELECT * FROM memory_items mi WHERE {where_sql}", params).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            mid = item["id"]
            # Fetch embeddings
            embs = db.execute("SELECT embedding, embed_model, dim, created_at, content_hash FROM memory_embeddings WHERE memory_id = ?", (mid,)).fetchall()
            item["embeddings"] = []
            for e in embs:
                edata = dict(e)
                if edata["embedding"]:
                    edata["embedding"] = base64.b64encode(edata["embedding"]).decode("utf-8")
                item["embeddings"].append(edata)
            
            # Fetch relationships
            rels = db.execute("SELECT to_id, relationship_type, created_at FROM memory_relationships WHERE from_id = ?", (mid,)).fetchall()
            item["relationships"] = [dict(r) for r in rels]
            items.append(item)
            
    return json.dumps({
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "item_count": len(items),
        "items": items
    }, indent=2, default=str)

def memory_import_impl(data: str):
    """Import memories from a JSON export. UPSERT semantics — safe to re-run."""
    try:
        payload = json.loads(data)
        items = payload.get("items", [])
    except Exception as e:
        return f"Error parsing import data: {e}"
    
    i_count, e_count, r_count = 0, 0, 0
    with _db() as db:
        for item in items:
            # 1. UPSERT memory_items
            fields = ["id", "type", "title", "content", "metadata_json", "agent_id", "model_id", "change_agent", "importance", "source", "origin_device", "user_id", "scope", "expires_at", "created_at", "updated_at", "valid_from", "valid_to", "content_hash", "is_deleted"]
            # Filter item to only include known fields
            clean_item = {k: item.get(k) for k in fields if k in item}
            placeholders = ", ".join(["?"] * len(clean_item))
            columns = ", ".join(clean_item.keys())
            update_stmt = ", ".join([f"{k}=excluded.{k}" for k in clean_item.keys() if k != "id"])
            
            sql = f"INSERT INTO memory_items ({columns}) VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {update_stmt}"
            db.execute(sql, list(clean_item.values()))
            i_count += 1
            
            # 2. Re-insert embeddings
            mid = clean_item["id"]
            for edata in item.get("embeddings", []):
                eblob = base64.b64decode(edata["embedding"]) if edata.get("embedding") else None
                db.execute(
                    "INSERT OR REPLACE INTO memory_embeddings (id, memory_id, embedding, embed_model, dim, created_at, content_hash) VALUES (?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), mid, eblob, edata.get("embed_model"), edata.get("dim"), edata.get("created_at"), edata.get("content_hash"))
                )
                e_count += 1
            
            # 3. Re-insert relationships
            for rdata in item.get("relationships", []):
                db.execute(
                    "INSERT OR REPLACE INTO memory_relationships (id, from_id, to_id, relationship_type, created_at) VALUES (?,?,?,?,?)",
                    (str(uuid.uuid4()), mid, rdata.get("to_id"), rdata.get("relationship_type"), rdata.get("created_at"))
                )
                r_count += 1
                
    return f"Imported {i_count} items, {e_count} embeddings, {r_count} relationships"

async def memory_consolidate_impl(type_filter="", agent_filter="", threshold=20):
    """Consolidate old memories of the same type into summaries using the local LLM."""
    # 1. Query groups exceeding threshold
    sql = "SELECT type, agent_id, COUNT(*) as cnt FROM memory_items WHERE is_deleted = 0"
    params = []
    if type_filter:
        sql += " AND type = ?"
        params.append(type_filter)
    if agent_filter:
        sql += " AND agent_id = ?"
        params.append(agent_filter)
    sql += " GROUP BY type, agent_id HAVING cnt > ?"
    params.append(threshold)
    
    with _db() as db:
        groups = db.execute(sql, params).fetchall()
        
    if not groups:
        return "No memory groups exceed consolidation threshold."
    
    token = ctx.get_secret("LM_API_TOKEN") or "lm-studio"
    client = _get_embed_client()
    result = await get_best_llm(client, token)
    if not result:
        return "Error: No local LLM available for consolidation."
    base_url, model = result
    
    results = []
    for g in groups:
        g_type, g_agent = g["type"], g["agent_id"]
        n_to_consolidate = g["cnt"] - threshold
        
        # 2. Fetch oldest N items
        with _db() as db:
            rows = db.execute(
                "SELECT id, title, content FROM memory_items WHERE type = ? AND agent_id = ? AND is_deleted = 0 ORDER BY created_at ASC LIMIT ?",
                (g_type, g_agent, n_to_consolidate)
            ).fetchall()
        
        if not rows: continue
        
        # 3. Concatenate content
        items_text = "\n".join(f"- {r['title'] or '(untitled)'}: {r['content']}" for r in rows)
        
        # 4. Call LLM
        prompt = f"Consolidate these {len(rows)} memory items into a single comprehensive summary. Preserve all facts, decisions, and key details.\n\n{items_text}"
        try:
            resp = await client.post(
                f"{base_url}/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3
                },
                headers={"Authorization": f"Bearer {token}"},
                timeout=memory_core.LLM_TIMEOUT
            )
            resp.raise_for_status()
            data = resp.json()
            if "choices" not in data or not data["choices"]:
                results.append(f"Error consolidating {g_type}/{g_agent}: LLM returned no choices")
                continue
            summary_text = data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            results.append(f"Error consolidating {g_type}/{g_agent}: {type(e).__name__}: {e}")
            continue
            
        # 5. Store summary
        summary_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        
        # Embed the summary so it's searchable
        s_vec, s_model = await _embed(summary_text)
        
        with _db() as db:
            db.execute(
                "INSERT INTO memory_items (id, type, title, content, agent_id, created_at, content_hash) VALUES (?, 'summary', ?, ?, ?, ?, ?)",
                (summary_id, f"Consolidated {g_type} memories for {g_agent}", summary_text, g_agent, now, _content_hash(summary_text))
            )
            
            if s_vec:
                db.execute(
                    "INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim, created_at, content_hash) VALUES (?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), summary_id, _pack(s_vec), s_model, len(s_vec), now, _content_hash(summary_text))
                )
            
            # 6. Link to sources and 7. Soft-delete
            for r in rows:
                memory_link_impl(summary_id, r["id"], "consolidates", db=db)
                db.execute("UPDATE memory_items SET is_deleted = 1 WHERE id = ?", (r["id"],))
        
        results.append(f"Consolidated {len(rows)} {g_type} items into summary {summary_id}")
        
    return "\n".join(results)
