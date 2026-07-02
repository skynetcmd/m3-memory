"""catalog.tools_diagnostics — diagnostics-domain ToolSpec entries (3).

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
        name="embedder_status",
        description="Check the status of the local sovereign embedder server (default port 8082, override via M3_EMBED_FALLBACK_URL).",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        impl=memory_core.embedder_status_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_doctor",
        description=(
            "Self-service diagnostic for the m3-memory embedding cascade. "
            "Probes tier-1 (in-proc GGUF), tier-2 (m3-embed-server :8082), "
            "DB integrity, and end-to-end embed roundtrip — all concurrently "
            "with bounded 2s per-probe timeouts. Returns a structured dict "
            "with status ('healthy' | 'degraded' | 'broken'), per-tier "
            "details, issues, and actionable recommendations. Use this when "
            "memory_search hangs, embeddings look wrong, or you're standing "
            "up a new deployment."
        ),
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        impl=memory_core.memory_doctor_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),

    ToolSpec(
        name="memory_doctor_fix",
        description=(
            "Run the m3-memory self-repair mode (m3 doctor --fix). "
            "Attempts to auto-fix the most common deployment issues in order: "
            "(1) apply pending SQLite migrations, "
            "(2) rebuild the FTS5 full-text index, "
            "(3) embed-backfill items that are missing vector embeddings (capped at 500/run), "
            "(4) rebuild the m3_system_cohesion table if absent. "
            "Set dry_run=True to see what *would* be done without making any changes. "
            "Returns a structured dict with per-action outcomes and a summary status "
            "('ok' | 'partial' | 'nothing_to_do' | 'failed')."
        ),
        parameters={
            "type": "object",
            "properties": {
                "dry_run": {
                    "type": "boolean",
                    "description": (
                        "If true, report what would be done without writing anything. "
                        "Default: false."
                    ),
                    "default": False,
                },
            },
            "required": [],
        },
        impl=memory_core.memory_doctor_fix_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
]
