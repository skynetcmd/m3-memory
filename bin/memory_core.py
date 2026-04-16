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
            timeout=LLM_TIMEOUT
        )
        resp.raise_for_status()
        summary_text = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"Error during LLM summarization: {type(e).__name__}: {e}"

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
LLM_TIMEOUT            = float(os.environ.get("LLM_TIMEOUT", "120.0"))

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

    # Localized copy of mcp_tool_catalog.VALID_MEMORY_TYPES minus "auto"
    # (auto is the sentinel that requests classification, not a classifier output).
    # Kept local to avoid circular import: mcp_tool_catalog imports memory_core.
    valid_types = {
        "note", "fact", "decision", "preference", "conversation", "message",
        "task", "code", "config", "observation", "plan", "summary", "snippet",
        "reference", "log", "home", "user_fact", "scratchpad", "knowledge",
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

def _record_history(memory_id: str, event: str, prev_value: str = None, new_value: str = None, field: str = "content", actor_id: str = "", db=None):
    """Records a change event in the memory_history audit trail.

    Pass ``db`` when the caller already holds an open connection (e.g. inside
    a ``with _db() as db:`` block). Opening a second pool connection while
    the outer one has an uncommitted writer causes SQLite WAL writer
    contention, which burns the full ``busy_timeout`` per call.
    """
    row = (str(uuid.uuid4()), memory_id, event, prev_value, new_value, field, actor_id)
    sql = "INSERT INTO memory_history (id, memory_id, event, prev_value, new_value, field, actor_id) VALUES (?,?,?,?,?,?,?)"
    try:
        if db is not None:
            db.execute(sql, row)
        else:
            with _db() as inner:
                inner.execute(sql, row)
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


# Tuned against llama-server --parallel 4 + --ubatch-size 4096:
# 4 in-flight chunks × 1024 texts/chunk ≈ 161 embeds/sec on RTX 5080.
EMBED_BULK_CHUNK = int(os.environ.get("EMBED_BULK_CHUNK", "1024"))
EMBED_BULK_CONCURRENCY = int(os.environ.get("EMBED_BULK_CONCURRENCY", "4"))
_EMBED_BULK_SEM = asyncio.Semaphore(EMBED_BULK_CONCURRENCY)


async def _embed_many(texts: list[str]) -> list[tuple[list[float] | None, str]]:
    """Batched embed path that bypasses the per-call semaphore and posts many
    inputs in a single /embeddings request. Honors the content-hash cache so
    repeated texts cost nothing. Returns a list aligned with `texts`."""
    if not texts:
        return []

    out: list[tuple[list[float] | None, str] | None] = [None] * len(texts)

    # Cache lookup: dedupe by content_hash, fetch any cached rows in one pass.
    hashes = [_content_hash(t) for t in texts]
    uniq_hashes = list(set(hashes))
    cached_vecs: dict[str, tuple[list[float], str]] = {}
    try:
        with _db() as db:
            placeholders = ",".join("?" * len(uniq_hashes))
            rows = db.execute(
                f"SELECT content_hash, embedding, embed_model FROM memory_embeddings "
                f"WHERE embed_model = ? AND content_hash IN ({placeholders})",
                (EMBED_MODEL, *uniq_hashes),
            ).fetchall()
            for r in rows:
                cached_vecs[r["content_hash"]] = (_unpack(r["embedding"]), r["embed_model"])
    except Exception as e:
        logger.debug(f"Bulk embed cache lookup failed: {e}")

    # Fill cached slots; collect misses to embed.
    miss_indices: list[int] = []
    miss_texts: list[str] = []
    for i, (t, h) in enumerate(zip(texts, hashes)):
        hit = cached_vecs.get(h)
        if hit is not None:
            out[i] = hit
        else:
            miss_indices.append(i)
            miss_texts.append(t)

    if not miss_texts:
        return out  # type: ignore[return-value]

    _track_cost("embed_calls", sum(len(t.split()) * 2 for t in miss_texts))
    token = ctx.get_secret("LM_API_TOKEN") or "lm-studio"
    client = _get_embed_client()
    result = await get_best_embed(client, token)
    if not result:
        for i in miss_indices:
            out[i] = (None, EMBED_MODEL)
        return out  # type: ignore[return-value]
    base_url, model = result

    # Captured by _post_once's except handlers so the drop log can surface
    # the real reason. Shared across all concurrent chunks in this call.
    _last_embed_err: dict[str, str] = {"msg": ""}

    async def _post_once(chunk_texts: list[str]) -> list[list[float] | None] | None:
        """One POST. Returns vectors on success, None on failure (caller decides bisect).

        On failure, stashes the last error so the final drop log can surface
        why (e.g. HTTP 400 with "exceeds context size" — invisible before).
        """
        try:
            resp = await client.post(
                f"{base_url}/embeddings",
                json={"model": model, "input": chunk_texts},
                headers={"Authorization": f"Bearer {token}"},
                timeout=_httpx.Timeout(CHROMA_CONNECT_T, read=EMBED_TIMEOUT_READ * 4),
            )
            resp.raise_for_status()
            data = resp.json()["data"]
            ordered = sorted(data, key=lambda d: d.get("index", 0))
            return [d["embedding"] for d in ordered]
        except _httpx.HTTPStatusError as e:
            _last_embed_err["msg"] = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
            return None
        except Exception as e:
            _last_embed_err["msg"] = f"{type(e).__name__}: {e}"
            return None

    async def _post_chunk(chunk_texts: list[str]) -> list[list[float] | None]:
        """Post a chunk with retry + bisection on failure.

        If a batch fails 3 attempts, split it in half and recurse on both halves.
        Single-text failures (len==1) are surfaced as [None] so a single bad
        input never takes down its neighbors.
        """
        async with _EMBED_BULK_SEM:
            # Try up to 3 times at this chunk size.
            for attempt in range(3):
                result = await _post_once(chunk_texts)
                if result is not None:
                    return result
                if attempt < 2:
                    await asyncio.sleep(2 * (2 ** attempt))

        # All retries failed. Bisect if we can.
        if len(chunk_texts) == 1:
            reason = _last_embed_err.get("msg") or "unknown"
            logger.warning(
                f"Bulk embed: dropping single input of len={len(chunk_texts[0])} "
                f"after 3 attempts — last error: {reason}"
            )
            return [None]
        mid = len(chunk_texts) // 2
        logger.info(
            f"Bulk embed: bisecting failed chunk of {len(chunk_texts)} into "
            f"{mid} + {len(chunk_texts) - mid}"
        )
        left, right = await asyncio.gather(
            _post_chunk(chunk_texts[:mid]),
            _post_chunk(chunk_texts[mid:]),
        )
        return [*left, *right]

    # Split misses into chunks and fan out under _EMBED_BULK_SEM.
    chunks = [
        miss_texts[i : i + EMBED_BULK_CHUNK]
        for i in range(0, len(miss_texts), EMBED_BULK_CHUNK)
    ]
    chunk_results = await asyncio.gather(*(_post_chunk(c) for c in chunks))

    global _EMBED_DIM_VALIDATED
    flat: list[list[float] | None] = []
    for cr in chunk_results:
        flat.extend(cr)
    for local_i, vec in enumerate(flat):
        if vec is not None and not _EMBED_DIM_VALIDATED:
            if len(vec) != EMBED_DIM:
                logger.error(
                    f"Embedding dimension mismatch: got {len(vec)}, expected {EMBED_DIM}"
                )
            _EMBED_DIM_VALIDATED = True
        out[miss_indices[local_i]] = (vec, model)

    return out  # type: ignore[return-value]


async def memory_write_bulk_impl(items: list[dict]) -> list[str]:
    """Bulk write that routes embeddings through `_embed_many`. Intended for
    benchmark / import paths where per-item contradiction detection would
    dominate wall-clock. Returns a list of item_ids (or empty string on failure)."""
    if not items:
        return []

    now = datetime.now(timezone.utc).isoformat()
    prepared: list[dict] = []
    for it in items:
        mid = it.get("id") or str(uuid.uuid4())
        meta = it.get("metadata", "{}")
        if isinstance(meta, dict):
            meta = json.dumps(meta)
        scope = it.get("scope", "agent")
        if scope not in VALID_SCOPES:
            scope = "agent"
        content = it.get("content") or ""
        title = it.get("title") or ""
        agent = (
            (it.get("change_agent") or "").strip().lower()
            or _infer_change_agent_util(
                it.get("agent_id", ""), it.get("model_id", ""), default=DEFAULT_CHANGE_AGENT
            )
        )
        try:
            importance = float(it.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        expires_at = None
        if scope == "session":
            from datetime import timedelta
            expires_at = (
                datetime.now(timezone.utc) + timedelta(hours=24)
            ).isoformat()
        prepared.append(
            {
                "id": mid,
                "type": it.get("type", "note"),
                "title": title,
                "content": content,
                "metadata": meta,
                "agent_id": it.get("agent_id", ""),
                "model_id": it.get("model_id", ""),
                "change_agent": agent,
                "importance": importance,
                "source": it.get("source", "agent"),
                "user_id": it.get("user_id", ""),
                "scope": scope,
                "expires_at": expires_at,
                "valid_from": it.get("valid_from") or now,
                "valid_to": it.get("valid_to") or "",
                "conversation_id": it.get("conversation_id") or None,
                "refresh_on": it.get("refresh_on") or None,
                "refresh_reason": it.get("refresh_reason") or None,
                "embed": it.get("embed", True),
                "embed_text": content or title,
            }
        )

    # Phase 1: INSERT memory_items + chroma queue + history in one transaction.
    with _db() as db:
        for p in prepared:
            db.execute(
                "INSERT INTO memory_items (id, type, title, content, metadata_json, agent_id, model_id, "
                "change_agent, importance, source, origin_device, user_id, scope, expires_at, created_at, "
                "valid_from, valid_to, conversation_id, refresh_on, refresh_reason, content_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    p["id"], p["type"], p["title"], p["content"], p["metadata"],
                    p["agent_id"], p["model_id"], p["change_agent"], p["importance"],
                    p["source"], ORIGIN_DEVICE, p["user_id"], p["scope"], p["expires_at"],
                    now, p["valid_from"], p["valid_to"], p["conversation_id"],
                    p["refresh_on"], p["refresh_reason"],
                    hashlib.sha256((p["content"] or "").encode("utf-8")).hexdigest(),
                ),
            )
            if p["embed"]:
                db.execute(
                    "INSERT INTO chroma_sync_queue (memory_id, operation) VALUES (?,?)",
                    (p["id"], "upsert"),
                )
            _record_history(
                p["id"], "create", None, p["content"], "content",
                p["agent_id"] or p["change_agent"], db=db,
            )

    # Phase 2: batched embeddings for items that requested them.
    to_embed = [p for p in prepared if p["embed"] and p["embed_text"]]
    if to_embed:
        vecs = await _embed_many([p["embed_text"] for p in to_embed])
        with _db() as db:
            for p, (vec, m) in zip(to_embed, vecs):
                if not vec:
                    continue
                db.execute(
                    "INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim, created_at, content_hash) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()), p["id"], _pack(vec), m, len(vec), now,
                        _content_hash(p["embed_text"]),
                    ),
                )

    return [p["id"] for p in prepared]


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

