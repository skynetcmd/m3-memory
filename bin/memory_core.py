from __future__ import annotations

import sqlite3
import os
import logging
import sys
import uuid
import json
import hashlib
import threading
import platform
from typing import Any
from datetime import datetime, timezone
from contextlib import contextmanager
import asyncio

import re
from m3_sdk import M3Context
from llm_failover import get_best_embed, get_best_llm

async def conversation_summarize_impl(conversation_id: str, threshold: int = 20) -> str:
    """Summarizes a conversation into key points using the local LLM."""
    # 1. Fetch all messages for the conversation
    with _db() as db:
        rows = db.execute(
            """SELECT mi.title AS role, mi.content
               FROM memory_relationships mr
               JOIN memory_items mi ON mr.to_id = mi.id
               WHERE mr.from_id = ? AND mr.relationship_type = 'message' AND mi.is_deleted = 0
               ORDER BY mi.created_at ASC""",
            (conversation_id,)
        ).fetchall()

    # 2. Threshold check
    if len(rows) < threshold:
        return f"Conversation too short to summarize ({len(rows)} messages, threshold={threshold})"

    # 3. Concatenate messages
    messages_text = "\n".join(f"{row['role']}: {row['content']}" for row in rows)

    # 4. Call the local LLM via failover logic
    token = ctx.get_secret("LM_API_TOKEN") or "lm-studio"
    client = _get_embed_client()
    result = await get_best_llm(client, token)
    if not result:
        return "Error: No local LLM available for summarization."

    base_url, model = result
    prompt = f"Summarize this conversation into 3-5 key points. Preserve facts, decisions, and action items.\n\n{messages_text}"

    try:
        resp = await client.post(
            f"{base_url}/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=120.0
        )
        resp.raise_for_status()
        summary_text = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"Error during LLM summarization: {e}"

    # 5. Store the summary as a new memory item
    summary_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _db() as db:
        db.execute(
            "INSERT INTO memory_items (id, type, title, content, created_at, content_hash) VALUES (?, 'summary', ?, ?, ?, ?)",
            (summary_id, f"Summary of {conversation_id[:8]}", summary_text, now, _content_hash(summary_text))
        )
    
    # 6. Link it to the conversation
    memory_link_impl(summary_id, conversation_id, "references")
    
    _record_history(summary_id, "create", None, summary_text, "content", "system")
    return summary_text
from embedding_utils import (
    pack as _pack, unpack as _unpack,
    batch_cosine as _batch_cosine,
    infer_change_agent as _infer_change_agent_util,
)

logger = logging.getLogger("memory_core")
ctx = M3Context()

# ── Constants ─────────────────────────────────────────────────────────────────
BASE_DIR            = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH             = os.path.join(BASE_DIR, "memory", "agent_memory.db")
ARCHIVE_DB_PATH     = os.path.join(BASE_DIR, "memory", "agent_memory_archive.db")
EMBED_MODEL         = os.environ.get("EMBED_MODEL", "qwen3-embedding")
EMBED_DIM           = int(os.environ.get("EMBED_DIM", "1024"))
EMBED_TIMEOUT_READ  = 30.0
ORIGIN_DEVICE       = os.environ.get("ORIGIN_DEVICE", platform.node())

# Task 1: Configurable Dedup/Search Limits (#46)
DEDUP_LIMIT            = int(os.environ.get("DEDUP_LIMIT", "1000"))
DEDUP_THRESHOLD        = float(os.environ.get("DEDUP_THRESHOLD", "0.92"))
CONTRADICTION_THRESHOLD = float(os.environ.get("CONTRADICTION_THRESHOLD", "0.85"))
SEARCH_ROW_CAP         = int(os.environ.get("SEARCH_ROW_CAP", "500"))

VALID_CHANGE_AGENTS = {"claude", "gemini", "aider", "openclaw", "deepseek", "grok", "manual", "system", "unknown", "legacy"}

_FTS_OPERATORS = re.compile(r'\b(OR|AND|NOT|NEAR)\b|[*()\[\]{}]')
def _sanitize_fts(query: str, max_len: int = 500) -> str:
    """Strip FTS5 operators from user input to prevent query injection."""
    if len(query) > max_len:
        query = query[:max_len]
    return _FTS_OPERATORS.sub(' ', query).strip()

_POISON_PATTERNS = [
    re.compile(r'<script\b', re.I),
    re.compile(r'(?:DROP|DELETE|ALTER)\s+TABLE', re.I),
    re.compile(r'__import__|exec\s*\(|eval\s*\(', re.I),
    re.compile(r'(?:ignore|disregard)\s+(?:all\s+)?(?:previous|prior)\s+instructions', re.I),
]

def _check_content_safety(content: str) -> str | None:
    """Returns error message if content appears malicious, None if safe."""
    if not content:
        return None
    for pattern in _POISON_PATTERNS:
        if pattern.search(content):
            return f"Error: content rejected — matches safety pattern: {pattern.pattern[:50]}"
    return None

DEFAULT_CHANGE_AGENT = "unknown"

CHROMA_BASE_URL     = os.environ.get("CHROMA_BASE_URL")
CHROMA_COLLECTION   = "agent_memory"
CHROMA_COLLECTIONS  = ["agent_memory", "home_memory", "user_facts"]
CHROMA_V2_PREFIX    = "/api/v2/tenants/default_tenant/databases/default_database/collections"
CHROMA_CONNECT_T    = 3.0
CHROMA_READ_T       = 10.0
CHROMA_PULL_PAGE_SIZE = 100
CHROMA_CONTENT_MAX    = 10_000

