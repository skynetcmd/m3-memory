"""catalog.tools_agent — agent-domain ToolSpec entries (6).

Split out of mcp_tool_catalog.py's flat TOOLS list. Entries copied verbatim
(name, description, parameters, impl, validators, flags unchanged). Domain
assignment per tool_domains.domain_of_tool().
"""
from __future__ import annotations

from .lazy import LazyModuleProxy
from .spec import ToolSpec

memory_core = LazyModuleProxy("memory_core")


TOOLS: list[ToolSpec] = [
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
        name="agent_set_trust",
        description=(
            "Set an agent's trust score (0.5-1.0, clamped). Trust weights that "
            "agent's assertions in memory confidence aggregation; 1.0 is neutral. "
            "Upserts the agent if absent."
        ),
        parameters={
            "type": "object",
            "properties": {
                "agent_id":    {"type": "string", "description": "Unique agent identifier."},
                "trust_score": {"type": "number", "description": "Trust in [0.5, 1.0]; clamped."},
            },
            "required": ["agent_id", "trust_score"],
        },
        impl=memory_core.agent_set_trust_impl,
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
]
