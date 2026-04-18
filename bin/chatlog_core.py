"""chatlog_core.py — the load-bearing module for the chat log subsystem.

Provides:
- Async write queue (asyncio.Queue) with flush-on-size/interval
- Spill-to-disk backpressure at memory/chatlog_spill/YYYYMMDD.jsonl
- chatlog_write_impl / chatlog_write_bulk_impl — enqueue + flush
- chatlog_search_impl — delegates to memory_core.memory_search_scored_impl
- chatlog_promote_impl — ATTACH DATABASE cross-DB copy (separate/hybrid) or UPDATE (integrated)
- chatlog_list_conversations_impl
- chatlog_cost_report_impl — aggregates tokens/cost from metadata_json
- chatlog_set_redaction_impl / chatlog_rescrub_impl
- PRICE_TABLE for client-side cost computation
- atexit + SIGTERM drain on shutdown

All paths route through M3Context.get_chatlog_conn() so integrated/separate/hybrid
modes are handled transparently.
"""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import logging
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import chatlog_config
import chatlog_redaction

logger = logging.getLogger("chatlog_core")

VALID_ROLES = frozenset({"user", "assistant", "system", "tool"})
VALID_HOST_AGENTS = frozenset({"claude-code", "gemini-cli", "opencode", "aider"})
VALID_PROVIDERS = frozenset({
    "anthropic", "google", "openai", "local", "xai",
    "deepseek", "mistral", "meta", "other",
})
MAX_CONTENT_LEN = 50_000

# Price table — USD per 1M tokens. Unknown entries → cost_usd stays null (no fake zeros).
# Keys: (provider, model_id_prefix_or_exact). Lookup walks exact first, then prefix.
PRICE_TABLE: dict[tuple[str, str], dict[str, float]] = {
    ("anthropic", "claude-opus-4-7"):    {"in":  15.0, "out": 75.0},
    ("anthropic", "claude-opus-4-6"):    {"in":  15.0, "out": 75.0},
    ("anthropic", "claude-opus-4"):      {"in":  15.0, "out": 75.0},
    ("anthropic", "claude-sonnet-4-6"):  {"in":   3.0, "out": 15.0},
    ("anthropic", "claude-sonnet-4-5"):  {"in":   3.0, "out": 15.0},
    ("anthropic", "claude-sonnet-4"):    {"in":   3.0, "out": 15.0},
    ("anthropic", "claude-haiku-4-5"):   {"in":   1.0, "out":  5.0},
    ("anthropic", "claude-haiku-4"):     {"in":   1.0, "out":  5.0},
    ("google",    "gemini-2.5-pro"):     {"in":   1.25,"out": 10.0},
    ("google",    "gemini-2.5-flash"):   {"in":   0.30,"out":  2.50},
    ("google",    "gemini-2.0-flash"):   {"in":   0.10,"out":  0.40},
    ("openai",    "gpt-4.1"):            {"in":   2.0, "out":  8.0},
    ("openai",    "gpt-4o"):             {"in":   2.50,"out": 10.0},
    ("openai",    "o3"):                 {"in":   2.0, "out":  8.0},
    ("openai",    "o1"):                 {"in":  15.0, "out": 60.0},
    ("xai",       "grok-4"):             {"in":   3.0, "out": 15.0},
    ("deepseek",  "deepseek-chat"):      {"in":   0.27,"out":  1.10},
}