_local = threading.local()
_init_lock = threading.RLock()
_initialized = False
_EMBED_SEM = asyncio.Semaphore(4)
_EMBED_DIM_VALIDATED = False

_COST_COUNTERS = {"embed_calls": 0, "embed_tokens_est": 0, "search_calls": 0, "write_calls": 0}
_CLASSIFY_CACHE = {}

async def _auto_classify(content: str, title: str) -> str:
    """Uses the local LLM to classify a memory into a valid type."""
    c_hash = _content_hash(content + title)
    if c_hash in _CLASSIFY_CACHE:
        return _CLASSIFY_CACHE[c_hash]

    # Use a localized set to avoid circular import with memory_bridge
    valid_types = {
        "note", "fact", "decision", "preference", "conversation", "message",
        "task", "code", "config", "observation", "plan", "summary", "snippet",
        "reference", "log", "home", "user_fact", "scratchpad",
    }
    
    token = ctx.get_secret("LM_API_TOKEN") or "lm-studio"
    client = _get_embed_client()
    result = await get_best_llm(client, token)
    if not result:
        return "note"
    
    base_url, model = result
    prompt = (
        f"Classify this memory into exactly one type. Valid types: {', '.join(sorted(valid_types))}\n"
        f"Title: {title}\n"
        f"Content: {content[:500]}\n"
        f"Reply with ONLY the type name, nothing else."
    )
    
    try:
        resp = await client.post(
            f"{base_url}/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0
        )
        resp.raise_for_status()
        m_type = resp.json()["choices"][0]["message"]["content"].strip().lower()
        if m_type in valid_types:
            _CLASSIFY_CACHE[c_hash] = m_type
            return m_type
    except Exception as e:
        logger.debug(f"Auto-classification failed: {e}")
    
    return "note"

def _track_cost(operation: str, tokens_est: int = 0):
    _COST_COUNTERS[operation] = _COST_COUNTERS.get(operation, 0) + 1
    if tokens_est:
        _COST_COUNTERS["embed_tokens_est"] += tokens_est

def _ensure_sync_tables() -> None:
    import subprocess
    try:
        migration_script = os.path.join(BASE_DIR, "bin", "migrate_memory.py")
        subprocess.run([sys.executable, migration_script], check=True, timeout=30)
    except Exception as e:
        logger.exception(f"_ensure_sync_tables failed: {e}")

def _backfill_change_agent() -> None:
    try:
        with _db() as db:
            rows = db.execute("SELECT id, agent_id, model_id FROM memory_items WHERE change_agent IS NULL").fetchall()
            for row in rows:
                agent = _infer_change_agent_util(row["agent_id"] or "", row["model_id"] or "", default="legacy")
                db.execute("UPDATE memory_items SET change_agent = ? WHERE id = ?", (agent, row["id"]))
    except Exception as e:
        logger.warning(f"Backfill failed: {e}")

def _lazy_init() -> None:
    global _initialized
    with _init_lock:
        if not _initialized:
            _initialized = True  # Set before init so re-entrant calls from _backfill_change_agent are no-ops
            _ensure_sync_tables()
            _backfill_change_agent()

@contextmanager
def _db():
    _lazy_init()
    with ctx.get_sqlite_conn() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

@contextmanager
def _conn():
    """Legacy alias for _db context manager (C7)."""
    with _db() as db:
        yield db

def _record_history(memory_id: str, event: str, prev_value: str = None, new_value: str = None, field: str = "content", actor_id: str = ""):
    """Records a change event in the memory_history audit trail."""
    try:
        with _db() as db:
            db.execute(
                "INSERT INTO memory_history (id, memory_id, event, prev_value, new_value, field, actor_id) VALUES (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), memory_id, event, prev_value, new_value, field, actor_id)
            )
    except Exception as e:
        logger.debug(f"History recording failed: {e}")

def memory_history_impl(memory_id: str, limit: int = 20) -> str:
    """Returns the change history for a memory item."""
    with _db() as db:
        rows = db.execute(
            "SELECT event, field, prev_value, new_value, actor_id, created_at FROM memory_history WHERE memory_id = ? ORDER BY created_at DESC LIMIT ?",
            (memory_id, limit)
        ).fetchall()
    if not rows:
        return f"No history found for {memory_id}"
    lines = [f"History for {memory_id} ({len(rows)} events):"]
    for r in rows:
        prev = (r["prev_value"] or "")[:80]
        new = (r["new_value"] or "")[:80]
        lines.append(f"  [{r['created_at']}] {r['event']} ({r['field']}) by {r['actor_id'] or 'unknown'}: {prev!r} -> {new!r}")
    return "\n".join(lines)

def _content_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()

# Shared Async Client
import httpx as _httpx
_shared_embed_client: _httpx.AsyncClient | None = None

def _get_embed_client() -> _httpx.AsyncClient:
    return ctx.get_async_client()

