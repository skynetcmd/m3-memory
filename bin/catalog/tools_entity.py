"""catalog.tools_entity — entity-domain ToolSpec entries (3).

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
        name="entity_mentions",
        description=(
            "List memory_ids that mention a specific entity in a single "
            "conversation. Pass either entity_id (preferred — exact match) or "
            "canonical_name (case-insensitive, optionally disambiguated by "
            "entity_type). Returns {entity_id, canonical_name, entity_type, "
            "total, memory_ids: [...]}. Caller fetches text via existing read "
            "paths (which carry their own authz). Companion to entity_search "
            "and entity_get; lives in the 'entity' domain."
        ),
        parameters={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Required."},
                "entity_id":       {"type": "string", "default": "", "description": "Preferred lookup — the entities.id. If supplied, canonical_name and entity_type are ignored."},
                "canonical_name":  {"type": "string", "default": "", "description": "Alternative lookup. Case-insensitive exact match. One of entity_id OR canonical_name is required."},
                "entity_type":     {"type": "string", "default": "", "description": "Optional disambiguator when canonical_name is ambiguous across types."},
                "limit":           {"type": "integer", "default": 0, "description": "Max memory_ids to return. 0 = default (1000). Hard cap = 10000."},
            },
            "required": ["conversation_id"],
        },
        impl=memory_core.list_mentions_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
]