def _apply_recency_bonus(scored, recency_bias, explain=False):
    """Add a rank-based recency bonus to each (score, item) pair.

    Items are ranked lexicographically by `valid_from` (ISO-8601 sorts
    correctly as strings). The oldest dated item receives bonus 0, the
    newest receives `recency_bias`, with linear interpolation between.
    Items with empty `valid_from` receive bonus 0. If fewer than two dated
    items exist, the input is returned unchanged.

    Used to break ties in favor of supersession evidence for "what is my
    current X" queries without parsing timestamps.
    """
    if not scored or recency_bias <= 0:
        return scored
    with_vf = [(i, (it.get("valid_from") or "")) for i, (_, it) in enumerate(scored)]
    dated = [(i, v) for i, v in with_vf if v]
    if len(dated) < 2:
        return scored
    dated.sort(key=lambda x: x[1])
    n = len(dated) - 1
    rank_of = {idx: rank for rank, (idx, _) in enumerate(dated)}
    rescored = []
    for i, (s, it) in enumerate(scored):
        bonus = recency_bias * (rank_of[i] / n) if i in rank_of else 0.0
        if explain and "_explanation" in it:
            it["_explanation"]["recency_bonus"] = bonus
        rescored.append((s + bonus, it))
    return rescored


def _trim_by_elbow(ranked: list[tuple[float, dict]], sensitivity: float = 1.5) -> list[tuple[float, dict]]:
    """Trims results where the score drop-off is significantly higher than average."""
    if len(ranked) < 3:
        return ranked
    
    # Calculate score differences between consecutive results
    diffs = [ranked[i][0] - ranked[i+1][0] for i in range(len(ranked) - 1)]
    avg_diff = sum(diffs) / len(diffs)
    
    # Find the first 'elbow' where the drop is significantly larger than the average
    for i, d in enumerate(diffs):
        if d > avg_diff * sensitivity:
            # We found an elbow, trim here
            return ranked[:i+1]
            
    return ranked


