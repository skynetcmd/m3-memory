"""catalog.tools_memory — memory-domain ToolSpec entries (33).

Split out of mcp_tool_catalog.py's flat TOOLS list. Entries copied verbatim
(name, description, parameters, impl, validators, flags unchanged). Domain
assignment per tool_domains.domain_of_tool().
"""
from __future__ import annotations

import asyncio

from .lazy import LazyImpl, LazyModuleProxy
from .spec import ToolSpec, VALID_MEMORY_TYPES
from .validators import (
    _memory_write_validator,
    _memory_supersede_validator,
    _memory_search_gated_validator,
    _memory_search_scored_validator,
    _memory_suggest_validator,
    _memory_update_validator,
    _memory_delete_validator,
    _memory_set_retention_validator,
)
from .dispatch import _memory_verify_impl

memory_core = LazyModuleProxy("memory_core")
memory_maintenance = LazyModuleProxy("memory_maintenance")


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
        name="memory_write_from_file",
        description=(
            "Write a memory whose content is read from a file on disk. Use this "
            "when the memory body is large (>1k chars) to avoid the autoregressive "
            "decode latency of streaming a multi-thousand-token JSON `input` field "
            "through tool_use — write the body with the Write tool first (off the "
            "streaming path, fast), then call this tool with just the path + tiny "
            "metadata. The MCP server reads the file, writes the row through the "
            "same path as memory_write (all gates apply), and by default deletes "
            "the source file on success. Path must be absolute on the host running "
            "this MCP server. Files >200000 bytes are rejected; underlying content "
            "is still capped at 50000 chars by memory_write_impl."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path":          {"type": "string", "description": "Absolute path to a UTF-8 text file on the MCP server host. The file's contents become the memory `content`."},
                "type":          {"type": "string", "description": f"Memory type. One of: {', '.join(sorted(VALID_MEMORY_TYPES))}."},
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
                "conversation_id": {"type": "string", "description": "Groups this memory with a conversation / team session.", "default": ""},
                "refresh_on":    {"type": "string", "description": "ISO-8601 timestamp when this memory should be flagged for review.", "default": ""},
                "refresh_reason": {"type": "string", "description": "Why this memory needs refreshing.", "default": ""},
                "variant":       {"type": "string", "description": "Pipeline identifier for A/B variant tracking.", "default": ""},
                "delete_after_read": {"type": "boolean", "description": "Delete the source file after successful write. Default true (signals contents are now authoritative in m3-memory).", "default": True},
            },
            "required": ["path", "type"],
        },
        impl=memory_core.memory_write_from_file_impl,
        is_async=True,
        validators=(_memory_write_validator,),
        default_allowed=True,
        inject_agent_id=True,
    ),
    ToolSpec(
        name="memory_supersede",
        description=(
            "Explicitly supersede an existing memory with a new one. Use this to "
            "record an intentional update — 'this fact replaces that specific "
            "memory' — when you know the old memory's id. Unlike memory_write's "
            "automatic contradiction detection (a cosine + title heuristic that "
            "may link the wrong prior memory or none at all), this targets the "
            "given old_id deterministically. Non-destructive: the old memory is "
            "retained, its validity interval is closed (is_deleted=1, valid_to "
            "set), and a 'supersedes' edge is recorded new -> old. The old "
            "memory stays retrievable by id and via memory_history, and "
            "as_of-filtered search still sees it valid before the supersession "
            "point — it is only dropped from default search. Fields you omit "
            "(type, title, importance, scope) are inherited from the old memory, "
            "so pass only what changed. To hard-delete instead, that is a "
            "separate gated tool (memory_delete). "
            "old_id MUST be the full UUID — a prefix is rejected (full UUID "
            "required for mutation safety; memory_get accepts a prefix, this "
            "does not). Note: each supersede creates a NEW successor memory; "
            "call it once with the full id, do not chain supersedes."
        ),
        parameters={
            "type": "object",
            "properties": {
                "old_id":       {"type": "string", "description": "FULL UUID of the memory being superseded (not an 8-char prefix — a prefix is rejected for mutation safety). Must exist and not already be deleted/superseded."},
                "content":      {"type": "string", "description": "Body of the replacement memory (max 50000 chars)."},
                "type":         {"type": "string", "description": f"Memory type of the replacement. Omit to inherit the old memory's type. One of: {', '.join(sorted(VALID_MEMORY_TYPES))}.", "default": ""},
                "title":        {"type": "string", "description": "Title of the replacement. Omit to inherit the old memory's title.", "default": ""},
                "metadata":     {"type": "string", "description": "JSON-encoded metadata object for the replacement.", "default": "{}"},
                "agent_id":     {"type": "string", "description": "Owning agent id. Injected by the orchestrator.", "default": ""},
                "model_id":     {"type": "string", "description": "Originating model id.", "default": ""},
                "change_agent": {"type": "string", "description": "Agent causing the supersede (audit).", "default": ""},
                "importance":   {"type": "number", "description": "0.0-1.0 relevance of the replacement. Omit (leave at -1) to inherit the old memory's importance.", "default": -1.0},
                "source":       {"type": "string", "description": "Provenance tag.", "default": "agent"},
                "embed":        {"type": "boolean", "description": "Embed the replacement for semantic search.", "default": True},
                "user_id":      {"type": "string", "description": "Data subject id.", "default": ""},
                "scope":        {"type": "string", "description": "Isolation scope of the replacement. Omit to inherit the old memory's scope.", "default": ""},
                "valid_from":   {"type": "string", "description": "ISO-8601 validity start of the replacement; also the point at which the old memory's validity is closed. Defaults to now.", "default": ""},
                "variant":      {"type": "string", "description": "Pipeline identifier for A/B variant tracking.", "default": ""},
                "embed_text":   {"type": "string", "description": "Override text used for embedding; falls back to content when empty.", "default": ""},
            },
            "required": ["old_id", "content"],
        },
        impl=memory_core.memory_supersede_impl,
        is_async=True,
        validators=(_memory_supersede_validator,),
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
                "requesting_agent":   {"type": "string", "description": "Calling agent's id. When set, ENFORCES cross-agent isolation: private (scope='agent') rows from OTHER agents are excluded; the caller still sees its own private rows plus all shared scopes (org/user/session). Empty = no enforcement (sees all, back-compat).", "default": ""},
                "as_of":              {"type": "string", "description": "ISO-8601 time-travel cutoff.", "default": ""},
                "conversation_id":    {"type": "string", "description": "Restrict to a conversation / team session.", "default": ""},
                "recency_bias":       {"type": "number", "description": "Boost newer items (0.0=off, 0.1-0.2=moderate, higher=aggressive). Useful for 'current' or 'latest' queries.", "default": 0.0},
                "adaptive_k":         {"type": "boolean", "description": "Auto-trim results at the score drop-off point. WARNING: regresses on temporal-reasoning, knowledge-update, and multi-session queries; safe only for sharp-curve queries where most retrievals would be noise. Prefer `auto_route=True` on memory_search_routed for safer multi-signal routing.", "default": False},
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
        name="memory_search_scored",
        description=(
            "Structured hybrid FTS5+vector+MMR search. Returns ranked rows "
            "[(score, item)] with content + metadata (id, valid_from, "
            "conversation_id, user_id) — NOT formatted text. Use when a caller "
            "needs parseable rows rather than an LLM-readable block (e.g. a "
            "memory-provider backend). Empty query + type_filter = filter-only "
            "listing. Same bench-data gate as memory_search."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query":              {"type": "string", "description": "Search text. Empty string = filter-only (type/scope) listing.", "default": ""},
                "k":                  {"type": "integer", "description": "Max rows (1-100).", "default": 8},
                "type_filter":        {"type": "string", "description": "Restrict to a memory type (e.g. 'user_fact').", "default": ""},
                "agent_filter":       {"type": "string", "description": "Restrict to an agent id.", "default": ""},
                "search_mode":        {"type": "string", "enum": ["hybrid", "semantic", "keyword"], "description": "Retrieval mode.", "default": "hybrid"},
                "user_id":            {"type": "string", "description": "Filter by data subject.", "default": ""},
                "scope":              {"type": "string", "description": "Filter by isolation scope.", "default": ""},
                "requesting_agent":   {"type": "string", "description": "Calling agent's id. When set, ENFORCES cross-agent isolation: private (scope='agent') rows from OTHER agents are excluded; the caller still sees its own private rows plus all shared scopes (org/user/session). Empty = no enforcement (sees all, back-compat).", "default": ""},
                "as_of":              {"type": "string", "description": "ISO-8601 time-travel cutoff (bitemporal point-in-time query).", "default": ""},
                "conversation_id":    {"type": "string", "description": "Restrict to a conversation / team session.", "default": ""},
                "recency_bias":       {"type": "number", "description": "Boost newer items (0.0=off).", "default": 0.0},
                "variant":            {"type": "string", "description": "Ingest-pipeline filter. '' = real user data only.", "default": ""},
                "include_bench_data": {"type": "boolean", "description": "Opt in to LOCOMO / LongMemEval bench rows. Default False hides any variant-tagged row.", "default": False},
            },
            "required": [],
        },
        impl=memory_core.memory_search_scored_impl,
        is_async=True,
        validators=(_memory_search_scored_validator,),
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
                "requesting_agent": {"type": "string", "description": "Calling agent's id. When set, ENFORCES cross-agent isolation: other agents' private (scope='agent') rows are excluded; caller sees its own private + shared scopes. Empty = no enforcement.", "default": ""},
                "type_filter":     {"type": "string", "default": ""},
                "agent_filter":    {"type": "string", "default": ""},
                "search_mode":     {"type": "string", "default": "hybrid"},
                "variant":         {"type": "string", "default": ""},
                "as_of":           {"type": "string", "default": ""},
                "conversation_id": {"type": "string", "default": ""},
                "explain":         {"type": "boolean", "default": False},
                "entity_graph":    {"type": "boolean", "description": "Direct lever for entity-graph expansion. Default False = OFF (production default; matches memory_search behavior). True = parse query for named entities, traverse entity_relationships up to entity_graph_depth hops, fold matched memory_ids into the result set tagged expanded_via='entity_graph'. The AUTO layer can also flip this on for the entity_anchored branch (see auto_entity_graph_enabled), but caller-explicit entity_graph=True works without auto_route. Use for benchmarking + production opt-in once empirically validated.", "default": False},
                "rerank":          {"type": "boolean", "description": "Cross-encoder reranking. Default False = OFF (production behavior unchanged). True = re-score top (rerank_pool_k or 3*k) hits with sentence-transformers CrossEncoder, blend with hybrid score per rerank_blend, re-sort. Adds ~12MB-560MB model download (cached at ~/.cache/torch) + ~50ms/pair on GPU, ~200ms/pair on CPU. Used in benchmarking; opt-in for production retrieval.", "default": False},
                "rerank_model":    {"type": "string", "description": "Cross-encoder model id. Default empty = DEFAULT_RERANK_MODEL (cross-encoder/ms-marco-MiniLM-L-6-v2 — small, fast, English-tuned). Higher-accuracy alternative: BAAI/bge-reranker-v2-m3 (multilingual, larger, slower). Override via M3_RERANK_MODEL env var.", "default": ""},
                "rerank_pool_k":   {"type": "integer", "description": "Pool size before rerank. Default 0 = 3*k. Higher pool = more candidates rescored = slower but potentially higher recall. Never truncates below final k. Only used when rerank=True.", "default": 0},
                "rerank_blend":    {"type": "number", "description": "Blend factor: final_score = rerank_blend * ce_score + (1 - rerank_blend) * hybrid_score. Default 1.0 = pure CE replacement (most aggressive). 0.5 = average. 0.3 = CE as tiebreaker over hybrid. 0.0 = no-op (skip rerank — same effect as rerank=False). Only used when rerank=True.", "default": 1.0},
                "entity_graph_depth":         {"type": "integer", "description": "BFS hop count over entity_relationships when entity_graph=True. Default 1 = direct neighbors only. Clamped to [1,3] core-side. Higher depth pulls more neighbors but adds noise; depth=2 typically helps multi-hop questions but regresses sharp single-fact lookups.", "default": 1},
                "entity_graph_max_neighbors": {"type": "integer", "description": "Cap on entity nodes discovered during BFS traversal when entity_graph=True. Default 20. Clamped to [1,100] core-side. Lower = fewer rows folded in but tighter relevance; higher = broader recall at cost of precision.", "default": 20},
                "auto_route":      {"type": "boolean", "description": "Multi-signal retrieval routing. Default False = no auto-routing (existing behavior). True = router picks branch (temporal/multi_session/sharp/default) and fills in unset parameters with branch-specific values; caller-explicit values still win.", "default": False},
                "auto_top1_sharp_min": {"type": "number", "description": "Top-1 score above which query is marked sharp. Default 0.89. Used by sharp branch to detect high-confidence queries.", "default": 0.89},
                "auto_slope_at_3_sharp_min": {"type": "number", "description": "Slope-at-3 (score drop per rank) above which query is marked sharp. Default 0.08. Steeper curves = more relevance discrimination.", "default": 0.08},
                "auto_conv_id_diversity_threshold": {"type": "integer", "description": "Number of distinct conversation IDs in top-10 hits above which query is routed to multi_session. Default 5. Higher threshold = only very scattered results trigger expansion.", "default": 5},
                "auto_top1_low_threshold": {"type": "number", "description": "Score floor for sharp detection (OOD guard). Default 0.50. Below this, query is not marked sharp even if other signals fire (prevents misclassifying low-confidence matches).", "default": 0.50},
                "auto_temporal_k": {"type": "integer", "description": "k for temporal branch when auto_route=True. Default 15. Branch fires when query has temporal cues (when/since/before/after/dates).", "default": 15},
                "auto_temporal_recency_bias": {"type": "number", "description": "recency_bias for temporal branch. Default 0.05. Boosts recent memories over older ones (useful for 'recently'/'today' questions).", "default": 0.05},
                "auto_temporal_expand_sessions": {"type": "boolean", "description": "expand_sessions for temporal branch. Default True. Pulls full conversation context when a temporal hit is found (important for 'what happened after X' questions).", "default": True},
                "auto_temporal_graph_depth": {"type": "integer", "description": "graph_depth for temporal branch (AUTO_v2 fix). Default 1. Traverses memory relationships to find cross-temporal references (e.g., follow-up discussions on an earlier event).", "default": 1},
                "auto_multi_k": {"type": "integer", "description": "k for multi_session branch when auto_route=True. Default 20. Branch fires on comparison queries (how many/count/total) or when hits scatter across multiple conversations.", "default": 20},
                "auto_multi_expand_sessions": {"type": "boolean", "description": "expand_sessions for multi_session branch. Default True. Pulls all turns from detected conversation IDs for aggregate comparisons ('list all X across conversations').", "default": True},
                "auto_sharp_threshold_ratio": {"type": "number", "description": "Trim hits below (top_score * ratio) in sharp branch. Default 0.85. Removes tail noise when there's a clear high-confidence cluster.", "default": 0.85},
                "auto_sharp_k_min": {"type": "integer", "description": "Floor for hit count after sharp threshold trim. Default 3. Ensures at least this many hits even if threshold trim is aggressive.", "default": 3},
                "auto_sharp_k_max": {"type": "integer", "description": "Ceiling for hit count after sharp threshold trim. Default 10. Caps result set when sharp curve is very steep.", "default": 10},
                "auto_entity_graph_enabled": {"type": "boolean", "description": "AUTO entity-anchored branch enable. Default True = branch fires when query has named entities AND auto_route=True. Caller can pass False to disable AUTO from enabling entity_graph (still works if caller passes entity_graph=True explicitly).", "default": True},
                "auto_entity_graph_depth": {"type": "integer", "description": "Entity-graph traversal depth when AUTO entity_anchored branch fires. Default 1 = single-hop neighbors. Higher (2-3) traverses further but adds noise.", "default": 1},
                "auto_entity_graph_max_neighbors": {"type": "integer", "description": "Cap on entities expanded during AUTO entity-anchored traversal. Default 20.", "default": 20},
                "auto_entity_graph_named_entity_threshold": {"type": "integer", "description": "Minimum named-entity count in query for AUTO entity_anchored branch to fire. Default 1 = fire on any proper noun phrase (two+ capitalized words). Higher (2-3) is more conservative.", "default": 1},
                "entity_graph_valid_types": {"type": "array", "items": {"type": "string"}, "description": "Override list of allowed entity_type values for graph traversal. Default empty = use core defaults (person, place, organization, event, concept, object, date). Pass a list to filter traversal to specific types.", "default": []},
                "entity_graph_valid_predicates": {"type": "array", "items": {"type": "string"}, "description": "Override list of allowed predicate values for graph traversal. Default empty = use core defaults (works_at, located_in, before, after, same_as, contradicts, mentions, relates_to). Pass a list to filter traversal to specific predicates.", "default": []},
            },
            "required": ["query"],
        },
        impl=memory_core.memory_search_routed_impl,
        is_async=True,
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_search_multi_db",
        description="Search across multiple SQLite databases (e.g. agent_memory.db AND agent_chatlog.db) in one call. Each DB is searched independently via hybrid FTS5+vector search and the top results are merged by score. Returns the global top-K with each item tagged with its source database. Caveat: FTS5 BM25 scores depend on per-DB corpus stats so cross-DB ranks are approximate; works well for small fan-out (typically 2-5 DBs sharing the same embed_model).",
        parameters={
            "type": "object",
            "properties": {
                "query":           {"type": "string", "description": "Search query."},
                "databases":       {"type": "string", "description": "Comma-separated list of DB paths to fan out to (e.g. 'memory/agent_memory.db,memory/agent_chatlog.db'). Empty = no-op."},
                "k":               {"type": "integer", "default": 8, "description": "Top-K to return globally after merge. Each per-DB search also retrieves K, then results are pooled and re-sorted."},
                "type_filter":     {"type": "string", "default": ""},
                "agent_filter":    {"type": "string", "default": ""},
                "search_mode":     {"type": "string", "default": "hybrid"},
                "user_id":         {"type": "string", "default": ""},
                "scope":           {"type": "string", "default": ""},
                "requesting_agent": {"type": "string", "description": "Calling agent's id. When set, ENFORCES cross-agent isolation: other agents' private (scope='agent') rows are excluded; caller sees its own private + shared scopes. Empty = no enforcement.", "default": ""},
                "as_of":           {"type": "string", "default": ""},
                "conversation_id": {"type": "string", "default": ""},
                "recency_bias":    {"type": "number", "default": 0.0},
                "adaptive_k":      {"type": "boolean", "default": False},
                "variant":         {"type": "string", "default": "", "description": "Variant filter applied uniformly to each DB. Pass a comma-separated list to use multi-variant IN filtering (passed through to memory_search_scored_impl)."},
                "fan_out_limit":   {"type": "integer", "default": 0, "description": "Cap on concurrent per-DB searches. 0 = unbounded (typical, since fan-out N is small)."},
            },
            "required": ["query", "databases"],
        },
        impl=memory_core.memory_search_multi_db_impl,
        is_async=True,
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_get",
        description="Retrieves a full MemoryItem; accepts full UUID or 8-char prefix; ambiguous prefixes return an error.",
        parameters={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Memory item id — 36-char UUID or 8-char prefix. "
                                   "Ambiguous prefixes return an error listing the matching ids.",
                },
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
        name="memory_update_bulk",
        description=(
            "Apply many metadata-only updates in one transaction per chunk. "
            "Designed for curation passes that retroactively set retention, "
            "importance, or supersession metadata. Per-id reembed is NOT "
            "supported here (use memory_update for reembed, or re_embed_all "
            "for the bulk reembed case). Returns structured "
            "{succeeded, not_found, no_change, total}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "updates": {
                    "type": "array",
                    "description": (
                        "List of update specs. Each MUST include `id`. "
                        "Any subset of {content, title, importance, metadata, "
                        "refresh_on, refresh_reason, conversation_id} may be "
                        "set. Field semantics match memory_update."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id":              {"type": "string"},
                            "content":         {"type": "string"},
                            "title":           {"type": "string"},
                            "importance":      {"type": "number"},
                            "metadata":        {"type": "string", "description": "JSON-encoded."},
                            "refresh_on":      {"type": "string"},
                            "refresh_reason":  {"type": "string"},
                            "conversation_id": {"type": "string"},
                        },
                        "required": ["id"],
                    },
                },
            },
            "required": ["updates"],
        },
        impl=memory_core.memory_update_bulk_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="curate_memory_apply",
        description=(
            "Deterministically apply a memory.db curator plan in ONE call. "
            "No LLM in the loop — the apply phase is a pure function over the "
            "structured plan. Replaces the agent-driven APPLY-mode loop. "
            "Plan sections: delete (soft, list of UUIDs), delete_hard (cascade, "
            "list of UUIDs), link (list of {from_id, to_id, relationship_type}), "
            "update (list of {id, importance, metadata, ...}). Any section may "
            "be omitted. Returns structured per-section results + summary."
        ),
        parameters={
            "type": "object",
            "properties": {
                "plan": {
                    "type": "object",
                    "description": "Curator plan; see tool description for schema.",
                    "properties": {
                        "delete":      {"type": "array", "items": {"type": "string"}},
                        "delete_hard": {"type": "array", "items": {"type": "string"}},
                        "link":        {"type": "array", "items": {"type": "object"}},
                        "update":      {"type": "array", "items": {"type": "object"}},
                    },
                },
            },
            "required": ["plan"],
        },
        impl=lambda plan: __import__("curator_apply").apply_memory_plan(plan),
        is_async=False,
        validators=(),
        default_allowed=False,  # destructive: bulk deletes + writes
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_delete",
        description=(
            "Deletes a MemoryItem (soft or hard). id MUST be the full UUID — a "
            "prefix is rejected (full UUID required for mutation safety; "
            "memory_get accepts a prefix, this does not)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "id":   {"type": "string", "description": "FULL UUID of the memory (not an 8-char prefix — a prefix is rejected for mutation safety)."},
                "hard": {"type": "boolean", "description": "Hard delete (permanent).", "default": False},
            },
            "required": ["id"],
        },
        impl=memory_core.memory_delete_impl,
        is_async=False,
        validators=(_memory_delete_validator,),
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_delete_bulk",
        description=(
            "Deletes a list of MemoryItems (soft or hard) in one transaction per chunk. "
            "Use for curation/dedup passes deleting many items; falls back to per-id "
            "memory_delete behavior with the same hard-cascade semantics. Returns a "
            "structured {succeeded, not_found, mode} dict."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ids":  {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of memory item UUIDs to delete.",
                },
                "hard": {"type": "boolean", "description": "Hard delete (permanent).", "default": False},
            },
            "required": ["ids"],
        },
        impl=memory_core.memory_delete_bulk_impl,
        is_async=False,
        validators=(),
        default_allowed=False,  # destructive — gated by MCP_PROXY_ALLOW_DESTRUCTIVE
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
        impl=lambda **kw: asyncio.to_thread(memory_maintenance.memory_maintenance_impl, **kw),
        is_async=True,
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
        description=(
            "Find (and optionally soft-delete) near-duplicate memory items by "
            "cosine similarity over embeddings. Returns "
            "{count, groups: [{a, b, title_a, title_b, score}, ...], threshold, "
            "scanned, applied}. Use dry_run=True (default) for a preview; "
            "dry_run=False soft-deletes the second item of each pair."
        ),
        parameters={
            "type": "object",
            "properties": {
                "threshold": {"type": "number", "description": "Cosine similarity threshold in [0, 1]. Higher = stricter near-duplicate.", "default": 0.92},
                "dry_run":   {"type": "boolean", "description": "Preview without applying.", "default": True},
                "limit":     {"type": "integer", "description": "Cap on returned groups (0 = no cap). count reflects true total regardless.", "default": 0},
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
        name="memory_link_bulk",
        description=(
            "Create many memory_relationships rows in one transaction per chunk. "
            "Use for curation passes adding many LINK edges (device pairs, "
            "supersession chains, etc.). Validates existence of every referenced "
            "memory_id; skips duplicates without raising. Returns a structured "
            "{created, skipped_missing, skipped_duplicate, total} dict."
        ),
        parameters={
            "type": "object",
            "properties": {
                "links": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "from_id":           {"type": "string"},
                            "to_id":             {"type": "string"},
                            "relationship_type": {"type": "string", "enum": ["related", "supports", "contradicts", "extends", "supersedes", "references", "consolidates", "message", "handoff"]},
                        },
                        "required": ["from_id", "to_id"],
                    },
                    "description": "List of link specs. relationship_type per entry overrides the outer default.",
                },
                "relationship_type": {
                    "type": "string",
                    "enum": ["related", "supports", "contradicts", "extends", "supersedes", "references", "consolidates", "message", "handoff"],
                    "description": "Default link type for entries that omit it.",
                    "default": "related",
                },
            },
            "required": ["links"],
        },
        impl=memory_core.memory_link_bulk_impl,
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
        name="memory_pin",
        description=(
            "Pin a memory to exempt it from decay, expiry, and retention "
            "purges. Pinned memories are never auto-archived, never "
            "importance/confidence-decayed, and never expiry- or "
            "TTL-purged by memory_maintenance — use for facts that must "
            "survive indefinitely regardless of access recency."
        ),
        parameters={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Memory item id — 36-char UUID or 8-char prefix. "
                                   "Ambiguous prefixes return an error listing the matching ids.",
                },
            },
            "required": ["id"],
        },
        impl=memory_core.memory_pin_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_unpin",
        description="Unpin a memory, restoring normal decay/expiry/retention handling.",
        parameters={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Memory item id — 36-char UUID or 8-char prefix. "
                                   "Ambiguous prefixes return an error listing the matching ids.",
                },
            },
            "required": ["id"],
        },
        impl=memory_core.memory_unpin_impl,
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
        name="memory_count_entities",
        description=(
            "Count distinct entities mentioned in a single conversation. "
            "Direct-index aggregation — no LLM, no embedding. Use this for "
            "'how many distinct X did I mention' inventory questions where "
            "top-k embedding retrieval would miss instances spread thinly "
            "across many turns. Returns {count, conversation_id, entity_type, "
            "pattern}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Required. The conversation to scope the count to. Empty / missing → ValueError (cross-conversation scans are not supported)."},
                "entity_type":     {"type": "string", "default": "", "description": "Optional type filter (e.g. 'product', 'place', 'person'). Empty = all types."},
                "pattern":         {"type": "string", "default": "", "description": "Optional case-insensitive substring filter on canonical_name. Max length 256."},
            },
            "required": ["conversation_id"],
        },
        impl=memory_core.count_entities_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_count_mentions",
        description=(
            "Per-entity mention frequency within a single conversation, "
            "sorted DESC by count. Use for 'what are the most-mentioned X' or "
            "'rank entities by frequency.' Returns {total, rows: [{entity_id, "
            "canonical_name, entity_type, mention_count}, ...]}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Required. The conversation to scope the count to."},
                "entity_type":     {"type": "string", "default": "", "description": "Optional type filter."},
                "pattern":         {"type": "string", "default": "", "description": "Optional case-insensitive substring filter on canonical_name. Max length 256."},
                "limit":           {"type": "integer", "default": 0, "description": "Max rows to return. 0 = default (1000). Hard cap = 10000."},
            },
            "required": ["conversation_id"],
        },
        impl=memory_core.count_mentions_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
]