def compute_cost_usd(provider: str, model_id: str,
                     tokens_in: Optional[int], tokens_out: Optional[int]) -> Optional[float]:
    """Client-side cost computation from the price table.

    Returns None if provider/model not in table or token counts unavailable
    (never returns fake zero)."""
    if not model_id or tokens_in is None:
        return None
    row = PRICE_TABLE.get((provider, model_id))
    if row is None:
        # Prefix fallback: e.g. "claude-opus-4-7-20260101" → "claude-opus-4-7"
        for (p, m), r in PRICE_TABLE.items():
            if p == provider and model_id.startswith(m):
                row = r
                break
    if row is None:
        return None
    cost = (tokens_in / 1_000_000.0) * row["in"]
    if tokens_out:
        cost += (tokens_out / 1_000_000.0) * row["out"]
    return round(cost, 6)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _content_hash(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def _redaction_dict(spec) -> dict:
    """Normalize a RedactionSpec dataclass into the dict shape chatlog_redaction.scrub expects."""
    return {
        "enabled": spec.enabled,
        "patterns": list(spec.patterns),
        "custom_regex": list(spec.custom_regex),
        "redact_pii": spec.redact_pii,
        "store_original_hash": spec.store_original_hash,
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_write(item: dict) -> None:
    """Strict provenance validator. Raises ValueError on missing/invalid fields."""
    if not item.get("content") or not isinstance(item["content"], str):
        raise ValueError("content is required (non-empty string)")
    if len(item["content"]) > MAX_CONTENT_LEN:
        raise ValueError(f"content exceeds {MAX_CONTENT_LEN} chars")
    role = item.get("role")
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {sorted(VALID_ROLES)}")
    if not item.get("conversation_id"):
        raise ValueError("conversation_id is required")
    host = item.get("host_agent")
    if host not in VALID_HOST_AGENTS:
        raise ValueError(f"host_agent must be one of {sorted(VALID_HOST_AGENTS)}")
    provider = item.get("provider")
    if provider not in VALID_PROVIDERS:
        raise ValueError(f"provider must be one of {sorted(VALID_PROVIDERS)}")
    if not item.get("model_id"):
        raise ValueError("model_id is required (non-empty string)")


def _build_metadata(item: dict, scrubbed: bool = False,
                    redaction_count: int = 0, groups_fired: list[str] | None = None,
                    original_hash: Optional[str] = None) -> str:
    """Assemble metadata_json with full provenance + optional cost + optional redaction stamps."""
    meta: dict[str, Any] = {
        "role":            item["role"],
        "host_agent":      item["host_agent"],
        "provider":        item["provider"],
        "model_id":        item["model_id"],
    }
    # Optional turn index (nullable)
    if item.get("turn_index") is not None:
        meta["turn_index"] = int(item["turn_index"])
    # Optional cost/telemetry (nullable — never fake zeros)
    for k in ("tokens_in", "tokens_out", "cost_usd", "latency_ms"):
        if item.get(k) is not None:
            meta[k] = item[k]
    # Fill cost_usd client-side if missing but tokens are present and model priced
    if "cost_usd" not in meta and item.get("tokens_in") is not None:
        c = compute_cost_usd(
            item["provider"], item["model_id"],
            item.get("tokens_in"), item.get("tokens_out"),
        )
        if c is not None:
            meta["cost_usd"] = c
    # User-supplied metadata merges in (doesn't clobber required fields)
    extra = item.get("metadata")
    if isinstance(extra, str) and extra:
        try:
            extra = json.loads(extra)
        except json.JSONDecodeError:
            extra = {}
    if isinstance(extra, dict):
        for k, v in extra.items():
            if k not in meta:  # don't overwrite provenance
                meta[k] = v
    # Redaction stamps
    if scrubbed:
        meta["redacted"] = True
        meta["redaction_count"] = int(redaction_count)
        if groups_fired:
            meta["redaction_groups"] = groups_fired
        if original_hash:
            meta["original_content_sha256"] = original_hash
    return json.dumps(meta, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Async write queue + flush loop
# ---------------------------------------------------------------------------

_QUEUE: Optional[asyncio.Queue] = None
_FLUSH_TASK: Optional[asyncio.Task] = None
_LAST_FLUSH_TS: float = 0.0
_QUEUE_LOCK = asyncio.Lock() if False else None  # set lazily in _ensure_queue
_WROTE_COUNT = 0


def _ensure_queue() -> asyncio.Queue:
    """Create the queue + flush task on first use in this event loop."""
    global _QUEUE, _FLUSH_TASK, _LAST_FLUSH_TS
    cfg = chatlog_config.resolve_config()
    if _QUEUE is None:
        _QUEUE = asyncio.Queue(maxsize=cfg.queue_max_depth)
        _LAST_FLUSH_TS = time.time()
    if _FLUSH_TASK is None or _FLUSH_TASK.done():
        loop = asyncio.get_running_loop()
        _FLUSH_TASK = loop.create_task(_flush_loop(), name="chatlog_flush_loop")
    return _QUEUE


async def _flush_loop() -> None:
    """Drain the queue when size threshold hit or interval elapsed."""
    global _LAST_FLUSH_TS
    cfg = chatlog_config.resolve_config()
    interval_s = cfg.queue_flush_ms / 1000.0
    while True:
        try:
            await asyncio.sleep(min(interval_s / 2, 0.5))
            q = _QUEUE
            if q is None:
                continue
            now = time.time()
            age = now - _LAST_FLUSH_TS
            if q.qsize() >= cfg.queue_flush_rows or (q.qsize() > 0 and age >= interval_s):
                await _flush_once()
                _LAST_FLUSH_TS = time.time()
        except asyncio.CancelledError:
            # Drain everything on shutdown
            try:
                if _QUEUE is not None and _QUEUE.qsize() > 0:
                    await _flush_once()
            except Exception as e:
                logger.warning(f"Final flush on shutdown failed: {e}")
            raise
        except Exception as e:
            logger.exception(f"flush loop error: {e}")


async def _flush_once() -> int:
    """Drain up to queue_flush_rows items into a single transaction. Returns rows written."""
    global _WROTE_COUNT
    q = _QUEUE
    if q is None or q.qsize() == 0:
        return 0
    cfg = chatlog_config.resolve_config()
    batch: list[dict] = []
    while not q.empty() and len(batch) < cfg.queue_flush_rows:
        try:
            batch.append(q.get_nowait())
        except asyncio.QueueEmpty:
            break

    if not batch:
        return 0

    # Run SQLite insert off the event loop
    loop = asyncio.get_running_loop()
    try:
        written = await loop.run_in_executor(None, _executemany_insert, batch)
        _WROTE_COUNT += written
        _update_state_after_flush(q.qsize(), written)
        return written
    except Exception as e:
        logger.error(f"Flush failed, spilling {len(batch)} rows: {e}")
        _spill_batch(batch)
        return 0


def _executemany_insert(batch: list[dict]) -> int:
    """Synchronous bulk INSERT into the chatlog DB. Called from executor thread."""
    from m3_sdk import M3Context
    ctx = M3Context()
    rows = []
    for item in batch:
        rows.append((
            item["_id"],
            "chat_log",
            item["_title"],
            item["_content"],
            item["_metadata_json"],
            item.get("agent_id") or "",
            item["model_id"],
            item.get("change_agent") or "chatlog_ingest",
            0.3,  # importance — chat logs start low; promote bumps to 0.5+
            "chatlog",
            item.get("origin_device") or "",
            item.get("user_id") or "",
            item.get("scope") or "",
            item.get("expires_at"),
            item["_created_at"],
            item.get("valid_from") or item["_created_at"],
            item.get("valid_to"),
            item["conversation_id"],
            item.get("refresh_on"),
            item.get("refresh_reason"),
            _content_hash(item["_content"]),
            item.get("variant"),
        ))
    sql = (
        "INSERT INTO memory_items ("
        "id, type, title, content, metadata_json, agent_id, model_id, "
        "change_agent, importance, source, origin_device, user_id, scope, expires_at, "
        "created_at, valid_from, valid_to, conversation_id, refresh_on, refresh_reason, "
        "content_hash, variant) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    with ctx.get_chatlog_conn() as conn:
        conn.executemany(sql, rows)
        conn.commit()
    return len(rows)


def _spill_batch(batch: list[dict]) -> None:
    """Dump pending rows to memory/chatlog_spill/YYYYMMDD.jsonl. Sweeper picks up."""
    os.makedirs(chatlog_config.SPILL_DIR, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = os.path.join(chatlog_config.SPILL_DIR, f"{day}.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        for item in batch:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    _update_state_spill()


def _update_state_after_flush(queue_depth: int, written: int) -> None:
    """Atomic-rename the state file after a successful flush."""
    state = _read_state()
    state["queue_depth"] = queue_depth
    state["last_flush_at"] = _utcnow_iso()
    state["last_write_at"] = _utcnow_iso()
    state["total_written"] = int(state.get("total_written", 0)) + written
    _write_state(state)


def _update_state_spill() -> None:
    state = _read_state()
    # Recompute spill bytes
    total = 0
    oldest_mtime = None
    if os.path.isdir(chatlog_config.SPILL_DIR):
        for fn in os.listdir(chatlog_config.SPILL_DIR):
            fp = os.path.join(chatlog_config.SPILL_DIR, fn)
            if os.path.isfile(fp):
                total += os.path.getsize(fp)
                mt = os.path.getmtime(fp)
                if oldest_mtime is None or mt < oldest_mtime:
                    oldest_mtime = mt
    state["spill"] = {
        "bytes": total,
        "oldest_ms_ago": int((time.time() - oldest_mtime) * 1000) if oldest_mtime else None,
    }
    _write_state(state)


def _read_state() -> dict:
    try:
        with open(chatlog_config.STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_state(state: dict) -> None:
    """Atomic write: tmp file + os.replace."""
    os.makedirs(os.path.dirname(chatlog_config.STATE_FILE), exist_ok=True)
    tmp = chatlog_config.STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, chatlog_config.STATE_FILE)


# ---------------------------------------------------------------------------
# Public write API
# ---------------------------------------------------------------------------

async def chatlog_write_impl(
    content: str,
    role: str,
    conversation_id: str,
    host_agent: str,
    provider: str,
    model_id: str,
    turn_index: Optional[int] = None,
    agent_id: str = "",
    user_id: str = "",
    metadata: str = "{}",
    timestamp: str = "",
    tokens_in: Optional[int] = None,
    tokens_out: Optional[int] = None,
    cost_usd: Optional[float] = None,
    latency_ms: Optional[int] = None,
    embed: bool = False,  # reserved — always enqueued without embedding; sweeper handles
) -> str:
    """Enqueue a single chat log row. Returns the row id immediately."""
    item = {
        "role": role,
        "content": content,
        "conversation_id": conversation_id,
        "host_agent": host_agent,
        "provider": provider,
        "model_id": model_id,
        "turn_index": turn_index,
        "agent_id": agent_id,
        "user_id": user_id,
        "metadata": metadata,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost_usd,
        "latency_ms": latency_ms,
    }
    _validate_write(item)

    cfg = chatlog_config.resolve_config()

    # Redaction (OFF by default — fast path returns original content unchanged)
    scrubbed_content = content
    scrubbed = False
    rcount = 0
    groups_fired: list[str] = []
    original_hash: Optional[str] = None
    if cfg.redaction.enabled:
        scrubbed_content, rcount, groups_fired = chatlog_redaction.scrub(
            content, _redaction_dict(cfg.redaction),
        )
        if rcount > 0:
            scrubbed = True
            if cfg.redaction.store_original_hash:
                original_hash = _content_hash(content)

    row_id = str(uuid.uuid4())
    title = _derive_title(role, scrubbed_content, host_agent)
    meta_json = _build_metadata(
        item, scrubbed=scrubbed, redaction_count=rcount,
        groups_fired=groups_fired, original_hash=original_hash,
    )
    now = timestamp or _utcnow_iso()

    queued = {
        **item,
        "_id": row_id,
        "_title": title,
        "_content": scrubbed_content,
        "_metadata_json": meta_json,
        "_created_at": now,
    }

    q = _ensure_queue()
    try:
        q.put_nowait(queued)
    except asyncio.QueueFull:
        # Backpressure: spill this single item + drain any in-flight
        _spill_batch([queued])
    return row_id


async def chatlog_write_bulk_impl(items: list[dict], embed: bool = False) -> dict:
    """Validate + enqueue N rows. Returns {'written_ids': [...], 'spilled': K, 'failed': K, 'errors': [...]}."""
    ids: list[str] = []
    spilled = 0
    failed = 0
    errors: list[str] = []
    cfg = chatlog_config.resolve_config()
    q = _ensure_queue()

    for item in items:
        try:
            _validate_write(item)
        except ValueError as e:
            failed += 1
            errors.append(str(e))
            continue

        content = item["content"]
        scrubbed_content = content
        scrubbed = False
        rcount = 0
        groups_fired: list[str] = []
        original_hash: Optional[str] = None
        if cfg.redaction.enabled:
            scrubbed_content, rcount, groups_fired = chatlog_redaction.scrub(
                content, _redaction_dict(cfg.redaction),
            )
            if rcount > 0:
                scrubbed = True
                if cfg.redaction.store_original_hash:
                    original_hash = _content_hash(content)

        row_id = str(uuid.uuid4())
        title = _derive_title(item["role"], scrubbed_content, item["host_agent"])
        meta_json = _build_metadata(
            item, scrubbed=scrubbed, redaction_count=rcount,
            groups_fired=groups_fired, original_hash=original_hash,
        )
        now = item.get("timestamp") or _utcnow_iso()

        queued = {
            **item,
            "_id": row_id,
            "_title": title,
            "_content": scrubbed_content,
            "_metadata_json": meta_json,
            "_created_at": now,
        }
        ids.append(row_id)

        try:
            q.put_nowait(queued)
        except asyncio.QueueFull:
            _spill_batch([queued])
            spilled += 1

    return {
        "written_ids": ids,
        "spilled": spilled,
        "failed": failed,
        "errors": errors,
        "queue_depth": q.qsize(),
    }


def _derive_title(role: str, content: str, host_agent: str) -> str:
    """Short, greppable title: '<role>@<host_agent>: <first 60 chars>'."""
    snippet = (content or "").strip().replace("\n", " ")[:60]
    return f"{role}@{host_agent}: {snippet}"


# ---------------------------------------------------------------------------
# Search — delegates to memory_core.memory_search_scored_impl
# ---------------------------------------------------------------------------

async def chatlog_search_impl(
    query: str,
    k: int = 8,
    conversation_id: str = "",
    host_agent: str = "",
    provider: str = "",
    model_id: str = "",
    agent_id: str = "",
    search_mode: str = "hybrid",
    since: str = "",
    until: str = "",
) -> str:
    """Keyword/vector/hybrid search over chat_log rows in the configured chat log DB.

    Returns a JSON string with {"results": [...], "count": N, "db_path": "...", "mode": "..."}."""
    import memory_core

    # Ensure flushes complete before searching (best-effort)
    await _flush_once()

    db_path = chatlog_config.chatlog_db_path()
    mode = chatlog_config.chatlog_mode()

    # memory_search_scored_impl reads the primary DB via the shared M3Context pool.
    # In integrated mode, that's correct. In separate/hybrid, we need to point it at
    # the chatlog DB — we do that by temporarily overriding the DB path for the search.
    if mode == "integrated":
        # Direct delegation
        results_json = await memory_core.memory_search_scored_impl(
            query=query, k=k, type_filter="chat_log",
            conversation_id=conversation_id, agent_id=agent_id,
            search_mode=search_mode, since=since, until=until,
        )
        return json.dumps({
            "results": json.loads(results_json).get("results", []),
            "count":   json.loads(results_json).get("count", 0),
            "db_path": db_path,
            "mode":    "integrated",
        })

    # Separate/hybrid: run a lightweight FTS+filter query against the chat log DB directly.
    # (Full vector/hybrid on the separate DB is out of scope for v1 — keyword-only is the
    # dominant path for chat log lookup by role/conversation/time.)
    return await _chatlog_search_separate(
        query=query, k=k, conversation_id=conversation_id,
        host_agent=host_agent, provider=provider, model_id=model_id,
        agent_id=agent_id, since=since, until=until, db_path=db_path,
    )


async def _chatlog_search_separate(
    query: str, k: int, conversation_id: str, host_agent: str,
    provider: str, model_id: str, agent_id: str, since: str, until: str,
    db_path: str,
) -> str:
    from m3_sdk import M3Context
    ctx = M3Context()

    def _run() -> dict:
        clauses = ["mi.type='chat_log'", "mi.is_deleted=0"]
        params: list[Any] = []
        if conversation_id:
            clauses.append("mi.conversation_id=?")
            params.append(conversation_id)
        if host_agent:
            clauses.append("json_extract(mi.metadata_json,'$.host_agent')=?")
            params.append(host_agent)
        if provider:
            clauses.append("json_extract(mi.metadata_json,'$.provider')=?")
            params.append(provider)
        if model_id:
            clauses.append("mi.model_id=?")
            params.append(model_id)
        if agent_id:
            clauses.append("mi.agent_id=?")
            params.append(agent_id)
        if since:
            clauses.append("mi.created_at>=?")
            params.append(since)
        if until:
            clauses.append("mi.created_at<=?")
            params.append(until)

        where = " AND ".join(clauses)

        with ctx.get_chatlog_conn() as conn:
            if query.strip():
                sql = (
                    "SELECT mi.id, mi.title, mi.content, mi.metadata_json, mi.created_at, "
                    "       mi.conversation_id, mi.model_id "
                    "FROM memory_items_fts f "
                    "JOIN memory_items mi ON mi.rowid = f.rowid "
                    f"WHERE memory_items_fts MATCH ? AND {where} "
                    "ORDER BY rank LIMIT ?"
                )
                rows = conn.execute(sql, [query] + params + [k]).fetchall()
            else:
                sql = (
                    "SELECT id, title, content, metadata_json, created_at, "
                    "       conversation_id, model_id "
                    f"FROM memory_items mi WHERE {where} "
                    "ORDER BY created_at DESC LIMIT ?"
                )
                rows = conn.execute(sql, params + [k]).fetchall()

            results = []
            for r in rows:
                try:
                    meta = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
                except json.JSONDecodeError:
                    meta = {}
                results.append({
                    "id": r["id"],
                    "title": r["title"],
                    "content": r["content"],
                    "created_at": r["created_at"],
                    "conversation_id": r["conversation_id"],
                    "model_id": r["model_id"],
                    "metadata": meta,
                })
            return {"results": results, "count": len(results)}

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _run)
    return json.dumps({
        **result,
        "db_path": db_path,
        "mode": chatlog_config.chatlog_mode(),
    })


# ---------------------------------------------------------------------------
# Promote: copy/move chat_log rows into main DB
# ---------------------------------------------------------------------------

async def chatlog_promote_impl(
    ids: Optional[list[str]] = None,
    conversation_id: str = "",
    since: str = "",
    until: str = "",
    copy: bool = True,
    target_type: str = "conversation",
) -> str:
    """Promote chat_log rows from chatlog DB into main DB with a new type.

    - In integrated mode: UPDATE type WHERE ...
    - In separate/hybrid mode: ATTACH main DB and INSERT ... SELECT; when copy=False, also DELETE.
    Returns JSON: {"promoted": N, "ids": [...], "mode": "..."}.
    """
    from m3_sdk import M3Context

    mode = chatlog_config.chatlog_mode()

    def _run() -> dict:
        ctx = M3Context()

        clauses = ["type='chat_log'", "is_deleted=0"]
        params: list[Any] = []
        if ids:
            placeholders = ",".join("?" for _ in ids)
            clauses.append(f"id IN ({placeholders})")
            params.extend(ids)
        if conversation_id:
            clauses.append("conversation_id=?")
            params.append(conversation_id)
        if since:
            clauses.append("created_at>=?")
            params.append(since)
        if until:
            clauses.append("created_at<=?")
            params.append(until)
        if not ids and not conversation_id and not since and not until:
            raise ValueError("promote requires ids, conversation_id, since, or until")
        where = " AND ".join(clauses)

        if mode == "integrated":
            # Same DB — just flip type
            with ctx.get_sqlite_conn() as conn:
                rows = conn.execute(
                    f"SELECT id FROM memory_items WHERE {where}", params,
                ).fetchall()
                row_ids = [r["id"] for r in rows]
                if row_ids:
                    conn.execute(
                        f"UPDATE memory_items SET type=?, updated_at=? WHERE {where}",
                        [target_type, _utcnow_iso()] + params,
                    )
                    conn.commit()
                return {"promoted": len(row_ids), "ids": row_ids, "mode": "integrated"}

        # Separate/hybrid: ATTACH the main DB onto a chatlog connection and copy
        main_path = chatlog_config.MAIN_DB_PATH
        with ctx.get_chatlog_conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM memory_items WHERE {where}", params,
            ).fetchall()
            row_ids = [r["id"] for r in rows]
            if not row_ids:
                return {"promoted": 0, "ids": [], "mode": mode}

            conn.execute("ATTACH DATABASE ? AS main_db", (main_path,))
            try:
                # Main DB has more columns than chatlog — copy by explicit column list
                col_names = [
                    "id", "type", "title", "content", "metadata_json", "agent_id", "model_id",
                    "change_agent", "importance", "source", "origin_device", "user_id", "scope",
                    "expires_at", "created_at", "valid_from", "valid_to", "conversation_id",
                    "refresh_on", "refresh_reason", "content_hash", "variant",
                ]
                cols_sql = ", ".join(col_names)
                placeholders = ",".join("?" for _ in row_ids)
                # Override type via the SELECT
                ", ".join(
                    "? as type" if c == "type" else c for c in col_names
                )
                # Simpler: build rows in Python, insert into main with target_type
                for r in rows:
                    values = []
                    for c in col_names:
                        if c == "type":
                            values.append(target_type)
                        else:
                            try:
                                values.append(r[c])
                            except (KeyError, IndexError):
                                values.append(None)
                    conn.execute(
                        f"INSERT OR IGNORE INTO main_db.memory_items ({cols_sql}) "
                        f"VALUES ({','.join('?' for _ in col_names)})",
                        values,
                    )

                if not copy:
                    conn.execute(
                        f"DELETE FROM memory_items WHERE id IN ({placeholders})",
                        row_ids,
                    )
                conn.commit()
            finally:
                conn.execute("DETACH DATABASE main_db")

            return {"promoted": len(row_ids), "ids": row_ids, "mode": mode}

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _run)
    return json.dumps(result)


# ---------------------------------------------------------------------------
# List conversations
# ---------------------------------------------------------------------------

def chatlog_list_conversations_impl(
    host_agent: str = "", limit: int = 50, offset: int = 0,
) -> str:
    """List distinct conversation_ids with counts/timespans."""
    from m3_sdk import M3Context
    ctx = M3Context()

    clauses = ["type='chat_log'", "is_deleted=0", "conversation_id IS NOT NULL"]
    params: list[Any] = []
    if host_agent:
        clauses.append("json_extract(metadata_json,'$.host_agent')=?")
        params.append(host_agent)
    where = " AND ".join(clauses)
    sql = (
        "SELECT conversation_id, COUNT(*) AS turns, "
        "MIN(created_at) AS first_at, MAX(created_at) AS last_at, "
        "json_extract(metadata_json,'$.host_agent') AS host_agent, "
        "json_extract(metadata_json,'$.model_id') AS model_id "
        f"FROM memory_items WHERE {where} "
        "GROUP BY conversation_id "
        "ORDER BY last_at DESC LIMIT ? OFFSET ?"
    )
    with ctx.get_chatlog_conn() as conn:
        rows = conn.execute(sql, params + [limit, offset]).fetchall()
    out = [
        {
            "conversation_id": r["conversation_id"],
            "turns":           r["turns"],
            "first_at":        r["first_at"],
            "last_at":         r["last_at"],
            "host_agent":      r["host_agent"],
            "model_id":        r["model_id"],
        }
        for r in rows
    ]
    return json.dumps({"conversations": out, "count": len(out)})


# ---------------------------------------------------------------------------
# Cost report
# ---------------------------------------------------------------------------

_VALID_GROUPBY = frozenset({"provider", "model_id", "host_agent", "conversation_id", "day"})


async def chatlog_cost_report_impl(
    since: str = "",
    until: str = "",
    group_by: str = "model_id",
) -> str:
    """Aggregate tokens/cost from chat_log rows' metadata_json.

    Null cost_usd rows are EXCLUDED from sums (never treated as zero).
    """
    if group_by not in _VALID_GROUPBY:
        raise ValueError(f"group_by must be one of {sorted(_VALID_GROUPBY)}")

    from m3_sdk import M3Context
    ctx = M3Context()

    if group_by == "day":
        gcol = "substr(created_at,1,10)"
    elif group_by == "conversation_id":
        gcol = "conversation_id"
    elif group_by == "model_id":
        gcol = "model_id"
    else:
        gcol = f"json_extract(metadata_json,'$.{group_by}')"

    clauses = ["type='chat_log'", "is_deleted=0"]
    params: list[Any] = []
    if since:
        clauses.append("created_at>=?")
        params.append(since)
    if until:
        clauses.append("created_at<=?")
        params.append(until)
    where = " AND ".join(clauses)

    sql = (
        f"SELECT {gcol} AS bucket, "
        "COUNT(*) AS rows, "
        "SUM(CAST(json_extract(metadata_json,'$.tokens_in') AS INTEGER)) AS tokens_in, "
        "SUM(CAST(json_extract(metadata_json,'$.tokens_out') AS INTEGER)) AS tokens_out, "
        "SUM(CASE WHEN json_extract(metadata_json,'$.cost_usd') IS NOT NULL "
        "         THEN CAST(json_extract(metadata_json,'$.cost_usd') AS REAL) END) AS cost_usd, "
        "SUM(CASE WHEN json_extract(metadata_json,'$.cost_usd') IS NOT NULL THEN 1 END) AS priced_rows "
        f"FROM memory_items WHERE {where} "
        "GROUP BY bucket ORDER BY bucket"
    )

    def _run() -> list[dict]:
        with ctx.get_chatlog_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "bucket":      r["bucket"],
                "rows":        r["rows"],
                "tokens_in":   r["tokens_in"],
                "tokens_out":  r["tokens_out"],
                "cost_usd":    r["cost_usd"],
                "priced_rows": r["priced_rows"],
            }
            for r in rows
        ]

    loop = asyncio.get_running_loop()
    buckets = await loop.run_in_executor(None, _run)
    return json.dumps({"group_by": group_by, "buckets": buckets})


# ---------------------------------------------------------------------------
# Redaction control
# ---------------------------------------------------------------------------

def chatlog_set_redaction_impl(
    enabled: bool,
    patterns: Optional[list[str]] = None,
    redact_pii: Optional[bool] = None,
    custom_regex: Optional[list[str]] = None,
    store_original_hash: Optional[bool] = None,
) -> str:
    """Flip redaction flags at runtime and persist to config file."""
    cfg = chatlog_config.resolve_config()
    cfg.redaction.enabled = bool(enabled)
    if patterns is not None:
        cfg.redaction.patterns = list(patterns)
    if redact_pii is not None:
        cfg.redaction.redact_pii = bool(redact_pii)
    if custom_regex is not None:
        cfg.redaction.custom_regex = list(custom_regex)
    if store_original_hash is not None:
        cfg.redaction.store_original_hash = bool(store_original_hash)
    chatlog_config.save_config(cfg)
    chatlog_config.invalidate_cache()
    return json.dumps({"ok": True, "redaction": _redaction_dict(cfg.redaction)})


async def chatlog_rescrub_impl(
    conversation_id: str = "",
    since: str = "",
    until: str = "",
    limit: int = 10_000,
) -> str:
    """Re-scrub existing rows. Requires redaction.enabled=true."""
    cfg = chatlog_config.resolve_config()
    if not cfg.redaction.enabled:
        raise ValueError("redaction.enabled must be true before rescrub")
    red_dict = _redaction_dict(cfg.redaction)

    from m3_sdk import M3Context
    ctx = M3Context()

    clauses = ["type='chat_log'", "is_deleted=0"]
    params: list[Any] = []
    if conversation_id:
        clauses.append("conversation_id=?")
        params.append(conversation_id)
    if since:
        clauses.append("created_at>=?")
        params.append(since)
    if until:
        clauses.append("created_at<=?")
        params.append(until)
    where = " AND ".join(clauses)

    def _run() -> dict:
        updated = 0
        matched_rows = 0
        with ctx.get_chatlog_conn() as conn:
            rows = conn.execute(
                f"SELECT id, content, metadata_json FROM memory_items WHERE {where} LIMIT ?",
                params + [limit],
            ).fetchall()
            for r in rows:
                if not r["content"]:
                    continue
                scrubbed, count, groups = chatlog_redaction.scrub(r["content"], red_dict)
                if count == 0:
                    continue
                matched_rows += 1
                try:
                    meta = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
                except json.JSONDecodeError:
                    meta = {}
                if not meta.get("redacted"):
                    meta["original_content_sha256"] = _content_hash(r["content"])
                meta["redacted"] = True
                meta["redaction_count"] = int(meta.get("redaction_count", 0)) + count
                meta["redaction_groups"] = sorted(set(
                    (meta.get("redaction_groups") or []) + groups
                ))
                conn.execute(
                    "UPDATE memory_items SET content=?, metadata_json=?, updated_at=? WHERE id=?",
                    (scrubbed, json.dumps(meta, ensure_ascii=False), _utcnow_iso(), r["id"]),
                )
                updated += 1
            conn.commit()
        return {"matched_rows": matched_rows, "updated": updated}

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _run)
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Shutdown drain
# ---------------------------------------------------------------------------

