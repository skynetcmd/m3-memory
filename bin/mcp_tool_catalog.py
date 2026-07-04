"""
mcp_tool_catalog.py — single source of truth for the m3-memory MCP tool catalog.

Imported by:
  - bin/memory_bridge.py (FastMCP stdio server — registers each spec via @mcp.tool())
  - examples/multi-agent-team/dispatch.py (orchestrator-side dispatch loop)

Zero FastMCP dependency. Pure Python + memory_core + memory_sync + memory_maintenance.
Never import this module from those modules — that would create a cycle.

Mutation-safety invariant (do not regress): mutating memory tools
(memory_delete, memory_supersede) require the FULL UUID for their target id —
a prefix is rejected via _is_full_uuid in their validators. Read tools
(memory_get) accept an 8-char prefix for convenience, but an ambiguous prefix
on a mutation could close/delete the wrong memory irreversibly. This asymmetry
is intentional; keep the validators and the "full UUID required" wording in the
tool descriptions so it survives doc-inventory regeneration. Also note:
memory_supersede is non-destructive and creates a NEW successor each call — it
is an update primitive, not a delete; do not chain it to "clean up" clutter.

── Module structure (catalog/ subpackage split) ──────────────────────────────
This module is the AGGREGATOR + INJECTION point. The actual ToolSpec support
code and the 108 ToolSpec entries live in the bin/catalog/ subpackage:
  - catalog.lazy        — LazyImpl / LazyModuleProxy
  - catalog.spec         — ToolSpec dataclass + validation constants
  - catalog.validators   — per-tool argument validators
  - catalog.dispatch      — execute_tool(_structured), timeout machinery,
                            the m3_call/m3_index dispatcher, and the inline
                            impl wrappers (_conversation_search_impl,
                            _memory_verify_impl)
  - catalog.tools_<domain> — one module per domain (admin, memory, chatlog,
                            conversations, agent, tasks, entity, diagnostics,
                            files), each exporting a flat `TOOLS` list.
This module re-imports everything the support modules define (so
`mcp_tool_catalog.ToolSpec`, `.execute_tool`, `.m3_call_impl`,
`.LazyModuleProxy`, all validators, etc. still resolve for external
importers), concatenates the 9 domain TOOLS lists into ONE flat `TOOLS` list,
then runs the `database` + `timeout` parameter injection over the aggregated
list (order does not matter for injection — every spec is mutated the same
way — but injection must run AFTER aggregation since it walks the full list),
and finally rebuilds `_BY_NAME`.
"""

from __future__ import annotations

from catalog.dispatch import (  # noqa: F401
    _DEFAULT_TOOL_TIMEOUT,
    _DESTRUCTIVE_ALLOWED,
    _DISPATCH_EXCLUDE,
    ToolTimeout,
    _conversation_search_impl,
    _did_you_mean,
    _dispatch_one,
    _memory_verify_impl,
    _pop_database,
    _resolve_tool_timeout,
    _run_impl_bounded,
    _spec_by_name,
    _tool_arg_rows,
    default_allowlist,
    execute_tool,
    execute_tool_structured,
    get_tool,
    m3_call_impl,
    m3_index_impl,
    validate_args,
)

# ── Support code re-exported from catalog/ for the external-import surface ───
# (mcp_proxy, memory_bridge, tool_loader, gen_tool_*.py, tests all do
# `import mcp_tool_catalog` then attribute-access these names.) These have no
# direct caller INSIDE this module — it is a re-export facade — so ruff's F401
# unused-import autofix would strip them and break every external importer.
# `# noqa: F401` pins them; do NOT remove it.
from catalog.lazy import LazyImpl, LazyModuleProxy  # noqa: F401
from catalog.spec import (  # noqa: F401
    _UUID_RE,
    MAX_CONTENT_SIZE,
    MAX_K,
    MAX_QUERY_LENGTH,
    VALID_ENTITY_PREDICATES,
    VALID_ENTITY_TYPES,
    VALID_MEMORY_TYPES,
    ToolSpec,
    _is_full_uuid,
)
from catalog.validators import (  # noqa: F401
    _gdpr_user_id_validator,
    _memory_delete_validator,
    _memory_search_gated_validator,
    _memory_search_scored_validator,
    _memory_search_validator,
    _memory_set_retention_validator,
    _memory_suggest_validator,
    _memory_supersede_validator,
    _memory_update_validator,
    _memory_write_validator,
    _variant_gate,
)

# Re-export the shared LazyModuleProxy handles some external code may expect
# to poke at via mcp_tool_catalog (memory_core etc. were previously module
# globals here). Kept as module-level names for import-surface compatibility.
chatlog_core = LazyModuleProxy("chatlog_core")
chatlog_status = LazyModuleProxy("chatlog_status")
memory_core = LazyModuleProxy("memory_core")
memory_maintenance = LazyModuleProxy("memory_maintenance")
memory_sync = LazyModuleProxy("memory_sync")
_files_tools = LazyModuleProxy("files_memory.tools")