def _apply_temporal_boost(scored, query, explain=False):
    """Detects dates in query and boosts items with matching or nearby valid_from dates."""
    # 1. Extract potential dates from query (YYYY-MM-DD)
    date_patterns = [
        r"\b(\d{4})-(\d{2})-(\d{2})\b",
        r"\b(\d+)\s+(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{4})\b",
    ]
    query_dates = []
    for pattern in date_patterns:
        for match in re.finditer(pattern, query.lower()):
            try:
                if "-" in match.group(0):
                    query_dates.append(datetime.fromisoformat(match.group(0)).date())
                else:
                    d, m, y = match.groups()
                    months = ["january", "february", "march", "april", "may", "june", 
                              "july", "august", "september", "october", "november", "december"]
                    query_dates.append(date(int(y), months.index(m) + 1, int(d)))
            except Exception:
                continue
    
    if not query_dates:
        return scored

    rescored = []
    for s, it in scored:
        boost = 0.0
        vf_str = it.get("valid_from", "")
        if vf_str:
            try:
                vf_date = datetime.fromisoformat(vf_str.split("T")[0]).date()
                for qd in query_dates:
                    diff = abs((vf_date - qd).days)
                    if diff == 0: boost = max(boost, 0.25)
                    elif diff <= 2: boost = max(boost, 0.15)
                    elif diff <= 7: boost = max(boost, 0.05)
            except Exception:
                pass
        
        if explain and boost > 0:
            if "_explanation" not in it: it["_explanation"] = {}
            it["_explanation"]["temporal_boost"] = boost
        rescored.append((s + boost, it))
    return rescored