async def _embed(text: str) -> tuple[list[float] | None, str]:
    global _EMBED_DIM_VALIDATED
    c_hash = _content_hash(text)
    try:
        with _db() as db:
            cached = db.execute("SELECT embedding, embed_model FROM memory_embeddings WHERE content_hash = ? AND embed_model = ? LIMIT 1", (c_hash, EMBED_MODEL)).fetchone()
            if cached: return _unpack(cached["embedding"]), cached["embed_model"]
    except Exception as e:
        logger.debug(f"Embedding cache lookup failed: {e}")

    # Acquire semaphore with timeout to prevent deadlock under load
    try:
        await asyncio.wait_for(_EMBED_SEM.acquire(), timeout=30.0)
    except asyncio.TimeoutError:
        logger.error("Embedding semaphore acquire timed out after 30s")
        return None, EMBED_MODEL

    try:
        _track_cost("embed_calls", len(text.split()) * 2)
        token = ctx.get_secret("LM_API_TOKEN") or "lm-studio"
        client = _get_embed_client()
        result = await get_best_embed(client, token)
        if not result: return None, EMBED_MODEL
        base_url, model = result

        last_exc = None
        for attempt in range(3):
            try:
                resp = await client.post(
                    f"{base_url}/embeddings",
                    json={"model": model, "input": text},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=_httpx.Timeout(CHROMA_CONNECT_T, read=EMBED_TIMEOUT_READ)
                )
                resp.raise_for_status()
                emb = resp.json()["data"][0]["embedding"]

                if not _EMBED_DIM_VALIDATED:
                    if len(emb) != EMBED_DIM:
                        logger.error(f"Embedding dimension mismatch: got {len(emb)}, expected EMBED_DIM={EMBED_DIM}. Update EMBED_DIM env var.")
                    _EMBED_DIM_VALIDATED = True

                return emb, model
            except Exception as e:
                last_exc = e
                if attempt < 2:
                    wait = 2 * (2 ** attempt)
                    logger.warning(f"Embedding attempt {attempt + 1} failed: {e}. Retrying in {wait}s...")
                    await asyncio.sleep(wait)

        logger.error(f"Embedding generation failed after 3 attempts: {last_exc}")
        return None, model
    finally:
        _EMBED_SEM.release()

def _queue_chroma(memory_id: str, operation: str) -> None:
    try:
        with _db() as db:
            db.execute("INSERT INTO chroma_sync_queue (memory_id, operation) VALUES (?,?)", (memory_id, operation))
    except Exception as e:
        logger.debug(f"ChromaDB queue insert failed: {e}")

async def _check_contradictions(item_id: str, content: str, title: str, vec: list[float], type_: str, agent_id: str) -> tuple[list[str], list[tuple[str, float]]]:
    """
    Detects contradictions with existing memories of the same type.
    Returns (superseded_ids, related_candidates) where related_candidates
    are (id, score) pairs with cosine > 0.7 that are NOT contradictions.
    """
    superseded = []
    related = []
    try:
        with _db() as db:
            # Find top-5 similar memories of the same type
            where = "mi.is_deleted = 0 AND mi.type = ? AND mi.id != ?"
            params = [type_, item_id]
            if agent_id:
                where += " AND mi.agent_id = ?"
                params.append(agent_id)
            rows = db.execute(
                f"SELECT mi.id, mi.title, mi.content, me.embedding FROM memory_items mi "
                f"JOIN memory_embeddings me ON mi.id = me.memory_id WHERE {where} LIMIT 200",
                params
            ).fetchall()

        if not rows:
            return superseded, related

        embeddings = [_unpack(r["embedding"]) for r in rows]
        scores = _batch_cosine(vec, embeddings)

        for i, row in enumerate(rows):
            score = scores[i]
            if score > CONTRADICTION_THRESHOLD:
                # High similarity — check if it's a contradiction (same topic, different content)
                old_title = (row["title"] or "").strip().lower()
                new_title = (title or "").strip().lower()
                titles_match = old_title == new_title or (old_title and new_title and (
                    old_title in new_title or new_title in old_title
                ))
                content_differs = (row["content"] or "").strip() != (content or "").strip()

                if titles_match and content_differs:
                    # Contradiction detected — supersede old memory
                    with _db() as db:
                        db.execute("UPDATE memory_items SET is_deleted = 1, updated_at = ? WHERE id = ?",
                                   (datetime.now(timezone.utc).isoformat(), row["id"]))
                        db.execute(
                            "INSERT INTO memory_relationships (id, from_id, to_id, relationship_type, created_at) VALUES (?,?,?,?,?)",
                            (str(uuid.uuid4()), item_id, row["id"], "supersedes", datetime.now(timezone.utc).isoformat())
                        )
                    _record_history(row["id"], "supersede", row["content"], item_id, "content")
                    superseded.append(row["id"])
                    logger.info(f"Memory {item_id} supersedes {row['id']} (contradiction detected)")
            elif score > 0.7:
                related.append((row["id"], score))
    except Exception as e:
        logger.debug(f"Contradiction check failed: {e}")
    return superseded, related

async def _query_chroma(query_vec: list[float], k: int = 5) -> list[dict]:
    """Queries the remote ChromaDB instance for federated results."""
    if not CHROMA_BASE_URL or not CHROMA_BASE_URL.startswith("http"):
        return []
    try:
        client = _get_embed_client()
        # 1. Resolve collection ID
        resp = await client.get(f"{CHROMA_BASE_URL}{CHROMA_V2_PREFIX}/{CHROMA_COLLECTION}", timeout=CHROMA_CONNECT_T)
        resp.raise_for_status()
        col_data = resp.json()
        col_id = col_data.get("id")
        if not col_id:
            logger.warning("ChromaDB collection response missing 'id' field")
            return []

        # 2. Perform query
        col_path = f"{CHROMA_BASE_URL}{CHROMA_V2_PREFIX}/{col_id}"
        query_resp = await client.post(f"{col_path}/query", json={
            "query_embeddings": [query_vec],
            "n_results": k,
            "include": ["documents", "metadatas", "distances"]
        }, timeout=CHROMA_READ_T)
        query_resp.raise_for_status()
        
        data = query_resp.json()
        results = []
        if data["ids"] and data["ids"][0]:
            for i in range(len(data["ids"][0])):
                # Chroma distance is often squared L2, but we'll treat it as a score component
                score = 1.0 - (data["distances"][0][i] / 2.0) if data["distances"] else 0.5
                results.append({
                    "id": data["ids"][0][i],
                    "content": data["documents"][0][i],
                    "title": data["metadatas"][0][i].get("title", ""),
                    "type": data["metadatas"][0][i].get("type", "federated"),
                    "score": score
                })
        return results
    except Exception as e:
        logger.debug(f"ChromaDB federated query failed: {e}")
        return []

