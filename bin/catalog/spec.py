"""catalog.spec — ToolSpec dataclass + validation constants.

Moved verbatim from mcp_tool_catalog.py (lines ~61-121 pre-split) as part of
the catalog/ subpackage split.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .lazy import LazyModuleProxy

memory_core = LazyModuleProxy("memory_core")

# ── Validation Constants (hoisted from memory_bridge.py) ─────────────────────
MAX_CONTENT_SIZE = 50_000
MAX_QUERY_LENGTH = 2_000
MAX_K = 100
VALID_MEMORY_TYPES = frozenset({
    "note", "fact", "decision", "preference", "conversation", "message",
    "task", "code", "config", "observation", "plan", "summary", "snippet",
    "reference", "log", "home", "user_fact", "scratchpad", "auto",
    "knowledge", "event_extraction", "fact_enriched", "chat_log",
    # Autonomous episodic->semantic abstraction (knowledge-maintenance Phase 4):
    # a higher-order rollup of many 'observation'/'fact' memories into a stable
    # belief, distinct from a manual 'summary'. Carries high confidence + links
    # back to its sources via 'consolidates' edges.
    "belief",
    # Home-network / infrastructure inventory categories. Pre-existing rows
    # in the store predate the strict catalog (restored 2026-04-17 from the
    # pre-hard-delete archive); widening lets new writes round-trip cleanly.
    "local_device", "network_config", "infrastructure", "home_automation",
    "migration-log",
    # Security: SSH endpoints, credentials references, firewall rules,
    # auth-related facts. Distinct from generic 'fact' for browsing.
    "security",
    # Platform-scoped notes — for guidance/snippets that only apply on one OS.
    "windows_only", "macos_only", "linux_only",
    # User-facing reminder / pending action. Lighter than 'task' which carries
    # the full task-state machine; 'to_do' is just "remember to do this".
    "to_do",
    # Reusable, learned PROCEDURE — the "how to do X" memory (skill/runbook/
    # how-to/checklist). A single head type; the sub-kind rides
    # metadata_json.procedure_kind ∈ {skill, runbook, how_to, checklist} so the
    # shipping taxonomy stays compact and users can extend the kind freely.
    # Ordered steps ride content (markdown) + a structured metadata_json.steps
    # array. Auto-distilled from successful task runs (memory_distill_procedures)
    # and linked back to sources via 'distills_from' edges.
    "procedure",
})

# Entity-graph enums — defined in memory_core to avoid circular import
# (mcp_tool_catalog imports memory_core, not vice versa). Re-exported here
# so callers see a single import surface.
VALID_ENTITY_TYPES = memory_core.VALID_ENTITY_TYPES
VALID_ENTITY_PREDICATES = memory_core.VALID_ENTITY_PREDICATES

# Canonical UUID shape (8-4-4-4-12 hex). Mutating tools (supersede/delete)
# require a full UUID, not the 8-char prefix that memory_get accepts for reads
# — an ambiguous prefix on a mutation could close the wrong memory.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _is_full_uuid(s: str) -> bool:
    """True iff s is a full canonical UUID (not a prefix). Used to gate
    mutating memory tools against prefix-based id ambiguity."""
    return bool(_UUID_RE.match(s.strip()))

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
    # Per-tool default timeout in seconds. None -> use the global default
    # (M3_TOOL_TIMEOUT env or 30s). Set generously on known-slow tools
    # (GPU/batch/network) so they don't hit the fast-fail cap; a per-call
    # `timeout` arg still overrides this. <= 0 disables the timeout.
    timeout_s: float | None = None