async def memory_search_scored_impl(
    query,
    k=8,
    type_filter="",
    agent_filter="",
    search_mode="hybrid",
    user_id="",
    scope="",
    as_of="",
    conversation_id="",
    explain=False,
    extra_columns=None,
    recency_bias=0.0,
    vector_weight=0.7,
    adaptive_k=False,
    _depth=0,
):
    """Hybrid FTS5+vector+MMR search returning a list of (score, item_dict).

    Structured sibling of `memory_search_impl`. Used by benchmarks and other
    callers that need raw result rows (with metadata_json, conversation_id,
    valid_from, etc.) rather than the formatted text output.

    `extra_columns` is an optional list of extra `mi.<column>` names to include
    in each item dict (e.g. ["metadata_json", "conversation_id", "valid_from",
    "valid_to", "user_id"]). Federated Chroma fallback results will NOT have
    these extra fields.
    """
    try:
        k = int(k)
    except (TypeError, ValueError):
        k = 8
    _track_cost("search_calls")
    if _depth > 1:
        return []

    q_vec, _ = await _embed(query)
    if not q_vec:
        return []

    extra_columns = list(extra_columns or [])
    _BASE_COLS = ["id", "content", "title", "type", "importance"]
    _allowed_extra = {
        "metadata_json", "conversation_id", "valid_from", "valid_to",
        "user_id", "scope", "agent_id", "created_at", "source",
    }
    if recency_bias and "valid_from" not in extra_columns:
        extra_columns = extra_columns + ["valid_from"]
    safe_extra = [c for c in extra_columns if c in _allowed_extra and c not in _BASE_COLS]
    extra_sql = (", " + ", ".join(f"mi.{c}" for c in safe_extra)) if safe_extra else ""

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
    if conversation_id:
        where_clauses.append("mi.conversation_id = ?")
        params.append(conversation_id)

    if as_of:
        where_clauses.append("(mi.valid_from = '' OR mi.valid_from <= ?)")
        where_clauses.append("(mi.valid_to = '' OR mi.valid_to > ?)")
        params.extend([as_of, as_of])

    where_sql = " AND ".join(where_clauses)

    def _recurse_semantic():
        return memory_search_scored_impl(
            query, k, type_filter, agent_filter, "semantic",
            user_id, scope, as_of, conversation_id, explain,
            extra_columns, recency_bias, vector_weight, _depth + 1,
        )

    try:
        with _db() as db:
            if search_mode == "semantic":
                sql = f"""
                    SELECT mi.id, mi.content, mi.title, mi.type, mi.importance, me.embedding, 0.0 as bm25_score{extra_sql}
                    FROM memory_items mi
                    JOIN memory_embeddings me ON mi.id = me.memory_id
                    WHERE {where_sql}
                    LIMIT 1000
                """
                rows = db.execute(sql, params).fetchall()
            else:
                sql = f"""
                    SELECT mi.id, mi.content, mi.title, mi.type, mi.importance, me.embedding,
                           bm25(memory_items_fts) as bm25_score{extra_sql}
                    FROM memory_items mi
                    JOIN memory_embeddings me ON mi.id = me.memory_id
                    JOIN memory_items_fts fts ON mi.rowid = fts.rowid
                    WHERE {where_sql} AND memory_items_fts MATCH ?
                    ORDER BY bm25_score ASC
                    LIMIT 1000
                """
                is_exact_query = (query.startswith('"') and query.endswith('"')) or (query.startswith("'") and query.endswith("'"))
                clean_query = query[1:-1] if is_exact_query else query
                if is_exact_query:
                    fts_query = f'"{clean_query}"'
                else:
                    clean_query = _sanitize_fts(clean_query)
                    if not clean_query:
                        return await _recurse_semantic()
                    fts_query = f"{clean_query}*" if " " not in clean_query and clean_query.isalnum() else clean_query

                rows = db.execute(sql, (*params, fts_query)).fetchall()
                if not rows:
                    return await _recurse_semantic()
    except sqlite3.OperationalError:
        return await _recurse_semantic()

    scored = []
    if len(rows) > SEARCH_ROW_CAP:
        rows = rows[:SEARCH_ROW_CAP]
    page_matrix = [_unpack(r["embedding"]) for r in rows]
    page_scores = _batch_cosine(q_vec, page_matrix)

    for i, row in enumerate(rows):
        item = dict(row)
        del item["embedding"]
        vector_score = page_scores[i]
        bm25_norm = (1.0 / (1.0 + abs(row["bm25_score"])))
        final_score = vector_score * vector_weight + bm25_norm * (1.0 - vector_weight)
        if explain:
            item["_explanation"] = {
                "vector": vector_score,
                "bm25": bm25_norm,
                "importance": row["importance"],
                "raw_hybrid": final_score,
                "vector_weight": vector_weight,
            }
        scored.append((final_score, item))

    # Apply temporal boost if dates detected in query
    if scored:
        scored = _apply_temporal_boost(scored, query, explain=explain)

    if recency_bias > 0 and scored:
        scored = _apply_recency_bonus(scored, recency_bias, explain=explain)

    _MMR_LAMBDA = 0.7
    pre_ranked_all = sorted(scored, key=lambda x: x[0], reverse=True)
    
    # Adaptive K: Trim by elbow if requested
    if adaptive_k:
        pre_ranked_all = _trim_by_elbow(pre_ranked_all)
        # Recalculate k to match the trimmed length if it's smaller
        if len(pre_ranked_all) < k:
            k = len(pre_ranked_all)

    seen_content: set[str] = set()
    pre_ranked: list = []
    for entry in pre_ranked_all:
        c = (entry[1].get("content") or "").strip()
        if c and c in seen_content:
            continue
        if c:
            seen_content.add(c)
        pre_ranked.append(entry)
        if len(pre_ranked) >= k * 3:
            break
    if len(pre_ranked) > k and len(page_matrix) > 0:
        _emb_lookup = {rows[i]["id"]: page_matrix[i] for i in range(len(rows))}
        selected = [pre_ranked[0]]
        candidates = list(pre_ranked[1:])
        while candidates and len(selected) < k:
            best_idx, best_mmr = 0, -float('inf')
            for ci, (c_score, c_item) in enumerate(candidates):
                c_vec = _emb_lookup.get(c_item["id"])
                if c_vec is None:
                    best_idx = ci
                    break
                similarities = [_batch_cosine(c_vec, [_emb_lookup[s[1]["id"]]])[0]
                                for s in selected if s[1]["id"] in _emb_lookup]
                max_sim = max(similarities, default=0.0)
                mmr = _MMR_LAMBDA * c_score - (1 - _MMR_LAMBDA) * max_sim
                if mmr > best_mmr:
                    best_mmr = mmr
                    best_idx = ci
                if explain:
                    if "_explanation" not in c_item:
                        c_item["_explanation"] = {}
                    c_item["_explanation"]["max_sim_to_selected"] = max_sim
                    c_item["_explanation"]["mmr_penalty"] = (1 - _MMR_LAMBDA) * max_sim
            selected.append(candidates.pop(best_idx))
        ranked = selected
    else:
        ranked = pre_ranked

    _skip_federated = bool(type_filter or conversation_id or agent_filter or user_id or scope)
    if len(ranked) < 3 and not _skip_federated:
        fed_results = await _query_chroma(q_vec, k=3)
        for fr in fed_results:
            if not any(r[1]["id"] == fr["id"] for r in ranked):
                if explain:
                    fr["_explanation"] = {"source": "federated_chroma"}
                ranked.append((fr["score"], fr))

    if ranked:
        ids = [item[1]["id"] for item in ranked if "bm25_score" in item[1]]
        if ids:
            try:
                with _db() as db:
                    placeholders = ",".join(["?"] * len(ids))
                    db.execute(
                        f"UPDATE memory_items SET last_accessed_at = ?, access_count = access_count + 1 WHERE id IN ({placeholders})",
                        (datetime.now(timezone.utc).isoformat(), *ids),
                    )
            except Exception as e:
                logger.debug(f"Search result timestamp update failed: {e}")

    return ranked


async def memory_search_impl(query, k=8, type_filter="", agent_filter="", search_mode="hybrid", include_scratchpad=False, user_id="", scope="", as_of="", explain=False, conversation_id="", _depth=0):
    ranked = await memory_search_scored_impl(
        query, k=k, type_filter=type_filter, agent_filter=agent_filter,
        search_mode=search_mode, user_id=user_id, scope=scope, as_of=as_of,
        conversation_id=conversation_id, explain=explain,
    )
    if ranked is None:
        return "Search failed: FTS and semantic both unavailable."

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
                vw = exp.get("vector_weight", 0.7)
                lines.append(f"   Breakdown: vector={exp['vector']:.4f} (weight {vw:.2f}) + bm25={exp['bm25']:.4f} (weight {1.0-vw:.2f}) -> raw={exp['raw_hybrid']:.4f}")
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