async def memory_search_impl(query, k=8, type_filter="", agent_filter="", search_mode="hybrid", include_scratchpad=False, user_id="", scope="", as_of="", explain=False, _depth=0):
    try:
        k = int(k)
    except (TypeError, ValueError):
        k = 8
    _track_cost("search_calls")
    if _depth > 1:
        return "Search failed: FTS and semantic both unavailable."
    
    q_vec, _ = await _embed(query)
    if not q_vec: return "Embedding failed"
    
    # 1. Base filtering (H12)
    where_clauses = ["mi.is_deleted = 0"]
    params = []
    
    if type_filter:
        is_exact = (type_filter.startswith('"') and type_filter.endswith('"')) or (type_filter.startswith("'") and type_filter.endswith("'"))
        actual_type = type_filter[1:-1] if is_exact else type_filter
        if is_exact:
            where_clauses.append("mi.type = ?")
        else:
            where_clauses.append("mi.type LIKE ?")
        params.append(actual_type)
        
    if agent_filter:
        is_exact = (agent_filter.startswith('"') and agent_filter.endswith('"')) or (agent_filter.startswith("'") and agent_filter.endswith("'"))
        actual_agent = agent_filter[1:-1] if is_exact else agent_filter
        if is_exact:
            where_clauses.append("mi.agent_id = ?")
        else:
            where_clauses.append("LOWER(mi.agent_id) = LOWER(?)")
        params.append(actual_agent)

    if user_id:
        where_clauses.append("mi.user_id = ?")
        params.append(user_id)
    if scope:
        where_clauses.append("mi.scope = ?")
        params.append(scope)

    if as_of:
        where_clauses.append("(mi.valid_from = '' OR mi.valid_from <= ?)")
        where_clauses.append("(mi.valid_to = '' OR mi.valid_to > ?)")
        params.extend([as_of, as_of])

    where_sql = " AND ".join(where_clauses)
    
    # 2. Hybrid search with FTS5 ranking (Efficiency Suggestion #2)
    # We join with the FTS table to get BM25 scores for keyword relevance
    sql = f"""
        SELECT mi.id, mi.content, mi.title, mi.type, mi.importance, me.embedding,
               bm25(memory_items_fts) as bm25_score
        FROM memory_items mi 
        JOIN memory_embeddings me ON mi.id = me.memory_id
        JOIN memory_items_fts fts ON mi.rowid = fts.rowid
        WHERE {where_sql} AND memory_items_fts MATCH ?
        ORDER BY bm25_score ASC
        LIMIT 1000
    """
    
    # Fallback to standard if no keywords match or search_mode is semantic-only
    try:
        with _db() as db:
            if search_mode == "semantic":
                # Just use base query without FTS join for pure vector search
                sql = f"""
                    SELECT mi.id, mi.content, mi.title, mi.type, mi.importance, me.embedding, 0.0 as bm25_score
                    FROM memory_items mi 
                    JOIN memory_embeddings me ON mi.id = me.memory_id
                    WHERE {where_sql}
                    LIMIT 1000
                """
                rows = db.execute(sql, params).fetchall()
            else:
                # Optimized FTS5 Hybrid Search
                # Handle exact quoted search or FTS prefix matching for better keyword tolerance
                is_exact_query = (query.startswith('"') and query.endswith('"')) or (query.startswith("'") and query.endswith("'"))
                clean_query = query[1:-1] if is_exact_query else query
                
                if is_exact_query:
                    fts_query = f'"{clean_query}"'
                else:
                    clean_query = _sanitize_fts(clean_query)
                    if not clean_query:
                        return await memory_search_impl(query, k, type_filter, agent_filter, "semantic", include_scratchpad, user_id, scope, as_of=as_of, explain=explain, _depth=_depth + 1)
                    fts_query = f"{clean_query}*" if " " not in clean_query and clean_query.isalnum() else clean_query
                
                rows = db.execute(sql, (*params, fts_query)).fetchall()
                if not rows:
                    # Fallback to partial semantic if no FTS matches
                    return await memory_search_impl(query, k, type_filter, agent_filter, "semantic", include_scratchpad, user_id, scope, as_of=as_of, explain=explain, _depth=_depth + 1)
    except sqlite3.OperationalError:
        # FTS query might fail on complex syntax, fallback to semantic
        return await memory_search_impl(query, k, type_filter, agent_filter, "semantic", include_scratchpad, user_id, scope, as_of=as_of, explain=explain, _depth=_depth + 1)

    # 3. Calculate Vector similarity and combine scores
    scored = []
    # Cap to SEARCH_ROW_CAP rows for cosine computation to bound memory usage
    if len(rows) > SEARCH_ROW_CAP:
        rows = rows[:SEARCH_ROW_CAP]
    page_matrix = [_unpack(r["embedding"]) for r in rows]
    page_scores = _batch_cosine(q_vec, page_matrix)
    
    for i, row in enumerate(rows):
        item = dict(row)
        del item["embedding"]
        # Normalize BM25 (lower is better in SQLite) and combine with cosine
        # Final Score = VectorScore + (Importance * Weight)
        vector_score = page_scores[i]
        bm25_norm = (1.0 / (1.0 + abs(row["bm25_score"])))
        final_score = vector_score * 0.7 + bm25_norm * 0.3
        
        # Store breakdown for explain mode
        if explain:
            item["_explanation"] = {
                "vector": vector_score,
                "bm25": bm25_norm,
                "importance": row["importance"],
                "raw_hybrid": final_score
            }
        scored.append((final_score, item))

    # MMR re-ranking for search result diversity
    _MMR_LAMBDA = 0.7
    pre_ranked = sorted(scored, key=lambda x: x[0], reverse=True)[:k * 3]
    if len(pre_ranked) > k and len(page_matrix) > 0:
        _emb_lookup = {}
        for i in range(len(rows)):
            _emb_lookup[rows[i]["id"]] = page_matrix[i]
        selected = [pre_ranked[0]]
        candidates = list(pre_ranked[1:])
        while candidates and len(selected) < k:
            best_idx, best_mmr = 0, -float('inf')
            for ci, (c_score, c_item) in enumerate(candidates):
                c_vec = _emb_lookup.get(c_item["id"])
                if c_vec is None:
                    best_idx = ci
                    break
                
                # MMR Penalty calculation
                similarities = [_batch_cosine(c_vec, [_emb_lookup[s[1]["id"]]])[0]
                                for s in selected if s[1]["id"] in _emb_lookup]
                max_sim = max(similarities, default=0.0)
                
                mmr = _MMR_LAMBDA * c_score - (1 - _MMR_LAMBDA) * max_sim
                
                if mmr > best_mmr:
                    best_mmr = mmr
                    best_idx = ci
                    
                if explain and "mmr_penalty" not in c_item.get("_explanation", {}):
                    # We only record the penalty for the final selected rank in a real implementation, 
                    # but for this logic we'll just store the last max_sim we saw for each candidate.
                    if "_explanation" not in c_item: c_item["_explanation"] = {}
                    c_item["_explanation"]["max_sim_to_selected"] = max_sim
                    c_item["_explanation"]["mmr_penalty"] = (1 - _MMR_LAMBDA) * max_sim

            selected.append(candidates.pop(best_idx))
        ranked = selected
    else:
        ranked = pre_ranked
    
    # 4. Federated Fallback (Properly using ChromaDB as L3)
    if len(ranked) < 3 and not type_filter:
        fed_results = await _query_chroma(q_vec, k=3)
        for fr in fed_results:
            # Avoid duplicating items that might already be in local SQLite
            if not any(r[1]["id"] == fr["id"] for r in ranked):
                if explain:
                    fr["_explanation"] = {"source": "federated_chroma"}
                ranked.append((fr["score"], fr))

    # 5. Bulk access tracking update
    if ranked:
        # Filter for items that actually came from SQLite (have bm25_score)
        ids = [item[1]["id"] for item in ranked if "bm25_score" in item[1]]
        if ids:
            try:
                with _db() as db:
                    placeholders = ",".join(["?"] * len(ids))
                    db.execute(f"UPDATE memory_items SET last_accessed_at = ?, access_count = access_count + 1 WHERE id IN ({placeholders})",
                              (datetime.now(timezone.utc).isoformat(), *ids))
            except Exception as e:
                logger.debug(f"Search result timestamp update failed: {e}")

    if not ranked:
        return "No results found."
    lines = [f"Top {len(ranked)} results:"]
    for rank, (score, item) in enumerate(ranked, 1):
        content = item.get("content") or ""
        lines.append("-" * 40)
        lines.append(f"{rank}. [{item['id']}] score={score:.4f}  type: {item.get('type', 'unknown')}  title: {item.get('title','')}")
        
        if explain and "_explanation" in item:
            exp = item["_explanation"]
            if "raw_hybrid" in exp:
                lines.append(f"   Breakdown: vector={exp['vector']:.4f} (weight 0.7) + bm25={exp['bm25']:.4f} (weight 0.3) -> raw={exp['raw_hybrid']:.4f}")
                if "mmr_penalty" in exp:
                    lines.append(f"   MMR penalty: -{exp['mmr_penalty']:.4f} (max_sim_to_selected={exp['max_sim_to_selected']:.4f})")
                lines.append(f"   Importance: {exp['importance']:.4f}")
            else:
                lines.append(f"   Source: {exp.get('source', 'unknown')}")

        lines.append(f"Content:\n{content}\n")
    lines.append("-" * 40)
    return "\n".join(lines)