def _final_drain_sync() -> None:
    """atexit hook — runs a final synchronous flush if the loop is still up."""
    try:
        q = _QUEUE
        if q is None or q.qsize() == 0:
            return
        # Drain on the current loop if available; otherwise create a short-lived one
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Can't run synchronously against a running loop — the flush_loop
                # is responsible. Best effort: nudge it.
                return
        except RuntimeError:
            loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_flush_once())
        finally:
            if not loop.is_running():
                loop.close()
    except Exception as e:
        logger.warning(f"final drain failed: {e}")


atexit.register(_final_drain_sync)


def _install_sigterm_handler() -> None:
    """Best-effort SIGTERM drain. No-op on Windows where SIGTERM is limited."""
    try:
        def _handler(signum, frame):
            _final_drain_sync()
            sys.exit(0)
        if sys.platform != "win32":
            signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError):
        # Signals can only be set from the main thread of the main interpreter
        pass


_install_sigterm_handler()


if __name__ == "__main__":
    # Smoke test
    async def _smoke():
        r = await chatlog_write_impl(
            content="hello from smoke test",
            role="user",
            conversation_id="smoke-1",
            host_agent="claude-code",
            provider="anthropic",
            model_id="claude-opus-4-7",
            tokens_in=5,
        )
        print(f"wrote id={r}")
        # Force flush
        await _flush_once()
        # Read back
        out = await chatlog_search_impl(query="smoke", k=5)
        print(out)

    asyncio.run(_smoke())
