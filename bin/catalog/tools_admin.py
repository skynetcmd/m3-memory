"""catalog.tools_admin — admin-domain ToolSpec entries (15).

Split out of mcp_tool_catalog.py's flat TOOLS list. Entries copied verbatim
(name, description, parameters, impl, validators, flags unchanged). Domain
assignment per tool_domains.domain_of_tool().
"""
from __future__ import annotations

from .dispatch import m3_call_impl, m3_index_impl
from .lazy import LazyImpl, LazyModuleProxy
from .spec import ToolSpec
from .validators import _gdpr_user_id_validator

memory_core = LazyModuleProxy("memory_core")
memory_maintenance = LazyModuleProxy("memory_maintenance")

import tool_loader as _tool_loader  # provides lazy domain-expansion impls

TOOLS: list[ToolSpec] = [
    # ── Meta-tools: lazy domain loading ──────────────────────────────────────
    # These two ALWAYS register at MCP startup. Every other tool may be hidden
    # behind a domain — the agent calls `tools_load_domain` to expose them.
    # Set M3_TOOLS_LAZY=0 to opt out and expose all tools eagerly.
    ToolSpec(
        name="tools_list_domains",
        description=(
            "List m3 tool domains (memory, chatlog, files, entity, agent, tasks, "
            "conversations, diagnostics, admin) and their tool counts. Call "
            "`tools_load_domain` to expose a domain's full tool surface."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        impl=_tool_loader.list_domains,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="tools_load_domain",
        description=(
            "Register a tool domain's full surface for the current MCP session. "
            "Use when you need tools beyond the essentials (memory_search, "
            "memory_write, memory_get, chatlog_search, chatlog_write, files_search). "
            "Valid domains: memory, chatlog, files, entity, agent, tasks, "
            "conversations, diagnostics, admin."
        ),
        parameters={
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Domain to expose. See `tools_list_domains`.",
                },
            },
            "required": ["domain"],
        },
        impl=_tool_loader.load_domain,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="m3_help_capabilities",
        description=(
            "Discover m3-memory tool capabilities, parameters, and availability. "
            "Allows filtering by a logical domain (memory, chatlog, files, entity, "
            "agent, tasks, conversations, admin, diagnostics) or searching by keywords."
        ),
        parameters={
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Optional domain to filter capabilities (e.g., 'memory', 'files').",
                },
                "query": {
                    "type": "string",
                    "description": "Optional keyword search term to filter tools.",
                },
            },
            "required": [],
        },
        impl=_tool_loader.help_capabilities,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="m3_index",
        description=(
            "List m3 catalog tools (optionally one domain) as structured rows: "
            "name, domain, one-line summary, destructive flag, and arg specs "
            "(name/type/required). Use this to discover the exact args for any "
            "tool before calling it via m3_call — cheaper than a failed call. "
            "Read-only catalog metadata; never returns tool output. Domains: "
            "memory, chatlog, files, entity, agent, tasks, conversations, "
            "diagnostics, admin."
        ),
        parameters={
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Filter to one domain (empty = whole catalog).",
                    "default": "",
                },
            },
            "required": [],
        },
        impl=m3_index_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="m3_call",
        description=(
            "Invoke ANY m3 catalog tool by name without loading its domain — the "
            "low-token path to the full tool surface. Single call: pass `tool` "
            "(e.g. 'files_stats') and `args` (an object). Batch: pass `batch`, a "
            "list of {tool, args} (each isolated — one failure won't abort the "
            "rest; capped at 100). Set `dry_run` to validate args + check the "
            "destructive gate WITHOUT executing. Returns JSON. Call `m3_index` "
            "first if you don't know a tool's args. Destructive tools require "
            "MCP_PROXY_ALLOW_DESTRUCTIVE=1."
        ),
        parameters={
            "type": "object",
            "properties": {
                "tool":    {"type": "string", "description": "Catalog tool name (see m3_index).", "default": ""},
                "args":    {"type": "object", "description": "Arguments object for the target tool.", "default": {}},
                "batch":   {"type": "array", "description": "List of {tool, args} for one-round-trip batch dispatch.", "default": None},
                "dry_run": {"type": "boolean", "description": "Validate + gate-check only; do not execute.", "default": False},
            },
            "required": [],
        },
        impl=m3_call_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
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
        name="extract_entities",
        description=(
            "Accepts raw text, extracts entities and relationship predicates "
            "based on the configured pluggable entity-extraction backend, "
            "and returns them as structured JSON without modifying the database. "
            "Use this to preview what entities and relationships would be extracted "
            "from raw content."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Raw text content to extract entities and links from.",
                },
            },
            "required": ["text"],
        },
        impl=LazyImpl("memory.extraction", "extract_entities_impl"),
        is_async=True,
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