async def memory_suggest_impl(query: str, k: int = 5) -> str:
    """Returns which memories would be retrieved for a query and explains why."""
    return await memory_search_impl(query, k=k, explain=True)

def memory_get_impl(id):
    with _db() as db:
        row = db.execute("SELECT * FROM memory_items WHERE id = ?", (id,)).fetchone()
        if not row:
            # Fall back to chroma_mirror for items pulled from remote
            mirror = db.execute("SELECT * FROM chroma_mirror WHERE id = ?", (id,)).fetchone()
            if mirror:
                return json.dumps(dict(mirror), indent=2, default=str)
            return "Error: not found"
    return json.dumps(dict(row), indent=2, default=str)

def memory_verify_impl(memory_id: str) -> str:
    """Verify content integrity by comparing stored hash with computed hash."""
    with _db() as db:
        row = db.execute("SELECT content, content_hash FROM memory_items WHERE id = ?", (memory_id,)).fetchone()
        if not row:
            return f"Error: memory {memory_id} not found"
        stored_hash = row["content_hash"] or ""
        computed_hash = hashlib.sha256((row["content"] or "").encode("utf-8")).hexdigest()
        if not stored_hash:
            return f"Warning: no content hash stored for {memory_id}. Computed: {computed_hash}"
        if stored_hash == computed_hash:
            return f"Integrity OK: {memory_id} (hash: {computed_hash[:16]}...)"
        return f"INTEGRITY VIOLATION: {memory_id} — stored hash {stored_hash[:16]}... != computed {computed_hash[:16]}..."

