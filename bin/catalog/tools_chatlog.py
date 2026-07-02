"""catalog.tools_chatlog — chatlog-domain ToolSpec entries (10).

Split out of mcp_tool_catalog.py's flat TOOLS list. Entries copied verbatim
(name, description, parameters, impl, validators, flags unchanged). Domain
assignment per tool_domains.domain_of_tool().
"""
from __future__ import annotations

from .lazy import LazyModuleProxy
from .spec import ToolSpec

chatlog_core = LazyModuleProxy("chatlog_core")
chatlog_status = LazyModuleProxy("chatlog_status")


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="curate_chatlog_apply",
        description=(
            "Deterministically apply a chatlog.db curator plan in ONE call. "
            "No LLM in the loop. Plan sections: decay (True/dict to run "
            "chatlog_decay), dedup (list of {keep_id, drop_ids}), promote "
            "(list of {ids, target_type}), prune (list of {conversation_id, "
            "reason}). Any section may be omitted. Returns structured per-"
            "section results + summary."
        ),
        parameters={
            "type": "object",
            "properties": {
                "plan": {
                    "type": "object",
                    "description": "Chatlog curator plan; see tool description for schema.",
                    "properties": {
                        "decay":   {"type": "boolean"},
                        "dedup":   {"type": "array", "items": {"type": "object"}},
                        "promote": {"type": "array", "items": {"type": "object"}},
                        "prune":   {"type": "array", "items": {"type": "object"}},
                    },
                },
                "db_path": {
                    "type": "string",
                    "description": "Optional chatlog DB path override.",
                    "default": "",
                },
            },
            "required": ["plan"],
        },
        impl=lambda plan, db_path="": __import__("curator_apply").apply_chatlog_plan(
            plan, db_path=db_path or None
        ),
        is_async=False,
        validators=(),
        default_allowed=False,  # destructive
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
]