async def memory_update_impl(id, content="", title="", metadata="", importance=-1.0, reembed=False, refresh_on="", refresh_reason="", conversation_id=""):
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
        old = db.execute(
            "SELECT content, title, refresh_on, refresh_reason, conversation_id FROM memory_items WHERE id = ?",
            (id,)
        ).fetchone()
        if content:
            _record_history(id, "update", old["content"] if old else None, content, "content", db=db)
            db.execute("UPDATE memory_items SET content = ? WHERE id = ?", (content, id))
        if title:
            _record_history(id, "update", old["title"] if old else None, title, "title", db=db)
            db.execute("UPDATE memory_items SET title = ? WHERE id = ?", (title, id))
        if importance >= 0: db.execute("UPDATE memory_items SET importance = ? WHERE id = ?", (importance, id))
        if metadata: db.execute("UPDATE memory_items SET metadata_json = ? WHERE id = ?", (metadata, id))
        # Refresh lifecycle: empty string leaves unchanged, "clear" clears, anything
        # else is treated as a new ISO timestamp. Using the explicit sentinel "clear"
        # lets callers distinguish "no change" from "mark as refreshed, remove reminder".
        if refresh_on:
            new_val = None if refresh_on == "clear" else refresh_on
            _record_history(id, "update", old["refresh_on"] if old else None, new_val, "refresh_on", db=db)
            db.execute("UPDATE memory_items SET refresh_on = ? WHERE id = ?", (new_val, id))
        if refresh_reason:
            new_val = None if refresh_reason == "clear" else refresh_reason
            _record_history(id, "update", old["refresh_reason"] if old else None, new_val, "refresh_reason", db=db)
            db.execute("UPDATE memory_items SET refresh_reason = ? WHERE id = ?", (new_val, id))
        if conversation_id:
            new_val = None if conversation_id == "clear" else conversation_id
            _record_history(id, "update", old["conversation_id"] if old else None, new_val, "conversation_id", db=db)
            db.execute("UPDATE memory_items SET conversation_id = ? WHERE id = ?", (new_val, id))
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
        _record_history(id, "delete", row["content"], None, "content", db=db)
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

def _memory_link_inner(from_id: str, to_id: str, relationship_type: str, db) -> str:
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


def memory_link_impl(from_id: str, to_id: str, relationship_type: str = "related", db=None) -> str:
    """Creates a directional link between two memory items."""
    if relationship_type not in VALID_RELATIONSHIP_TYPES:
        return f"Error: invalid relationship type '{relationship_type}'. Valid: {', '.join(sorted(VALID_RELATIONSHIP_TYPES))}"
    
    if db is not None:
        return _memory_link_inner(from_id, to_id, relationship_type, db)
    
    with _db() as db:
        return _memory_link_inner(from_id, to_id, relationship_type, db)

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
                        context_ids: list, note: str = "",
                        task_id: str = "") -> str:
    """Creates a handoff memory for inter-agent task transfer."""
    # 0. Validate agents are registered
    if not _agent_exists(to_agent):
        return f"Error: to_agent '{to_agent}' is not registered. Call agent_register first."
    if not _agent_exists(from_agent):
        return f"Error: from_agent '{from_agent}' is not registered. Call agent_register first."

    # 1. Generate new UUID
    new_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # 2. Insert handoff memory directly via raw SQL
    with _db() as db:
        meta = {"from_agent": from_agent, "note": note}
        if task_id:
            meta["task_id"] = task_id
        metadata_json = json.dumps(meta)
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

    # 5. Fire-and-forget notify to to_agent
    try:
        notify_impl(to_agent, "handoff", {
            "memory_id": new_id,
            "from_agent": from_agent,
            "task": (task or "")[:200],
            "task_id": task_id or None,
        })
    except Exception as e:
        logger.warning(f"handoff notify failed for {to_agent}: {e}")

    # 6. Return status
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