def memory_cost_report_impl() -> str:
    """Returns current session cost/usage counters."""
    lines = ["Memory Operation Costs (this session):"]
    for key, val in sorted(_COST_COUNTERS.items()):
        lines.append(f"  {key}: {val}")
    return "\n".join(lines)

async def memory_update_impl(id, content="", title="", metadata="", importance=-1.0, reembed=False):
    if isinstance(metadata, dict):
        metadata = json.dumps(metadata)
    elif not isinstance(metadata, str):
        metadata = ""
    now = datetime.now(timezone.utc).isoformat()
    try:
        importance = float(importance)
    except (TypeError, ValueError):
        importance = -1.0
    with _db() as db:
        # Read old values for audit trail
        old = db.execute("SELECT content, title FROM memory_items WHERE id = ?", (id,)).fetchone()
        if content:
            _record_history(id, "update", old["content"] if old else None, content, "content")
            db.execute("UPDATE memory_items SET content = ? WHERE id = ?", (content, id))
        if title:
            _record_history(id, "update", old["title"] if old else None, title, "title")
            db.execute("UPDATE memory_items SET title = ? WHERE id = ?", (title, id))
        if importance >= 0: db.execute("UPDATE memory_items SET importance = ? WHERE id = ?", (importance, id))
        if metadata: db.execute("UPDATE memory_items SET metadata_json = ? WHERE id = ?", (metadata, id))
        db.execute("UPDATE memory_items SET updated_at = ? WHERE id = ?", (now, id))
    if reembed and content:
        vec, m = await _embed(content)
        if vec:
            with _db() as db:
                db.execute("UPDATE memory_embeddings SET embedding = ?, embed_model = ? WHERE memory_id = ?", (_pack(vec), m, id))
    return f"Updated: {id}"

def _cosine(v1: list[float], v2: list[float]) -> float:
    """Fallback cosine similarity if numpy is unavailable."""
    from embedding_utils import cosine
    return cosine(v1, v2)

def memory_delete_impl(id, hard=False):
    """Deletes a MemoryItem (soft or hard). Implements cascade for hard delete (C5)."""
    with _db() as db:
        row = db.execute("SELECT id, content FROM memory_items WHERE id = ?", (id,)).fetchone()
        if not row:
            return f"Error: item {id} not found"
        _record_history(id, "delete", row["content"], None, "content")
        if hard:
            db.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (id,))
            db.execute("DELETE FROM memory_relationships WHERE from_id = ? OR to_id = ?", (id, id))
            db.execute("DELETE FROM chroma_sync_queue WHERE memory_id = ?", (id,))
            db.execute("DELETE FROM memory_items WHERE id = ?", (id,))
        else:
            db.execute("UPDATE memory_items SET is_deleted = 1, updated_at = ? WHERE id = ?",
                       (datetime.now(timezone.utc).isoformat(), id))
    return f"{'Hard' if hard else 'Soft'}-deleted: {id}"

VALID_RELATIONSHIP_TYPES = {"related", "supports", "contradicts", "extends", "supersedes", "references", "message", "consolidates", "handoff"}

def memory_link_impl(from_id: str, to_id: str, relationship_type: str = "related") -> str:
    """Creates a directional link between two memory items."""
    if relationship_type not in VALID_RELATIONSHIP_TYPES:
        return f"Error: invalid relationship type '{relationship_type}'. Valid: {', '.join(sorted(VALID_RELATIONSHIP_TYPES))}"
    with _db() as db:
        # Verify both items exist
        for mid in (from_id, to_id):
            if not db.execute("SELECT id FROM memory_items WHERE id = ?", (mid,)).fetchone():
                return f"Error: memory {mid} not found"
        # Check for duplicate link
        existing = db.execute(
            "SELECT id FROM memory_relationships WHERE from_id = ? AND to_id = ? AND relationship_type = ?",
            (from_id, to_id, relationship_type)
        ).fetchone()
        if existing:
            return f"Link already exists: {existing['id']}"
        rid = str(uuid.uuid4())
        db.execute(
            "INSERT INTO memory_relationships (id, from_id, to_id, relationship_type, created_at) VALUES (?,?,?,?,?)",
            (rid, from_id, to_id, relationship_type, datetime.now(timezone.utc).isoformat())
        )
    return f"Linked: {from_id} --[{relationship_type}]--> {to_id} (id: {rid})"

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

