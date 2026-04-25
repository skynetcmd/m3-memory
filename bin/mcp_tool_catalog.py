"""
mcp_tool_catalog.py — single source of truth for the m3-memory MCP tool catalog.

Imported by:
  - bin/memory_bridge.py (FastMCP stdio server — registers each spec via @mcp.tool())
  - examples/multi-agent-team/dispatch.py (orchestrator-side dispatch loop)

Zero FastMCP dependency. Pure Python + memory_core + memory_sync + memory_maintenance.
Never import this module from those modules — that would create a cycle.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

import chatlog_core
import chatlog_status
import memory_core
import memory_maintenance
import memory_sync
from m3_sdk import active_database

# ── Validation Constants (hoisted from memory_bridge.py) ─────────────────────
MAX_CONTENT_SIZE = 50_000
MAX_QUERY_LENGTH = 2_000
MAX_K = 100
VALID_MEMORY_TYPES = frozenset({
    "note", "fact", "decision", "preference", "conversation", "message",
    "task", "code", "config", "observation", "plan", "summary", "snippet",
    "reference", "log", "home", "user_fact", "scratchpad", "auto",
    "knowledge", "event_extraction", "fact_enriched", "chat_log",
})

# Entity-graph enums — defined in memory_core to avoid circular import
# (mcp_tool_catalog imports memory_core, not vice versa). Re-exported here
# so callers see a single import surface.
VALID_ENTITY_TYPES = memory_core.VALID_ENTITY_TYPES
VALID_ENTITY_PREDICATES = memory_core.VALID_ENTITY_PREDICATES

# ── Dataclass ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict
    impl: Callable
    is_async: bool = False
    validators: tuple = ()
    default_allowed: bool = True
    inject_agent_id: bool = False

# ── Validators ───────────────────────────────────────────────────────────────
def _memory_write_validator(args: dict) -> Any:
    t = args.get("type", "")
    if t not in VALID_MEMORY_TYPES:
        return f"Error: invalid memory type '{t}'. Valid types: {', '.join(sorted(VALID_MEMORY_TYPES))}"
    content = args.get("content", "") or ""
    if content and len(content) > MAX_CONTENT_SIZE:
        return f"Error: content too large ({len(content)} chars). Maximum is {MAX_CONTENT_SIZE}."
    md = args.get("metadata", "{}")
    if isinstance(md, dict):
        args["metadata"] = json.dumps(md)
    elif isinstance(md, str) and md and md != "{}":
        try:
            json.loads(md)
        except (ValueError, json.JSONDecodeError):
            return "Error: metadata is not valid JSON."
    if t == "auto":
        args["auto_classify"] = True
    return args

def _memory_search_validator(args: dict) -> Any:
    q = args.get("query", "")
    if not q or not str(q).strip():
        return "Error: query cannot be empty."
    q = str(q)
    if len(q) > MAX_QUERY_LENGTH:
        q = q[:MAX_QUERY_LENGTH]
    args["query"] = q
    try:
        k = int(args.get("k", 8))
    except (TypeError, ValueError):
        k = 8
    args["k"] = max(1, min(k, MAX_K))
    return args

def _variant_gate(args: dict) -> dict:
    """Bench-data gate for memory_search / memory_suggest. If include_bench_data=True,
    drop the variant filter; otherwise default to '__none__' so bench rows hide."""
    include_bench = bool(args.pop("include_bench_data", False))
    if include_bench:
        args["variant"] = ""
    elif not args.get("variant"):
        args["variant"] = "__none__"
    return args

def _memory_search_gated_validator(args: dict) -> Any:
    r = _memory_search_validator(args)
    if isinstance(r, str):
        return r
    return _variant_gate(r)

def _memory_suggest_validator(args: dict) -> Any:
    return _variant_gate(args)

def _memory_update_validator(args: dict) -> Any:
    md = args.get("metadata", "")
    if isinstance(md, dict):
        args["metadata"] = json.dumps(md)
    return args

def _memory_set_retention_validator(args: dict) -> Any:
    try:
        args["max_memories"] = int(args.get("max_memories", 1000))
        args["ttl_days"]     = int(args.get("ttl_days", 0))
        args["auto_archive"] = int(args.get("auto_archive", 1))
    except (TypeError, ValueError):
        return "Error: max_memories, ttl_days, and auto_archive must be integers."
    return args

def _gdpr_user_id_validator(args: dict) -> Any:
    uid = args.get("user_id", "")
    if not uid or not str(uid).strip():
        return "Error: user_id is required."
    args["user_id"] = str(uid).strip()
    return args

# ── Helpers ──────────────────────────────────────────────────────────────────
def get_tool(name: str) -> ToolSpec | None:
    return _BY_NAME.get(name)

def default_allowlist() -> set[str]:
    return {t.name for t in TOOLS if t.default_allowed}

def _pop_database(args: dict) -> str | None:
    """Pop the universal `database` arg so validators/impls never see it.

    The field is injected into every ToolSpec.parameters at module end (see
    ``_inject_database_arg``); MCP clients and direct Python callers can pass
    it to target a non-default SQLite DB. Empty string is treated as absent.
    """
    db = args.pop("database", None)
    if isinstance(db, str):
        db = db.strip() or None
    return db


def validate_args(spec: ToolSpec, args: dict) -> tuple[dict, str | None]:
    for v in spec.validators:
        result = v(args)
        if isinstance(result, str) and result.startswith("Error:"):
            return args, result
        if isinstance(result, dict):
            args = result
    return args, None

async def execute_tool(spec: ToolSpec, args: dict, agent_id: str) -> str:
    try:
        allowed_keys = set(spec.parameters.get("properties", {}).keys())
        args = {k: v for k, v in (args or {}).items() if k in allowed_keys}
        database = _pop_database(args)
        if spec.inject_agent_id and "agent_id" in allowed_keys:
            args["agent_id"] = agent_id
        args, err = validate_args(spec, args)
        if err:
            return err
        with active_database(database):
            if spec.is_async:
                result = await spec.impl(**args)
            else:
                result = spec.impl(**args)
        return result if isinstance(result, str) else str(result)
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"

# ── Inline impl wrapper for conversation_search ──────────────────────────────
async def _conversation_search_impl(query, k=8):
    """Search messages with automatic adjacent-turn pairing.

    When a user turn is found, the next assistant turn from the same
    conversation is included so callers always see the full Q&A pair.
    """
    ranked = await memory_core.memory_search_scored_impl(
        query, k=int(k), type_filter="message",
        extra_columns=["metadata_json", "conversation_id"],
    )
    if ranked is None:
        return "Search failed: FTS and semantic both unavailable."
    if not ranked:
        return "No results found."

    # Build initial result set
    items = []
    seen_ids: set = set()
    for score, item in ranked:
        item["score"] = score
        if "metadata_json" in item:
            item["_meta"] = json.loads(item.get("metadata_json") or "{}")
        else:
            item["_meta"] = {}
        items.append(item)
        seen_ids.add(item["id"])

    # Adjacent-turn pairing: pull the next turn for user messages
    extras = []
    for item in items:
        m = item.get("_meta", {})
        cid = item.get("conversation_id")
        if m.get("role") == "user" and "turn_index" in m and cid:
            next_idx = m["turn_index"] + 1
            with memory_core._db() as db:
                row = db.execute(
                    "SELECT id, content, title, metadata_json, conversation_id "
                    "FROM memory_items "
                    "WHERE conversation_id = ? AND is_deleted = 0 "
                    "  AND json_extract(metadata_json, '$.turn_index') = ?",
                    (cid, next_idx),
                ).fetchone()
                if row and row["id"] not in seen_ids:
                    seen_ids.add(row["id"])
                    rm = json.loads(row["metadata_json"] or "{}")
                    extras.append({
                        "id": row["id"],
                        "content": row["content"],
                        "title": row["title"],
                        "type": "message",
                        "conversation_id": row["conversation_id"],
                        "score": item["score"] * 0.85,
                        "_meta": rm,
                    })
    items.extend(extras)
    items.sort(key=lambda x: x.get("score", 0), reverse=True)

    # Format output
    lines = [f"Top {len(items)} results:"]
    for rank, item in enumerate(items, 1):
        content = item.get("content") or ""
        lines.append("-" * 40)
        lines.append(f"{rank}. [{item['id']}] score={item['score']:.4f}  type: {item.get('type', 'unknown')}  title: {item.get('title','')}")
        lines.append(f"Content:\n{content}\n")
    lines.append("-" * 40)
    return "\n".join(lines)

# ── Inline impl wrapper for memory_verify ────────────────────────────────────
# The LLM-facing parameter is `id` (preserves the existing bridge contract);
# memory_core.memory_verify_impl uses `memory_id`. Translate here.
def _memory_verify_impl(id):
    return memory_core.memory_verify_impl(id)

# ── TOOLS catalog ────────────────────────────────────────────────────────────
TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="memory_write",
        description=(
            "Creates a MemoryItem and optionally embeds it for semantic search. "
            "Contradiction detection is automatic — if new content conflicts with an existing "
            "memory of the same type/title, the old one is superseded. "
            "Use type='auto' to let the LLM decide the best category."
        ),
        parameters={
            "type": "object",
            "properties": {
                "type":          {"type": "string", "description": f"Memory type. One of: {', '.join(sorted(VALID_MEMORY_TYPES))}."},
                "content":       {"type": "string", "description": "Memory body (max 50000 chars)."},
                "title":         {"type": "string", "description": "Short title.", "default": ""},
                "metadata":      {"type": "string", "description": "JSON-encoded metadata object.", "default": "{}"},
                "agent_id":      {"type": "string", "description": "Owning agent id. Injected by the orchestrator.", "default": ""},
                "model_id":      {"type": "string", "description": "Originating model id.", "default": ""},
                "change_agent":  {"type": "string", "description": "Agent causing the write (audit).", "default": ""},
                "importance":    {"type": "number", "description": "0.0-1.0 relevance.", "default": 0.5},
                "source":        {"type": "string", "description": "Provenance tag.", "default": "agent"},
                "embed":         {"type": "boolean", "description": "Embed for semantic search.", "default": True},
                "user_id":       {"type": "string", "description": "Data subject id.", "default": ""},
                "scope":         {"type": "string", "description": "Isolation scope.", "default": "agent"},
                "valid_from":    {"type": "string", "description": "ISO-8601 validity start.", "default": ""},
                "valid_to":      {"type": "string", "description": "ISO-8601 validity end.", "default": ""},
                "auto_classify": {"type": "boolean", "description": "Let the LLM pick the type (forced true if type='auto').", "default": False},
                "conversation_id": {"type": "string", "description": "Groups this memory with a conversation / team session. Same ID space as conversation_start.", "default": ""},
                "refresh_on":    {"type": "string", "description": "ISO-8601 timestamp when this memory should be flagged for review (lifecycle / planned obsolescence).", "default": ""},
                "refresh_reason": {"type": "string", "description": "Why this memory needs refreshing (e.g., 'quarterly policy review').", "default": ""},
                "variant":       {"type": "string", "description": "Pipeline identifier for A/B variant tracking.", "default": ""},
                "embed_text":    {"type": "string", "description": "Override text used for embedding; falls back to content when empty.", "default": ""},
            },
            "required": ["type", "content"],
        },
        impl=memory_core.memory_write_impl,
        is_async=True,
        validators=(_memory_write_validator,),
        default_allowed=True,
        inject_agent_id=True,
    ),
    ToolSpec(
        name="memory_search",
        description="Search across memory items using semantic similarity or keyword matching. Filter by user_id and scope for isolation.",
        parameters={
            "type": "object",
            "properties": {
                "query":              {"type": "string", "description": "Search query."},
                "k":                  {"type": "integer", "description": "Max results (1-100).", "default": 8},
                "type_filter":        {"type": "string", "description": "Restrict to a memory type.", "default": ""},
                "agent_filter":       {"type": "string", "description": "Restrict to an agent id.", "default": ""},
                "search_mode":        {"type": "string", "enum": ["hybrid", "semantic", "keyword"], "description": "Retrieval mode.", "default": "hybrid"},
                "include_scratchpad": {"type": "boolean", "description": "Include ephemeral scratchpad items.", "default": False},
                "user_id":            {"type": "string", "description": "Filter by data subject.", "default": ""},
                "scope":              {"type": "string", "description": "Filter by isolation scope.", "default": ""},
                "as_of":              {"type": "string", "description": "ISO-8601 time-travel cutoff.", "default": ""},
                "conversation_id":    {"type": "string", "description": "Restrict to a conversation / team session.", "default": ""},
                "recency_bias":       {"type": "number", "description": "Boost newer items (0.0=off, 0.1-0.2=moderate, higher=aggressive). Useful for 'current' or 'latest' queries.", "default": 0.0},
                "adaptive_k":         {"type": "boolean", "description": "Auto-trim results at the score drop-off point, returning only high-relevance items.", "default": False},
                "variant":            {"type": "string", "description": "Ingest-pipeline filter. '' = real user data only (default, equivalent to IS NULL). Pass a specific variant name (e.g. 'heuristic_c1c4') to scope to that bench ingest.", "default": ""},
                "include_bench_data": {"type": "boolean", "description": "Opt in to LOCOMO / LongMemEval bench rows. Default False hides any row with a variant tag.", "default": False},
            },
            "required": ["query"],
        },
        impl=memory_core.memory_search_impl,
        is_async=True,
        validators=(_memory_search_gated_validator,),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_suggest",
        description="Preview which memories would be retrieved for a query, with score breakdowns explaining why each was selected.",
        parameters={
            "type": "object",
            "properties": {
                "query":              {"type": "string", "description": "Search query."},
                "k":                  {"type": "integer", "description": "Max results to preview.", "default": 5},
                "variant":            {"type": "string", "description": "Ingest-pipeline filter. Default '__none__' = real user data only.", "default": "__none__"},
                "include_bench_data": {"type": "boolean", "description": "Opt in to bench rows. Default False.", "default": False},
            },
            "required": ["query"],
        },
        impl=memory_core.memory_suggest_impl,
        is_async=True,
        validators=(_memory_suggest_validator,),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_search_routed",
        description="Temporal-aware routed retrieval. Routes temporal queries to verbatim search at k+temporal_k_bump; non-temporal queries to (optionally fact-fused) max-kind search at k. Pass fact_variant for two-tier fact-fusion. Optional graph_depth and expand_sessions add post-retrieval neighbor expansion.",
        parameters={
            "type": "object",
            "properties": {
                "query":           {"type": "string", "description": "Search query."},
                "k":               {"type": "integer", "default": 10, "description": "Top-K to return."},
                "fact_variant":    {"type": "string", "default": "", "description": "Optional fact-tier variant to fuse with base. Empty = single-variant."},
                "temporal_k_bump": {"type": "integer", "default": 5, "description": "Extra slots added when query is temporal."},
                "graph_depth":     {"type": "integer", "default": 0, "description": "If > 0, traverse memory_relationships up to N hops from each top-K hit and re-fuse. Clamped to 3."},
                "expand_sessions": {"type": "boolean", "default": False, "description": "If true, pull all turns sharing each top-K hit's conversation_id (capped at session_cap) and re-fuse."},
                "session_cap":     {"type": "integer", "default": 12, "description": "Per-session turn cap when expand_sessions=true."},
                "user_id":         {"type": "string", "default": ""},
                "scope":           {"type": "string", "default": ""},
                "type_filter":     {"type": "string", "default": ""},
                "agent_filter":    {"type": "string", "default": ""},
                "search_mode":     {"type": "string", "default": "hybrid"},
                "variant":         {"type": "string", "default": ""},
                "as_of":           {"type": "string", "default": ""},
                "conversation_id": {"type": "string", "default": ""},
                "explain":         {"type": "boolean", "default": False},
            },
            "required": ["query"],
        },
        impl=memory_core.memory_search_routed_impl,
        is_async=True,
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_get",
        description="Retrieves a full MemoryItem by UUID.",
        parameters={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Memory item UUID."},
            },
            "required": ["id"],
        },
        impl=memory_core.memory_get_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_update",
        description="Updates a MemoryItem by ID.",
        parameters={
            "type": "object",
            "properties": {
                "id":        {"type": "string", "description": "Memory item UUID."},
                "content":   {"type": "string", "description": "New content (empty = no change).", "default": ""},
                "title":     {"type": "string", "description": "New title (empty = no change).", "default": ""},
                "metadata":  {"type": "string", "description": "JSON-encoded metadata (empty = no change).", "default": ""},
                "importance": {"type": "number", "description": "New importance score (-1.0 = no change).", "default": -1.0},
                "reembed":   {"type": "boolean", "description": "Re-embed for semantic search.", "default": False},
                "refresh_on": {"type": "string", "description": "New refresh timestamp. 'clear' removes the reminder; empty = no change.", "default": ""},
                "refresh_reason": {"type": "string", "description": "New refresh reason. 'clear' removes; empty = no change.", "default": ""},
                "conversation_id": {"type": "string", "description": "New conversation id. 'clear' removes; empty = no change.", "default": ""},
            },
            "required": ["id"],
        },
        impl=memory_core.memory_update_impl,
        is_async=True,
        validators=(_memory_update_validator,),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_delete",
        description="Deletes a MemoryItem (soft or hard).",
        parameters={
            "type": "object",
            "properties": {
                "id":   {"type": "string", "description": "Memory item UUID."},
                "hard": {"type": "boolean", "description": "Hard delete (permanent).", "default": False},
            },
            "required": ["id"],
        },
        impl=memory_core.memory_delete_impl,
        is_async=False,
        validators=(),
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="conversation_start",
        description="Starts a new conversation thread.",
        parameters={
            "type": "object",
            "properties": {
                "title":    {"type": "string", "description": "Conversation title."},
                "agent_id": {"type": "string", "description": "Owning agent id.", "default": ""},
                "model_id": {"type": "string", "description": "Originating model id.", "default": ""},
                "tags":     {"type": "string", "description": "Comma-separated tags.", "default": ""},
            },
            "required": ["title"],
        },
        impl=memory_core.conversation_start_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="conversation_append",
        description="Appends a message to a conversation.",
        parameters={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Conversation UUID."},
                "role":            {"type": "string", "description": "Message role (e.g., 'user', 'assistant')."},
                "content":         {"type": "string", "description": "Message body."},
                "agent_id":        {"type": "string", "description": "Agent adding the message.", "default": ""},
                "model_id":        {"type": "string", "description": "Model that generated the message.", "default": ""},
                "embed":           {"type": "boolean", "description": "Embed for semantic search.", "default": True},
            },
            "required": ["conversation_id", "role", "content"],
        },
        impl=memory_core.conversation_append_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="conversation_search",
        description="Search messages across conversations using hybrid semantic/keyword search.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "k":     {"type": "integer", "description": "Max results (1-100).", "default": 8},
            },
            "required": ["query"],
        },
        impl=_conversation_search_impl,
        is_async=True,
        validators=(_memory_search_validator,),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="conversation_summarize",
        description="Summarize a conversation into key points using the local LLM.",
        parameters={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Conversation UUID."},
                "threshold":       {"type": "integer", "description": "Min message count to summarize.", "default": 20},
            },
            "required": ["conversation_id"],
        },
        impl=memory_core.conversation_summarize_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="chroma_sync",
        description="Bi-directional sync between local SQLite and ChromaDB.",
        parameters={
            "type": "object",
            "properties": {
                "max_items":     {"type": "integer", "description": "Max items per batch.", "default": 50},
                "direction":     {"type": "string", "enum": ["both", "to_chroma", "from_chroma"], "description": "Sync direction.", "default": "both"},
                "reset_stalled": {"type": "boolean", "description": "Reset stalled sync records.", "default": True},
            },
            "required": [],
        },
        impl=memory_sync.chroma_sync_impl,
        is_async=True,
        validators=(),
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_maintenance",
        description="Runs maintenance tasks on the memory store.",
        parameters={
            "type": "object",
            "properties": {
                "decay":                   {"type": "boolean", "description": "Apply importance decay.", "default": True},
                "purge_expired":           {"type": "boolean", "description": "Delete expired items.", "default": True},
                "prune_orphan_embeddings": {"type": "boolean", "description": "Remove orphaned embeddings.", "default": True},
            },
            "required": [],
        },
        impl=memory_maintenance.memory_maintenance_impl,
        is_async=False,
        validators=(),
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_consolidate",
        description="Consolidate old memories of the same type into summaries using the local LLM. Reduces clutter while preserving knowledge.",
        parameters={
            "type": "object",
            "properties": {
                "type_filter":  {"type": "string", "description": "Restrict to a memory type.", "default": ""},
                "agent_filter": {"type": "string", "description": "Restrict to an agent id.", "default": ""},
                "threshold":    {"type": "integer", "description": "Min items to consolidate.", "default": 20},
            },
            "required": [],
        },
        impl=memory_maintenance.memory_consolidate_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_dedup",
        description="Find and merge near-duplicate memory items.",
        parameters={
            "type": "object",
            "properties": {
                "threshold": {"type": "number", "description": "Similarity threshold (0-1).", "default": 0.92},
                "dry_run":   {"type": "boolean", "description": "Preview without applying.", "default": True},
            },
            "required": [],
        },
        impl=memory_maintenance.memory_dedup_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_feedback",
        description="Provide feedback on a memory item to improve quality.",
        parameters={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "Memory item UUID."},
                "feedback":  {"type": "string", "enum": ["useful", "not_useful", "misleading"], "description": "Feedback type.", "default": "useful"},
            },
            "required": ["memory_id"],
        },
        impl=memory_maintenance.memory_feedback_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_history",
        description="Returns the change history (audit trail) for a memory item. Tracks create, update, delete, and supersede events.",
        parameters={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "Memory item UUID."},
                "limit":     {"type": "integer", "description": "Max history records.", "default": 20},
            },
            "required": ["memory_id"],
        },
        impl=memory_core.memory_history_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_link",
        description="Creates a directional link between two memory items. Valid types: related, supports, contradicts, extends, supersedes, references, consolidates, message, handoff.",
        parameters={
            "type": "object",
            "properties": {
                "from_id":            {"type": "string", "description": "Source memory UUID."},
                "to_id":              {"type": "string", "description": "Target memory UUID."},
                "relationship_type":  {"type": "string", "enum": ["related", "supports", "contradicts", "extends", "supersedes", "references", "consolidates", "message", "handoff"], "description": "Link type.", "default": "related"},
            },
            "required": ["from_id", "to_id"],
        },
        impl=memory_core.memory_link_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_graph",
        description="Returns the local graph neighborhood of a memory item (connected memories up to N hops, max 3).",
        parameters={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "Memory item UUID."},
                "depth":     {"type": "integer", "description": "Traversal depth (1-3).", "default": 1},
            },
            "required": ["memory_id"],
        },
        impl=memory_core.memory_graph_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_verify",
        description="Verify content integrity by comparing stored hash with computed hash. Returns OK if content hasn't been tampered with.",
        parameters={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Memory item UUID."},
            },
            "required": ["id"],
        },
        impl=_memory_verify_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_set_retention",
        description="Set or update per-agent memory retention policy. Controls max memory count, TTL expiry, and auto-archival.",
        parameters={
            "type": "object",
            "properties": {
                "agent_id":     {"type": "string", "description": "Agent id for policy."},
                "max_memories": {"type": "integer", "description": "Max items to retain.", "default": 1000},
                "ttl_days":     {"type": "integer", "description": "Time-to-live in days (0 = no limit).", "default": 0},
                "auto_archive": {"type": "integer", "description": "Auto-archive threshold.", "default": 1},
            },
            "required": ["agent_id"],
        },
        impl=memory_maintenance.memory_set_retention_impl,
        is_async=False,
        validators=(_memory_set_retention_validator,),
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_export",
        description="Export memories as portable JSON. Filter by agent, type, or date.",
        parameters={
            "type": "object",
            "properties": {
                "agent_filter": {"type": "string", "description": "Restrict to an agent id.", "default": ""},
                "type_filter":  {"type": "string", "description": "Restrict to a memory type.", "default": ""},
                "since":        {"type": "string", "description": "ISO-8601 start date.", "default": ""},
            },
            "required": [],
        },
        impl=memory_maintenance.memory_export_impl,
        is_async=False,
        validators=(),
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_import",
        description="Import memories from a JSON export. UPSERT semantics — safe to re-run.",
        parameters={
            "type": "object",
            "properties": {
                "data": {"type": "string", "description": "JSON export string."},
            },
            "required": ["data"],
        },
        impl=memory_maintenance.memory_import_impl,
        is_async=False,
        validators=(),
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="gdpr_export",
        description="Export all memories for a data subject (GDPR data portability). Returns JSON with all memory items for the given user_id.",
        parameters={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "Data subject id."},
            },
            "required": ["user_id"],
        },
        impl=memory_maintenance.gdpr_export_impl,
        is_async=False,
        validators=(_gdpr_user_id_validator,),
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="gdpr_forget",
        description="Right to be forgotten — hard-deletes ALL data for a user_id including memories, embeddings, relationships, and history.",
        parameters={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "Data subject id to forget."},
            },
            "required": ["user_id"],
        },
        impl=memory_maintenance.gdpr_forget_impl,
        is_async=False,
        validators=(_gdpr_user_id_validator,),
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="enrich_pending",
        description="Enrich pending memory items with SLM-distilled facts. Default dry_run=true reports count + ETA; pass dry_run=false to execute.",
        parameters={
            "type": "object",
            "properties": {
                "dry_run":           {"type": "boolean", "default": True, "description": "If true, report count + ETA without executing; if false, execute enrichment."},
                "limit":             {"type": "integer", "default": 0, "description": "Max items to enrich (0 = no limit)."},
                "allowed_variants":  {"type": "array", "default": [], "description": "Variant names to include in enrichment (if empty, use default)."},
            },
            "required": [],
        },
        impl=memory_core.enrich_pending_impl,
        is_async=True,
        validators=(),
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_cost_report",
        description="Returns current session operation counts and estimated token usage for memory operations.",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        impl=memory_core.memory_cost_report_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_handoff",
        description=(
            "Hand off a task from one agent to another. Writes a new handoff-type memory "
            "owned by to_agent and links it to the given context memories with 'handoff' edges. "
            "Returns a confirmation string with the new memory id."
        ),
        parameters={
            "type": "object",
            "properties": {
                "from_agent":  {"type": "string", "description": "Sending agent id."},
                "to_agent":    {"type": "string", "description": "Receiving agent id."},
                "task":        {"type": "string", "description": "What the receiver should do."},
                "context_ids": {"type": "array", "items": {"type": "string"}, "description": "Memory ids to link via 'handoff' edges.", "default": []},
                "note":        {"type": "string", "description": "Optional free-text note.", "default": ""},
                "task_id":     {"type": "string", "description": "Optional tracked task id.", "default": ""},
            },
            "required": ["from_agent", "to_agent", "task"],
        },
        impl=memory_core.memory_handoff_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_inbox",
        description="List handoff messages addressed to agent_id, newest first. Pass unread_only=False to include already-acked items.",
        parameters={
            "type": "object",
            "properties": {
                "agent_id":     {"type": "string", "description": "Receiving agent id."},
                "unread_only":  {"type": "boolean", "description": "Show only unread messages.", "default": True},
                "limit":        {"type": "integer", "description": "Max messages to return.", "default": 20},
            },
            "required": ["agent_id"],
        },
        impl=memory_core.memory_inbox_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=True,
    ),
    ToolSpec(
        name="memory_inbox_ack",
        description="Mark a handoff memory as read (sets read_at = now).",
        parameters={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "Handoff memory UUID."},
            },
            "required": ["memory_id"],
        },
        impl=memory_core.memory_inbox_ack_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_refresh_queue",
        description=(
            "List memories whose refresh_on timestamp has arrived and need review. "
            "Read-only — to actually refresh a memory, call memory_update with new "
            "content/refresh_on. Pass include_future=True to see all memories with "
            "refresh_on set, not just overdue ones."
        ),
        parameters={
            "type": "object",
            "properties": {
                "agent_id":       {"type": "string", "description": "Restrict to memories owned by this agent.", "default": ""},
                "limit":          {"type": "integer", "description": "Max rows to return (1-500).", "default": 50},
                "include_future": {"type": "boolean", "description": "Include memories whose refresh_on is still in the future.", "default": False},
            },
            "required": [],
        },
        impl=memory_core.memory_refresh_queue_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=True,
    ),
    ToolSpec(
        name="agent_register",
        description="Register an agent (UPSERT). Sets status=active, last_seen=now.",
        parameters={
            "type": "object",
            "properties": {
                "agent_id":    {"type": "string", "description": "Unique agent identifier."},
                "role":        {"type": "string", "description": "Agent role or function.", "default": ""},
                "capabilities": {"type": "array", "items": {"type": "string"}, "description": "List of capabilities.", "default": []},
                "metadata":    {"type": "object", "description": "Free-form metadata.", "default": {}},
            },
            "required": ["agent_id"],
        },
        impl=memory_core.agent_register_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="agent_heartbeat",
        description="Update last_seen and set status=active. Errors if not registered.",
        parameters={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Unique agent identifier."},
            },
            "required": ["agent_id"],
        },
        impl=memory_core.agent_heartbeat_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=True,
    ),
    ToolSpec(
        name="agent_list",
        description="List registered agents, optionally filtered by status and/or role.",
        parameters={
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filter by agent status.", "default": ""},
                "role":   {"type": "string", "description": "Filter by agent role.", "default": ""},
            },
            "required": [],
        },
        impl=memory_core.agent_list_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="agent_get",
        description="Get full record for one registered agent.",
        parameters={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Unique agent identifier."},
            },
            "required": ["agent_id"],
        },
        impl=memory_core.agent_get_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="agent_offline",
        description="Mark an agent as offline.",
        parameters={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Unique agent identifier."},
            },
            "required": ["agent_id"],
        },
        impl=memory_core.agent_offline_impl,
        is_async=False,
        validators=(),
        default_allowed=False,
        inject_agent_id=True,
    ),
    ToolSpec(
        name="notify",
        description="Send a notification to an agent. Lightweight wake signal — agents poll notifications_poll.",
        parameters={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Recipient agent id."},
                "kind":     {"type": "string", "description": "Notification kind/type."},
                "payload":  {"type": "object", "description": "Free-form notification data.", "default": {}},
            },
            "required": ["agent_id", "kind"],
        },
        impl=memory_core.notify_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="notifications_poll",
        description="List notifications addressed to agent_id, newest first.",
        parameters={
            "type": "object",
            "properties": {
                "agent_id":    {"type": "string", "description": "Recipient agent id."},
                "unread_only": {"type": "boolean", "description": "Show only unread notifications.", "default": True},
                "limit":       {"type": "integer", "description": "Max notifications to return.", "default": 20},
            },
            "required": ["agent_id"],
        },
        impl=memory_core.notifications_poll_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=True,
    ),
    ToolSpec(
        name="notifications_ack",
        description="Mark one notification as read.",
        parameters={
            "type": "object",
            "properties": {
                "notification_id": {"type": "integer", "description": "Notification ID."},
            },
            "required": ["notification_id"],
        },
        impl=memory_core.notifications_ack_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="notifications_ack_all",
        description="Bulk-ack all unread notifications for an agent. Returns count acked.",
        parameters={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent id."},
            },
            "required": ["agent_id"],
        },
        impl=memory_core.notifications_ack_all_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=True,
    ),
    ToolSpec(
        name="task_create",
        description="Create a new task in 'pending' state. Returns task id.",
        parameters={
            "type": "object",
            "properties": {
                "title":          {"type": "string", "description": "Task title."},
                "created_by":     {"type": "string", "description": "Agent or user that created the task."},
                "description":    {"type": "string", "description": "Longer description.", "default": ""},
                "owner_agent":    {"type": "string", "description": "Initial owner (blank = unassigned).", "default": ""},
                "parent_task_id": {"type": "string", "description": "Optional parent task id for sub-tasks.", "default": ""},
                "metadata":       {"type": "object", "description": "Free-form metadata.", "default": {}},
            },
            "required": ["title", "created_by"],
        },
        impl=memory_core.task_create_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="task_assign",
        description="Assign a task to an owner. Sets state=in_progress and notifies the new owner.",
        parameters={
            "type": "object",
            "properties": {
                "task_id":     {"type": "string", "description": "Task UUID."},
                "owner_agent": {"type": "string", "description": "Agent to assign to."},
            },
            "required": ["task_id", "owner_agent"],
        },
        impl=memory_core.task_assign_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="task_update",
        description="Partial update for a task. Validates state transitions. On terminal state, sets completed_at.",
        parameters={
            "type": "object",
            "properties": {
                "task_id":     {"type": "string", "description": "Task UUID."},
                "state":       {"type": "string", "enum": ["", "pending", "in_progress", "blocked", "completed", "failed", "cancelled"], "description": "New state (empty = no change).", "default": ""},
                "description": {"type": "string", "description": "New description (empty = no change).", "default": ""},
                "metadata":    {"type": "object", "description": "New metadata (empty = no change).", "default": {}},
                "actor":       {"type": "string", "description": "Actor making the update.", "default": ""},
            },
            "required": ["task_id"],
        },
        impl=memory_core.task_update_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="task_delete",
        description="Delete a task. Soft-delete (default) sets a tombstone that propagates via pg_sync to the warehouse and peers. Hard-delete removes the row locally and requires a prior soft-delete.",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task UUID."},
                "hard":    {"type": "boolean", "description": "If true, permanently remove an already-tombstoned row from local SQLite.", "default": False},
                "actor":   {"type": "string", "description": "Actor performing the delete (audit log).", "default": ""},
            },
            "required": ["task_id"],
        },
        impl=memory_core.task_delete_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="task_set_result",
        description="Set the result memory pointer for a task. Does NOT change state.",
        parameters={
            "type": "object",
            "properties": {
                "task_id":         {"type": "string", "description": "Task UUID."},
                "result_memory_id": {"type": "string", "description": "Result memory UUID."},
            },
            "required": ["task_id", "result_memory_id"],
        },
        impl=memory_core.task_set_result_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="task_get",
        description="Get full record for one task.",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task UUID."},
            },
            "required": ["task_id"],
        },
        impl=memory_core.task_get_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="task_list",
        description="List tasks with optional filters. Newest updated first.",
        parameters={
            "type": "object",
            "properties": {
                "owner_agent":   {"type": "string", "description": "Filter by owner agent.", "default": ""},
                "state":         {"type": "string", "description": "Filter by task state.", "default": ""},
                "parent_task_id": {"type": "string", "description": "Filter by parent task id.", "default": ""},
                "limit":         {"type": "integer", "description": "Max tasks to return.", "default": 50},
            },
            "required": [],
        },
        impl=memory_core.task_list_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="task_tree",
        description="Render a recursive subtree of tasks rooted at root_task_id.",
        parameters={
            "type": "object",
            "properties": {
                "root_task_id": {"type": "string", "description": "Root task UUID."},
                "max_depth":    {"type": "integer", "description": "Max recursion depth.", "default": 3},
            },
            "required": ["root_task_id"],
        },
        impl=memory_core.task_tree_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    # ── Chat log subsystem ────────────────────────────────────────────────────
    ToolSpec(
        name="chatlog_write",
        description=(
            "Append one chat turn to the chat log DB. Provenance "
            "(host_agent, provider, model_id, conversation_id) is required. "
            "Writes are async-queued — returns the row id immediately."
        ),
        parameters={
            "type": "object",
            "properties": {
                "content":         {"type": "string",  "description": "Message text."},
                "role":            {"type": "string",  "description": "user|assistant|system|tool"},
                "conversation_id": {"type": "string",  "description": "Session/conversation UUID."},
                "host_agent":      {"type": "string",  "description": "Client: claude-code|gemini-cli|opencode|aider"},
                "provider":        {"type": "string",  "description": "Model provider: anthropic|google|openai|local|xai|deepseek|mistral|meta|other"},
                "model_id":        {"type": "string",  "description": "Exact model id, e.g. claude-opus-4-7"},
                "turn_index":      {"type": "integer", "description": "0-based turn index within conversation."},
                "agent_id":        {"type": "string",  "description": "Client agent id (host:user@machine).", "default": ""},
                "user_id":         {"type": "string",  "description": "Owning user id.", "default": ""},
                "metadata":        {"type": "string",  "description": "Extra metadata JSON string.", "default": "{}"},
                "tokens_in":       {"type": "integer", "description": "Prompt tokens (null if unknown)."},
                "tokens_out":      {"type": "integer", "description": "Completion tokens (null if unknown)."},
                "cost_usd":        {"type": "number",  "description": "Cost in USD (null → computed from price table)."},
                "latency_ms":      {"type": "integer", "description": "End-to-end request latency."},
            },
            "required": ["content", "role", "conversation_id", "host_agent", "provider", "model_id"],
        },
        impl=chatlog_core.chatlog_write_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="chatlog_write_bulk",
        description="Bulk-append N chat turns. Each item needs the same required fields as chatlog_write.",
        parameters={
            "type": "object",
            "properties": {
                "items": {"type": "array",   "description": "List of chat-turn dicts."},
                "embed": {"type": "boolean", "description": "Reserved; ignored — sweeper handles.", "default": False},
            },
            "required": ["items"],
        },
        impl=chatlog_core.chatlog_write_bulk_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="chatlog_search",
        description="Search chat_log rows. FTS5 keyword when query is non-empty; filter-only when empty.",
        parameters={
            "type": "object",
            "properties": {
                "query":           {"type": "string", "description": "FTS5 query; empty → filter-only listing."},
                "k":               {"type": "integer","description": "Max results.", "default": 8},
                "conversation_id": {"type": "string", "description": "Filter by conversation.", "default": ""},
                "host_agent":      {"type": "string", "description": "Filter by host agent.",   "default": ""},
                "provider":        {"type": "string", "description": "Filter by provider.",     "default": ""},
                "model_id":        {"type": "string", "description": "Filter by model id.",     "default": ""},
                "agent_id":        {"type": "string", "description": "Filter by agent id.",     "default": ""},
                "search_mode":     {"type": "string", "description": "hybrid|fts|vector (integrated mode only).", "default": "hybrid"},
                "since":           {"type": "string", "description": "ISO-8601 lower bound on created_at.", "default": ""},
                "until":           {"type": "string", "description": "ISO-8601 upper bound on created_at.", "default": ""},
            },
            "required": ["query"],
        },
        impl=chatlog_core.chatlog_search_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="chatlog_promote",
        description=(
            "Promote chat_log rows into the main memory DB under a new type "
            "(default 'conversation'). ATTACH + INSERT SELECT in separate/hybrid; "
            "UPDATE type in integrated."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ids":             {"type": "array",   "description": "Specific row ids to promote."},
                "conversation_id": {"type": "string",  "description": "Promote all rows in a conversation.", "default": ""},
                "since":           {"type": "string",  "description": "Promote rows at-or-after this ISO-8601.", "default": ""},
                "until":           {"type": "string",  "description": "Promote rows at-or-before this ISO-8601.", "default": ""},
                "copy":            {"type": "boolean", "description": "If false, delete source rows after copy.", "default": True},
                "target_type":     {"type": "string",  "description": "Type assigned in main DB.", "default": "conversation"},
            },
            "required": [],
        },
        impl=chatlog_core.chatlog_promote_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="chatlog_list_conversations",
        description="List distinct conversation_ids with turn counts and timespans.",
        parameters={
            "type": "object",
            "properties": {
                "host_agent": {"type": "string",  "description": "Filter by host agent.", "default": ""},
                "limit":      {"type": "integer", "description": "Max conversations.",    "default": 50},
                "offset":     {"type": "integer", "description": "Pagination offset.",    "default": 0},
            },
            "required": [],
        },
        impl=chatlog_core.chatlog_list_conversations_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="chatlog_cost_report",
        description="Aggregate tokens and cost_usd across chat_log rows. Groups: provider|model_id|host_agent|conversation_id|day.",
        parameters={
            "type": "object",
            "properties": {
                "since":    {"type": "string", "description": "ISO-8601 lower bound.",   "default": ""},
                "until":    {"type": "string", "description": "ISO-8601 upper bound.",   "default": ""},
                "group_by": {"type": "string", "description": "provider|model_id|host_agent|conversation_id|day", "default": "model_id"},
            },
            "required": [],
        },
        impl=chatlog_core.chatlog_cost_report_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="chatlog_set_redaction",
        description="Flip redaction on/off and update patterns. Persists to memory/.chatlog_config.json.",
        parameters={
            "type": "object",
            "properties": {
                "enabled":             {"type": "boolean", "description": "Turn redaction on/off."},
                "patterns":            {"type": "array",   "description": "Enabled pattern groups."},
                "redact_pii":          {"type": "boolean", "description": "Also redact PII (email/phone/SSN)."},
                "custom_regex":        {"type": "array",   "description": "User-supplied regex patterns."},
                "store_original_hash": {"type": "boolean", "description": "Store SHA-256 of pre-scrub content in metadata."},
            },
            "required": ["enabled"],
        },
        impl=chatlog_core.chatlog_set_redaction_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="chatlog_status",
        description=(
            "One-call health summary of the chat log subsystem: mode, DB paths, row "
            "counts, queue depth, spill files, embed backlog, hook timestamps, redaction state, warnings."
        ),
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        impl=chatlog_status.chatlog_status_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="chatlog_rescrub",
        description="Re-apply redaction to existing chat_log rows. Requires redaction.enabled=true.",
        parameters={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string",  "description": "Filter by conversation.", "default": ""},
                "since":           {"type": "string",  "description": "ISO-8601 lower bound.",  "default": ""},
                "until":           {"type": "string",  "description": "ISO-8601 upper bound.",  "default": ""},
                "limit":           {"type": "integer", "description": "Max rows to process.",   "default": 10000},
            },
            "required": [],
        },
        impl=chatlog_core.chatlog_rescrub_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="entity_search",
        description="Search entities by canonical_name and optionally by entity_type. Returns list of matching entities with optional neighbor counts.",
        parameters={
            "type": "object",
            "properties": {
                "query":         {"type": "string",  "default": "", "description": "Search term matched against canonical_name (LIKE %query%)."},
                "entity_type":   {"type": "string",  "default": "", "description": "Filter by entity type (if provided)."},
                "limit":         {"type": "integer", "default": 10,  "description": "Max results to return."},
                "with_neighbors": {"type": "boolean", "default": False, "description": "If true, compute neighbor_count for each entity."},
            },
            "required": [],
        },
        impl=memory_core.entity_search_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="entity_get",
        description="Load a single entity with its full neighborhood: predecessors, successors, and linked memory items.",
        parameters={
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "The entity ID to fetch."},
                "depth":     {"type": "integer", "default": 1, "description": "Graph depth for neighborhood walk (currently unused; reserved for future multi-hop)."},
            },
            "required": ["entity_id"],
        },
        impl=memory_core.entity_get_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="extract_pending",
        description="Extract pending entities from the queue. Default dry_run=true reports count + ETA; pass dry_run=false to execute.",
        parameters={
            "type": "object",
            "properties": {
                "dry_run":           {"type": "boolean", "default": True, "description": "If true, report count + ETA without executing; if false, execute extraction."},
                "limit":             {"type": "integer", "default": 0, "description": "Max items to extract (0 = no limit)."},
                "allowed_variants":  {"type": "array", "default": [], "description": "Variant names to include in extraction (if empty, use default)."},
            },
            "required": [],
        },
        impl=memory_core.extract_pending_impl,
        is_async=True,
        validators=(),
        default_allowed=False,
        inject_agent_id=False,
    ),
]


# ── Universal `database` parameter injection ─────────────────────────────────
# Every MCP tool gains an optional `database` argument so callers can route a
# single tool call to a non-default SQLite DB (separate stores for chatlog /
# memories / testing / benchmarking). Injection happens at module load so the
# catalog stays the single source of truth and schemas FastMCP introspects
# always include the field. The dispatcher (execute_tool and the
# memory_bridge wrapper) pops the value and activates it via active_database()
# before calling the impl — impl signatures do not change.
_DATABASE_PARAM_SCHEMA = {
    "type": "string",
    "description": (
        "Optional SQLite database path. Overrides M3_DATABASE env and the "
        "default memory/agent_memory.db for this call only. Empty = use default."
    ),
    "default": "",
}


def _inject_database_arg() -> None:
    for spec in TOOLS:
        props = spec.parameters.setdefault("properties", {})
        # Skip if some future spec already declared `database` explicitly.
        if "database" not in props:
            props["database"] = dict(_DATABASE_PARAM_SCHEMA)


_inject_database_arg()

_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in TOOLS}