def _count_refresh_backlog(agent_id: str = "") -> int:
    """Cheap count of memories whose refresh_on has arrived. Used by lifecycle
    hooks (agent_register / agent_offline) and maintenance to surface the
    backlog without expanding the full list. Backed by the partial index
    idx_mi_refresh_on, so this is O(index-size-of-flagged-rows).
    """
    now = datetime.now(timezone.utc).isoformat()
    where = ["is_deleted = 0", "refresh_on IS NOT NULL", "refresh_on <= ?"]
    params: list = [now]
    if agent_id:
        where.append("agent_id = ?")
        params.append(agent_id)
    try:
        with _db() as db:
            row = db.execute(
                f"SELECT COUNT(*) FROM memory_items WHERE {' AND '.join(where)}",
                params,
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        # refresh_on column may not exist on an un-migrated DB — fail quiet.
        return 0

def _refresh_hint(agent_id: str = "") -> str:
    """One-line hint suitable for appending to lifecycle response strings.
    Returns empty string when there is no backlog, so callers can concatenate
    unconditionally.
    """
    n = _count_refresh_backlog(agent_id)
    if n <= 0:
        return ""
    noun = "memory" if n == 1 else "memories"
    scope = "of yours" if agent_id else "in the store"
    return f" | {n} {noun} {scope} due for refresh (see memory_refresh_queue)"

def memory_refresh_queue_impl(agent_id: str = "", limit: int = 50, include_future: bool = False) -> str:
    """Lists memories whose refresh_on timestamp has arrived (or all with refresh_on set
    if include_future=True). Read-only — actual refresh goes through memory_update.

    Surfaces memories flagged for periodic review via the refresh_on lifecycle.
    Scope to an agent with agent_id, or leave empty to see everything.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 500))

    where = ["is_deleted = 0", "refresh_on IS NOT NULL"]
    params: list = []
    if not include_future:
        where.append("refresh_on <= ?")
        params.append(now)
    if agent_id:
        where.append("agent_id = ?")
        params.append(agent_id)
    where_sql = " AND ".join(where)

    with _db() as db:
        rows = db.execute(
            f"SELECT id, type, title, refresh_on, refresh_reason, agent_id, updated_at "
            f"FROM memory_items WHERE {where_sql} ORDER BY refresh_on ASC LIMIT ?",
            (*params, limit)
        ).fetchall()

    if not rows:
        scope_label = f" for {agent_id}" if agent_id else ""
        when = "with refresh_on set" if include_future else "due for refresh"
        return f"Refresh queue{scope_label}: (empty — no memories {when})"

    scope_label = f" for {agent_id}" if agent_id else ""
    lines = [f"Refresh queue{scope_label} ({len(rows)} item{'s' if len(rows) != 1 else ''}):"]
    for row in rows:
        title = (row["title"] or "")[:60]
        reason = row["refresh_reason"] or "(no reason)"
        lines.append(
            f"  [{row['id'][:8]}] {row['type']:<12} due={row['refresh_on']} "
            f"reason={reason} title={title}"
        )
    return "\n".join(lines)

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

async def memory_write_impl(type, content, title="", metadata="{}", agent_id="", model_id="", change_agent="", importance=0.5, source="agent", embed=True, user_id="", scope="agent", valid_from="", valid_to="", auto_classify=False, conversation_id="", refresh_on="", refresh_reason=""):
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
        _cid = conversation_id or None
        _ron = refresh_on or None
        _rreason = refresh_reason or None
        db.execute(
            "INSERT INTO memory_items (id, type, title, content, metadata_json, agent_id, model_id, change_agent, importance, source, origin_device, user_id, scope, expires_at, created_at, valid_from, valid_to, conversation_id, refresh_on, refresh_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, type, title, content, metadata, agent_id, model_id, agent, importance, source, ORIGIN_DEVICE, user_id, scope, expires_at, now, _vf, _vt, _cid, _ron, _rreason)
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

# ── Task Orchestration: State Machine + Helper Functions ─────────────────────────

TASK_STATE_TRANSITIONS = {
    "pending":     {"in_progress", "blocked", "cancelled"},
    "in_progress": {"blocked", "completed", "failed", "cancelled"},
    "blocked":     {"in_progress", "cancelled"},
    "completed":   set(),
    "failed":      set(),
    "cancelled":   set(),
}
VALID_TASK_STATES = frozenset(TASK_STATE_TRANSITIONS.keys())
TERMINAL_TASK_STATES = frozenset({"completed", "failed", "cancelled"})
VALID_AGENT_STATUSES = frozenset({"active", "idle", "offline"})

def _validate_task_transition(prev: str, new: str):
    """Validates task state transitions. Returns None if valid, error string if invalid."""
    if new not in VALID_TASK_STATES:
        return f"Error: invalid task state '{new}'. Valid: {', '.join(sorted(VALID_TASK_STATES))}"
    if prev == new:
        return None
    allowed = TASK_STATE_TRANSITIONS.get(prev, set())
    if new not in allowed:
        return (f"Error: cannot transition task from '{prev}' to '{new}'. "
                f"Allowed from '{prev}': {sorted(allowed) or '(terminal)'}")
    return None

def _agent_exists(agent_id: str) -> bool:
    """Checks if an agent is registered in the agents table."""
    with _db() as db:
        row = db.execute("SELECT 1 FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        return row is not None

# ── Agent Registry (5 functions) ──────────────────────────────────────────────────

def agent_register_impl(agent_id: str, role: str, capabilities: list, metadata: dict) -> str:
    """Registers or updates an agent in the registry."""
    if not agent_id:
        return "Error: agent_id cannot be empty"

    now = datetime.now(timezone.utc).isoformat()
    caps_json = json.dumps(capabilities or [])
    meta_json = json.dumps(metadata or {})

    with _db() as db:
        db.execute(
            """INSERT INTO agents (agent_id, role, capabilities, metadata_json, status, last_seen, created_at)
               VALUES (?, ?, ?, ?, 'active', ?, ?)
               ON CONFLICT(agent_id) DO UPDATE SET
                 role=excluded.role,
                 capabilities=excluded.capabilities,
                 metadata_json=excluded.metadata_json,
                 status='active',
                 last_seen=excluded.last_seen""",
            (agent_id, role, caps_json, meta_json, now, now)
        )

    return f"Registered: {agent_id} (role={role}, status=active)" + _refresh_hint(agent_id)

def agent_heartbeat_impl(agent_id: str) -> str:
    """Updates agent's last_seen timestamp and status to active."""
    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        cur = db.execute(
            "UPDATE agents SET last_seen = ?, status = 'active' WHERE agent_id = ?",
            (now, agent_id)
        )
        rowcount = cur.rowcount

    if rowcount == 0:
        return f"Error: agent '{agent_id}' not registered"

    return f"Heartbeat: {agent_id} (last_seen={now})"

def agent_list_impl(status: str = "", role: str = "") -> str:
    """Lists agents, optionally filtered by status and/or role."""
    where_clauses = []
    params = []

    if status:
        where_clauses.append("status = ?")
        params.append(status)
    if role:
        where_clauses.append("role = ?")
        params.append(role)

    where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    with _db() as db:
        rows = db.execute(
            f"SELECT agent_id, role, status, last_seen FROM agents {where} ORDER BY last_seen DESC",
            params
        ).fetchall()

    if not rows:
        return "(no agents)"

    lines = [f"Agents ({len(rows)}):"]
    for row in rows:
        lines.append(f"  [{row['agent_id']}] role={row['role']} status={row['status']} last_seen={row['last_seen']}")

    return "\n".join(lines)

def agent_get_impl(agent_id: str) -> str:
    """Retrieves detailed information about a single agent."""
    with _db() as db:
        row = db.execute(
            "SELECT * FROM agents WHERE agent_id = ?",
            (agent_id,)
        ).fetchone()

    if not row:
        return f"Error: agent '{agent_id}' not found"

    caps = json.loads(row["capabilities"] or "[]")
    meta = json.loads(row["metadata_json"] or "{}")

    lines = [
        f"Agent: {row['agent_id']}",
        f"  Role: {row['role']}",
        f"  Status: {row['status']}",
        f"  Capabilities: {caps}",
        f"  Metadata: {meta}",
        f"  Last Seen: {row['last_seen']}",
        f"  Created At: {row['created_at'] if 'created_at' in row.keys() else 'N/A'}",
    ]

    return "\n".join(lines)

def agent_offline_impl(agent_id: str) -> str:
    """Marks an agent as offline."""
    with _db() as db:
        cur = db.execute(
            "UPDATE agents SET status = 'offline' WHERE agent_id = ?",
            (agent_id,)
        )
        rowcount = cur.rowcount

    if rowcount == 0:
        return f"Error: agent '{agent_id}' not found"

    return f"Agent {agent_id} marked offline" + _refresh_hint(agent_id)

# ── Notifications (4 functions) ───────────────────────────────────────────────────

def notify_impl(agent_id: str, kind: str, payload: dict = None) -> str:
    """Sends a notification to an agent."""
    now = datetime.now(timezone.utc).isoformat()
    payload_json = json.dumps(payload or {})

    with _db() as db:
        db.execute(
            "INSERT INTO notifications (agent_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (agent_id, kind, payload_json, now)
        )
        # Get the ID of the newly inserted row
        new_id = db.execute(
            "SELECT last_insert_rowid() as id"
        ).fetchone()["id"]

    return f"Notified {agent_id}: {kind} (id={new_id})"

def notifications_poll_impl(agent_id: str, unread_only: bool = True, limit: int = 20) -> str:
    """Retrieves notifications for an agent."""
    where_clause = "WHERE agent_id = ?"
    params = [agent_id]

    if unread_only:
        where_clause += " AND read_at IS NULL"

    with _db() as db:
        rows = db.execute(
            f"SELECT id, kind, payload_json, created_at, read_at FROM notifications {where_clause} ORDER BY created_at DESC LIMIT ?",
            params + [limit]
        ).fetchall()

    if not rows:
        return f"Notifications for {agent_id}: (empty)"

    read_type = "unread" if unread_only else "total"
    lines = [f"Notifications for {agent_id} ({len(rows)} {read_type}):"]
    for row in rows:
        lines.append(f"  [{row['id']}] kind={row['kind']} payload={row['payload_json']} created={row['created_at']}")

    return "\n".join(lines)

def notifications_ack_impl(notification_id: int) -> str:
    """Marks a notification as read."""
    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        cur = db.execute(
            "UPDATE notifications SET read_at = ? WHERE id = ? AND read_at IS NULL",
            (now, notification_id)
        )
        rowcount = cur.rowcount

    if rowcount == 0:
        return f"Error: notification {notification_id} not found or already acked"

    return f"Acked notification {notification_id}"

def notifications_ack_all_impl(agent_id: str) -> str:
    """Marks all unread notifications for an agent as read."""
    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        cur = db.execute(
            "UPDATE notifications SET read_at = ? WHERE agent_id = ? AND read_at IS NULL",
            (now, agent_id)
        )
        rowcount = cur.rowcount

    return f"Acked {rowcount} notifications for {agent_id}"

# ── Tasks (7 functions) ───────────────────────────────────────────────────────────

def task_create_impl(title: str, created_by: str, description: str = "", owner_agent: str = "", parent_task_id: str = "", metadata: dict = None) -> str:
    """Creates a new task."""
    if not title:
        return "Error: title cannot be empty"
    if not created_by:
        return "Error: created_by cannot be empty"

    task_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        db.execute(
            """INSERT INTO tasks (id, title, description, state, created_by, owner_agent, parent_task_id, metadata_json, created_at, updated_at)
               VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)""",
            (task_id, title, description, created_by, owner_agent or None, parent_task_id or None, json.dumps(metadata or {}), now, now)
        )

    return f"Task created: {task_id}"

def task_assign_impl(task_id: str, owner_agent: str) -> str:
    """Assigns a task to an agent and transitions state to in_progress."""
    with _db() as db:
        row = db.execute(
            "SELECT state, created_by FROM tasks WHERE id = ? AND deleted_at IS NULL",
            (task_id,)
        ).fetchone()

    if not row:
        return f"Error: task '{task_id}' not found"

    prev_state = row["state"]
    err = _validate_task_transition(prev_state, "in_progress")
    if err:
        return err

    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        db.execute(
            "UPDATE tasks SET owner_agent = ?, state = 'in_progress', updated_at = ? WHERE id = ?",
            (owner_agent, now, task_id)
        )

    _record_history(task_id, "task_state", prev_state, "in_progress", "state", owner_agent)

    # Fire-and-forget notification
    try:
        notify_impl(owner_agent, "task_assigned", {"task_id": task_id})
    except Exception as e:
        logger.warning(f"task_assigned notify failed for {owner_agent}: {e}")

    return f"Task {task_id} assigned to {owner_agent} (state=in_progress)"

def task_update_impl(task_id: str, state: str = "", description: str = "", metadata: dict = None, actor: str = "") -> str:
    """Updates a task's state, description, and/or metadata."""
    with _db() as db:
        row = db.execute(
            "SELECT state, description, metadata_json, created_by FROM tasks WHERE id = ? AND deleted_at IS NULL",
            (task_id,)
        ).fetchone()

    if not row:
        return f"Error: task '{task_id}' not found"

    prev_state = row["state"]
    new_state = state if state else prev_state

    if state:
        err = _validate_task_transition(prev_state, new_state)
        if err:
            return err

    now = datetime.now(timezone.utc).isoformat()
    updates = ["updated_at = ?"]
    params = [now]

    if state:
        updates.append("state = ?")
        params.append(new_state)

    if description:
        updates.append("description = ?")
        params.append(description)

    if metadata is not None:
        updates.append("metadata_json = ?")
        params.append(json.dumps(metadata))

    if new_state in TERMINAL_TASK_STATES:
        updates.append("completed_at = ?")
        params.append(now)

    params.append(task_id)

    with _db() as db:
        db.execute(
            f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?",
            params
        )

    if state and prev_state != new_state:
        _record_history(task_id, "task_state", prev_state, new_state, "state", actor or "system")

        # Fire-and-forget notification if completed
        if new_state == "completed":
            try:
                notify_impl(row["created_by"], "task_completed", {"task_id": task_id})
            except Exception as e:
                logger.warning(f"task_completed notify failed for {row['created_by']}: {e}")

        return f"Task {task_id} updated: state={new_state}"
    else:
        return f"Task {task_id} updated"

def task_set_result_impl(task_id: str, result_memory_id: str) -> str:
    """Sets the result memory for a task (without changing state)."""
    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        cur = db.execute(
            "UPDATE tasks SET result_memory_id = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (result_memory_id, now, task_id)
        )
        rowcount = cur.rowcount

    if rowcount == 0:
        return f"Error: task '{task_id}' not found"

    return f"Task {task_id} result={result_memory_id}"

def task_get_impl(task_id: str, include_deleted: bool = False) -> str:
    """Retrieves detailed information about a task."""
    sql = "SELECT * FROM tasks WHERE id = ?"
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    with _db() as db:
        row = db.execute(sql, (task_id,)).fetchone()

    if not row:
        return f"Error: task '{task_id}' not found"

    lines = [
        f"Task: {row['id']}",
        f"  Title: {row['title']}",
        f"  Description: {row['description']}",
        f"  State: {row['state']}",
        f"  Created By: {row['created_by']}",
        f"  Owner: {row['owner_agent'] or '(unassigned)'}",
        f"  Parent Task: {row['parent_task_id'] or '(none)'}",
        f"  Result Memory: {row['result_memory_id'] or '(none)'}",
        f"  Created At: {row['created_at']}",
        f"  Updated At: {row['updated_at']}",
        f"  Completed At: {row['completed_at'] or '(not completed)'}",
        f"  Deleted At: {row['deleted_at'] or '(not deleted)'}",
    ]

    return "\n".join(lines)

def task_delete_impl(task_id: str, hard: bool = False, actor: str = "") -> str:
    """Delete a task.

    Soft-delete (default): sets `deleted_at` so pg_sync propagates the
    tombstone to the warehouse and peers on the next run. The row stays
    in local SQLite and is filtered out of reads.

    Hard-delete: only allowed once the row is already tombstoned. Removes
    the row from local SQLite. Note that sync is UPSERT-only, so a hard
    delete on one peer does NOT remove the row on other peers — they
    converge via the soft-delete tombstone.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _db() as db:
        row = db.execute(
            "SELECT state, deleted_at FROM tasks WHERE id = ?",
            (task_id,)
        ).fetchone()

        if not row:
            return f"Error: task '{task_id}' not found"

        if hard:
            if row["deleted_at"] is None:
                return (
                    f"Error: task '{task_id}' must be soft-deleted before hard-delete. "
                    "Call task_delete with hard=False first."
                )
            db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            _record_history(task_id, "task_deleted", row["state"], "hard_deleted", "deleted_at", actor or "system")
            return f"Task {task_id} hard-deleted"

        if row["deleted_at"] is not None:
            return f"Task {task_id} already soft-deleted at {row['deleted_at']}"

        db.execute(
            "UPDATE tasks SET deleted_at = ?, updated_at = ? WHERE id = ?",
            (now, now, task_id)
        )

    _record_history(task_id, "task_deleted", row["state"], "soft_deleted", "deleted_at", actor or "system")
    return f"Task {task_id} soft-deleted (tombstone will sync on next pg_sync run)"

def task_list_impl(owner_agent: str = "", state: str = "", parent_task_id: str = "", limit: int = 20, include_deleted: bool = False) -> str:
    """Lists tasks, optionally filtered by owner, state, and/or parent."""
    where_clauses = []
    params = []

    if not include_deleted:
        where_clauses.append("deleted_at IS NULL")
    if owner_agent:
        where_clauses.append("owner_agent = ?")
        params.append(owner_agent)
    if state:
        where_clauses.append("state = ?")
        params.append(state)
    if parent_task_id:
        where_clauses.append("parent_task_id = ?")
        params.append(parent_task_id)

    where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    with _db() as db:
        rows = db.execute(
            f"SELECT id, title, state, owner_agent FROM tasks {where} ORDER BY updated_at DESC LIMIT ?",
            params + [limit]
        ).fetchall()

    if not rows:
        return "Tasks: (empty)"

    lines = [f"Tasks ({len(rows)}):"]
    for row in rows:
        lines.append(f"  [{row['id'][:8]}] {row['title']} state={row['state']} owner={row['owner_agent']}")

    return "\n".join(lines)

def task_tree_impl(root_task_id: str, max_depth: int = 10) -> str:
    """Displays a task and its subtasks in a tree structure. Tombstoned tasks are hidden."""
    max_depth = max(1, min(max_depth, 20))

    with _db() as db:
        row = db.execute(
            "SELECT id, title, state, owner_agent FROM tasks WHERE id = ? AND deleted_at IS NULL",
            (root_task_id,)
        ).fetchone()

        if not row:
            return f"Error: task '{root_task_id}' not found"

        rows = db.execute(
            """WITH RECURSIVE subtree(id, title, state, owner_agent, parent_task_id, depth) AS (
                SELECT id, title, state, owner_agent, parent_task_id, 0
                  FROM tasks WHERE id = ? AND deleted_at IS NULL
                UNION ALL
                SELECT t.id, t.title, t.state, t.owner_agent, t.parent_task_id, s.depth + 1
                  FROM tasks t JOIN subtree s ON t.parent_task_id = s.id
                 WHERE s.depth + 1 <= ? AND t.deleted_at IS NULL
            )
            SELECT * FROM subtree ORDER BY depth, id""",
            (root_task_id, max_depth)
        ).fetchall()

    if not rows:
        return f"Error: task '{root_task_id}' not found"

    lines = [f"Task tree from {root_task_id[:8]} (max_depth={max_depth}):"]
    for row in rows:
        indent = "  " * row["depth"]
        owner_str = row["owner_agent"] or "-"
        lines.append(f"{indent}[{row['id'][:8]}] {row['title']} ({row['state']}, owner={owner_str})")

    return "\n".join(lines)