def memory_handoff_impl(from_agent: str, to_agent: str, task: str,
                        context_ids: list, note: str = "") -> str:
    """Creates a handoff memory for inter-agent task transfer."""
    # 1. Generate new UUID
    new_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # 2. Insert handoff memory directly via raw SQL
    with _db() as db:
        metadata_json = json.dumps({"from_agent": from_agent, "note": note})
        db.execute(
            "INSERT INTO memory_items (id, type, title, content, agent_id, scope, metadata_json, created_at, updated_at, is_deleted) "
            "VALUES (?, 'handoff', ?, ?, ?, 'agent', ?, ?, ?, 0)",
            (new_id, f"Handoff from {from_agent}", task, to_agent, metadata_json, now, now)
        )

    # 3. Link context items (each opens its own _db() context)
    for ctx_id in context_ids:
        try:
            memory_link_impl(new_id, ctx_id, "handoff")
        except Exception as e:
            logger.debug(f"Failed to link context {ctx_id}: {e}")

    # 4. Record history
    _record_history(new_id, "handoff_create", None, task, "content", from_agent)

    # 5. Return status
    return f"Handoff created: {new_id} ({from_agent} -> {to_agent}, {len(context_ids)} context links)"

def memory_inbox_impl(agent_id: str, unread_only: bool = True, limit: int = 20) -> str:
    """Retrieves handoff messages for an agent, optionally filtered to unread."""
    # Build WHERE clause dynamically
    where_clause = "WHERE agent_id = ? AND type = 'handoff' AND is_deleted = 0"
    if unread_only:
        where_clause += " AND read_at IS NULL"

    # Query the inbox
    with _db() as db:
        rows = db.execute(
            f"SELECT id, title, content, metadata_json, created_at, read_at FROM memory_items "
            f"{where_clause} ORDER BY created_at DESC LIMIT ?",
            (agent_id, limit)
        ).fetchall()

    # Format result
    if not rows:
        return f"Inbox for {agent_id}: (empty)"

    lines = [f"Inbox for {agent_id} ({len(rows)} {'unread' if unread_only else 'total'}):"]
    for row in rows:
        # Parse from_agent from metadata_json
        from_agent = "?"
        try:
            meta = json.loads(row["metadata_json"] or "{}")
            from_agent = meta.get("from_agent", "?")
        except Exception:
            pass

        # Truncate task (content) to 60 chars
        task_truncated = (row["content"] or "")[:60]
        lines.append(f"  [{row['id'][:8]}] from={from_agent} task={task_truncated} created={row['created_at']}")

    return "\n".join(lines)

def memory_inbox_ack_impl(memory_id: str) -> str:
    """Marks a handoff memory as read."""
    # 1. Compute current timestamp
    now = datetime.now(timezone.utc).isoformat()

    # 2. Update read_at and updated_at
    with _db() as db:
        db.execute(
            "UPDATE memory_items SET read_at = ?, updated_at = ? WHERE id = ? AND type = 'handoff' AND is_deleted = 0",
            (now, now, memory_id)
        )
        rows_affected = db.total_changes  # This may not be reliable; use a verify query instead

        # Verify update actually happened
        verify = db.execute(
            "SELECT id FROM memory_items WHERE id = ? AND type = 'handoff' AND is_deleted = 0 AND read_at IS NOT NULL",
            (memory_id,)
        ).fetchone()

    # 3. Check result
    if not verify:
        return f"Error: memory {memory_id} not found or not a handoff"

    # 4. Record history and return
    _record_history(memory_id, "handoff_ack", None, now, "read_at", "")
    return f"Acked: {memory_id}"

async def conversation_start_impl(title, agent_id="", model_id="", tags=""):
    cid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    metadata = json.dumps({"tags": [t.strip() for t in tags.split(",") if t.strip()]}) if tags else "{}"
    with _db() as db:
        db.execute(
            "INSERT INTO memory_items (id, type, title, agent_id, model_id, metadata_json, created_at) VALUES (?, 'conversation', ?, ?, ?, ?, ?)",
            (cid, title, agent_id, model_id, metadata, now)
        )
    return f"Conversation started: {cid}"

async def conversation_append_impl(conversation_id, role, content, agent_id="", model_id="", embed=True):
    with _db() as db:
        exists = db.execute(
            "SELECT id FROM memory_items WHERE id = ? AND type = 'conversation' AND is_deleted = 0",
            (conversation_id,)
        ).fetchone()
    if not exists:
        return f"Error: conversation {conversation_id} not found"
    mid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _db() as db:
        db.execute(
            "INSERT INTO memory_items (id, type, title, content, agent_id, model_id, created_at) VALUES (?, 'message', ?, ?, ?, ?, ?)",
            (mid, role, content, agent_id, model_id, now)
        )
        db.execute("INSERT INTO memory_relationships (id, from_id, to_id, relationship_type, created_at) VALUES (?, ?, ?, 'message', ?)",
                   (str(uuid.uuid4()), conversation_id, mid, now))
    if embed:
        vec, m = await _embed(content)
        if vec:
            with _db() as db:
                db.execute("INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim, created_at) VALUES (?,?,?,?,?,?)",
                          (str(uuid.uuid4()), mid, _pack(vec), m, len(vec), now))
    return f"Appended: {mid}"

VALID_SCOPES = {"user", "session", "agent", "org"}