# ── Domain-partitioned tool modules ───────────────────────────────────────────
import catalog.tools_admin as tools_admin
import catalog.tools_agent as tools_agent
import catalog.tools_chatlog as tools_chatlog
import catalog.tools_conversations as tools_conversations
import catalog.tools_diagnostics as tools_diagnostics
import catalog.tools_entity as tools_entity
import catalog.tools_files as tools_files
import catalog.tools_memory as tools_memory
import catalog.tools_tasks as tools_tasks
import tool_loader as _tool_loader  # noqa: F401  (side-effect import: registers lazy domain-expansion impls)
from m3_sdk import active_database  # noqa: F401  (re-exported on the import surface)

# ── TOOLS catalog (aggregated from the 9 domain modules) ─────────────────────
# Order is NOT semantically significant: gen_tool_manifest sorts by
# (domain, name), and every consumer (memory_bridge, mcp_proxy, tool_loader)
# does `for spec in TOOLS` — none indexes/slices.
TOOLS: list[ToolSpec] = [
    *tools_admin.TOOLS,
    *tools_memory.TOOLS,
    *tools_chatlog.TOOLS,
    *tools_conversations.TOOLS,
    *tools_agent.TOOLS,
    *tools_tasks.TOOLS,
    *tools_entity.TOOLS,
    *tools_diagnostics.TOOLS,
    *tools_files.TOOLS,
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


# ── Universal `timeout` parameter injection (§6 hardening) ───────────────────
# Every MCP tool gains an optional `timeout` (seconds) so a caller can bound a
# single call, or lengthen/disable it for a known-long op. Same injection model
# as `database`: added at module load, popped by the dispatcher before the impl
# is called (impl signatures do not change). Precedence and semantics are in
# _resolve_tool_timeout: per-call arg > M3_TOOL_TIMEOUT env > 30s default; <=0
# disables. Only async impls are bounded.
_TIMEOUT_PARAM_SCHEMA = {
    "type": "number",
    "description": (
        "Optional per-call timeout in seconds. Overrides the M3_TOOL_TIMEOUT "
        "env and the 30s default for this call only. Use a larger value for "
        "long-running ops; <= 0 disables the timeout entirely."
    ),
    "default": 30,
}


# Per-tool default timeout ceilings (seconds) for known-slow tools that can
# legitimately exceed the 30s fast-fail default. These are DEFAULTS: a per-call
# `timeout` arg still overrides them. Rationale per class:
#   - *search / entity_search: first call after a cold start pays the bge-m3 GPU
#     model load (~10-20s) on top of the query.
#   - *_index / enrich_pending / chroma_sync: GPU/batch/network ops proportional
#     to corpus size.
#   - *_bulk / *_dedup / promote: cost scales with the input set.
# A tool absent from this map keeps the 30s default (fast-fail by construction).
_SLOW_TOOL_TIMEOUTS: dict[str, float] = {
    "chatlog_search": 120,
    "chatlog_write_bulk": 120,
    "chatlog_promote": 120,
    "memory_search": 120,
    "memory_search_scored": 120,
    "memory_search_routed": 120,
    "memory_search_multi_db": 180,
    "entity_search": 120,
    "conversation_search": 120,
    "files_search": 120,
    "files_index": 300,
    "files_dedup": 180,
    "m3_index": 300,
    "enrich_pending": 300,
    "chroma_sync": 300,
    "memory_update_bulk": 120,
    "memory_delete_bulk": 120,
    "memory_dedup": 180,
}


def _inject_database_arg() -> None:
    """Inject universal `database` + `timeout` params into every tool schema and
    apply per-tool timeout ceilings. Rebuilds TOOLS entries in place because
    ToolSpec is frozen (schema-dict mutation is fine in place; the timeout_s
    field needs dataclasses.replace)."""
    import dataclasses

    for i, spec in enumerate(TOOLS):
        props = spec.parameters.setdefault("properties", {})
        # Skip if some future spec already declared `database` explicitly.
        if "database" not in props:
            props["database"] = dict(_DATABASE_PARAM_SCHEMA)
        # Same for the universal `timeout` knob.
        if "timeout" not in props:
            props["timeout"] = dict(_TIMEOUT_PARAM_SCHEMA)
        # Apply a per-tool timeout ceiling unless the spec already set one.
        override = _SLOW_TOOL_TIMEOUTS.get(spec.name)
        if override is not None and spec.timeout_s is None:
            TOOLS[i] = dataclasses.replace(spec, timeout_s=override)


_inject_database_arg()

_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in TOOLS}
