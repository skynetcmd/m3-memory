"""catalog.tools_conversations — conversations-domain ToolSpec entries (4).

Split out of mcp_tool_catalog.py's flat TOOLS list. Entries copied verbatim
(name, description, parameters, impl, validators, flags unchanged). Domain
assignment per tool_domains.domain_of_tool().
"""
from __future__ import annotations

from .lazy import LazyModuleProxy
from .spec import ToolSpec
from .validators import _memory_search_validator
from .dispatch import _conversation_search_impl

memory_core = LazyModuleProxy("memory_core")


TOOLS: list[ToolSpec] = [
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
]