async def memory_write_impl(type, content, title="", metadata="{}", agent_id="", model_id="", change_agent="", importance=0.5, source="agent", embed=True, user_id="", scope="agent", valid_from="", valid_to="", auto_classify=False):
    """Internal implementation for memory_write. Contradiction detection is automatic."""
    if isinstance(metadata, dict):
        metadata = json.dumps(metadata)
    elif not isinstance(metadata, str):
        metadata = "{}"
    _track_cost("write_calls")

    if auto_classify and (not type or type == "auto"):
        type = await _auto_classify(content, title)

    # Defense-in-depth content size check (primary validation is in memory_bridge.py)
    if content and len(content) > 50_000:
        return f"Error: content too large ({len(content)} chars, max 50000)"
    safety_err = _check_content_safety(content)
    if safety_err:
        return safety_err
    if scope not in VALID_SCOPES:
        scope = "agent"
    item_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    try:
        importance = float(importance)
    except (TypeError, ValueError):
        importance = 0.5
    agent = change_agent.strip().lower() or _infer_change_agent_util(agent_id, model_id, default=DEFAULT_CHANGE_AGENT)

    # Session-scoped memories auto-expire in 24 hours
    expires_at = None
    if scope == "session":
        from datetime import timedelta
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()

    with _db() as db:
        _vf = valid_from or now
        _vt = valid_to or ""
        db.execute(
            "INSERT INTO memory_items (id, type, title, content, metadata_json, agent_id, model_id, change_agent, importance, source, origin_device, user_id, scope, expires_at, created_at, valid_from, valid_to) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, type, title, content, metadata, agent_id, model_id, agent, importance, source, ORIGIN_DEVICE, user_id, scope, expires_at, now, _vf, _vt)
        )
        if embed:
            db.execute("INSERT INTO chroma_sync_queue (memory_id, operation) VALUES (?,?)", (item_id, "upsert"))
        db.execute("UPDATE memory_items SET content_hash = ? WHERE id = ?",
                   (hashlib.sha256((content or "").encode("utf-8")).hexdigest(), item_id))

    vec = None
    if embed:
        vec, m = await _embed(content or title)
        if vec:
            with _db() as db:
                db.execute(
                    "INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim, created_at, content_hash) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), item_id, _pack(vec), m, len(vec), now, _content_hash(content or title))
                )

    _record_history(item_id, "create", None, content, "content", agent_id or agent)

    # Contradiction detection + auto-linking (runs after embedding is stored)
    superseded_ids = []
    if vec and type not in ("conversation", "message"):
        superseded_ids, related_candidates = await _check_contradictions(item_id, content, title, vec, type, agent_id)
        # Auto-link top related (non-contradictory) memory
        if related_candidates and not superseded_ids:
            best_id, best_score = related_candidates[0]
            try:
                memory_link_impl(item_id, best_id, "related")
                logger.debug(f"Auto-linked {item_id} -> {best_id} (score={best_score:.3f})")
            except Exception:
                pass

    result = f"Created: {item_id}"
    if superseded_ids:
        result += f" (superseded {len(superseded_ids)} conflicting memories: {', '.join(superseded_ids[:3])})"
    return result

async def memory_write_batch_impl(items: list[dict]):
    """
    Speed Optimization: Parallelized batch memory write (Speed #1).
    Expects list of dicts with keys matching memory_write_impl args.
    """
    results = []
    # 1. First pass: Insert metadata in one transaction
    now = datetime.now(timezone.utc).isoformat()
    
    write_tasks = []
    for item in items:
        mid = str(uuid.uuid4())
        agent = item.get("change_agent", "").strip().lower() or _infer_change_agent_util(item.get("agent_id", ""), item.get("model_id", ""), default=DEFAULT_CHANGE_AGENT)
        
        with _db() as db:
            db.execute(
                "INSERT INTO memory_items (id, type, title, content, metadata_json, agent_id, model_id, change_agent, importance, source, origin_device, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (mid, item["type"], item.get("title", ""), item["content"], item.get("metadata", "{}"), 
                 item.get("agent_id", ""), item.get("model_id", ""), agent, item.get("importance", 0.5), 
                 item.get("source", "agent"), ORIGIN_DEVICE, now)
            )
            if item.get("embed", True):
                db.execute("INSERT INTO chroma_sync_queue (memory_id, operation) VALUES (?,?)", (mid, "upsert"))
        
        if item.get("embed", True):
            # Queue for parallel embedding (gather)
            write_tasks.append((mid, item.get("content") or item.get("title")))
        results.append(mid)

    # 2. Parallelize embedding generation (Speed Optimization #1)
    # Bounded by _EMBED_SEM to prevent LM Studio overload
    async def _bounded_embed(text):
        async with _EMBED_SEM:
            return await _embed(text)

    if write_tasks:
        embed_jobs = [_bounded_embed(text) for _, text in write_tasks]
        try:
            embeddings = await asyncio.wait_for(
                asyncio.gather(*embed_jobs, return_exceptions=True),
                timeout=120.0
            )
        except asyncio.TimeoutError:
            logger.error(f"Batch embedding timed out after 120s for {len(write_tasks)} items")
            embeddings = [None] * len(write_tasks)

        with _db() as db:
            for (mid, text), result in zip(write_tasks, embeddings):
                if isinstance(result, Exception):
                    logger.warning(f"Batch embed failed for {mid}: {result}")
                    continue
                if result is None:
                    continue
                vec, m = result
                if vec:
                    db.execute(
                        "INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim, created_at, content_hash) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), mid, _pack(vec), m, len(vec), now, _content_hash(text))
                    )
    
    return f"Batch created: {len(results)} items"
