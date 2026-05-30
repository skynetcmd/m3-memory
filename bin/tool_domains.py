"""Domain-grouped lazy loading for the MCP tool catalog.

Problem this solves
-------------------
m3 ships ~96 tools in `mcp_tool_catalog.TOOLS`. Their JSON schemas serialize
to ~22,600 tokens on the MCP wire. That cost is paid up-front at session
init — every client (Claude Code, Gemini CLI, OpenCode, OpenClaw, claude.ai
connector) burns ~10 % of a 200K context window on schemas the agent may
never touch.

Approach
--------
Group the tools into ~8 domains (memory, chatlog, files, entity, agent,
tasks, conversations, admin). At MCP startup, expose only a small
"essentials" set (search + write of the two main stores) plus a
`tools_load_domain` meta-tool. When the agent calls
`tools_load_domain(domain="files")` we register that domain's tools and
either:

  * Emit `notifications/tools/list_changed` (MCP protocol path) — clients
    that advertise `tools.listChanged` re-fetch the catalog and see the new
    tools natively.
  * Return the schemas as JSON in the tool-call result (fallback) — clients
    that don't support `listChanged` (Gemini CLI today, see issue #13850)
    still get the schemas in-band; the agent can pass arbitrary JSON to
    `mcp_tool_call` style bridges that accept dynamic tool names.

Disable lazy mode with `M3_TOOLS_LAZY=0` to restore the legacy "all
tools at startup" behavior.

Domain assignment is derived from the tool name prefix — see
`domain_of_tool()` below. This avoids touching every ToolSpec, and new
tools land in the right domain automatically as long as they follow the
existing naming convention.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

# ── Domain partition ─────────────────────────────────────────────────────────

# Order matters: first prefix match wins. Longer/more-specific prefixes go
# before short ones. The "admin" domain is the catch-all for cross-cutting
# system tools (notifications, GDPR, embedder mgmt, etc.).
_DOMAIN_PREFIXES: list[tuple[str, str]] = [
    # (prefix, domain)
    ("memory_search",       "memory"),
    ("memory_supersede",    "memory"),
    ("memory_write",        "memory"),
    ("memory_get",          "memory"),
    ("memory_update",       "memory"),
    ("memory_delete",       "memory"),
    ("memory_link",         "memory"),
    ("memory_graph",        "memory"),
    ("memory_history",      "memory"),
    ("memory_inbox",        "memory"),
    ("memory_handoff",      "memory"),
    ("memory_suggest",      "memory"),
    ("memory_export",       "memory"),
    ("memory_import",       "memory"),
    ("memory_verify",       "memory"),
    ("memory_feedback",     "memory"),
    ("memory_consolidate",  "memory"),
    ("memory_dedup",        "memory"),
    ("memory_maintenance",  "memory"),
    ("memory_refresh",      "memory"),
    ("memory_set_retention","memory"),
    ("memory_cost",         "memory"),
    # Aggregation queries (entity-count first-class) — memory-scoped
    # because they query the curated entity tables.
    ("memory_count_entities", "memory"),
    ("memory_count_mentions", "memory"),
    # System-diagnostic tools — separate from admin (which is GDPR /
    # notifications / chroma sync) so users grouping "what's wrong with
    # my install" can load just this bucket.
    ("memory_doctor",       "diagnostics"),
    ("embedder_status",     "diagnostics"),
    ("curate_memory",       "memory"),
    ("curate_chatlog",      "chatlog"),
    ("chatlog",             "chatlog"),
    ("files",               "files"),
    ("entity",              "entity"),
    ("agent",               "agent"),
    ("task",                "tasks"),
    ("conversation",        "conversations"),
    # dispatcher / meta tools (like tools_*, cross-cutting) — route to admin
    ("m3_call",             "admin"),
    ("m3_index",            "admin"),
    ("m3_help_capabilities", "admin"),
    # cross-cutting / system tools
    ("notify",              "admin"),
    ("notifications",       "admin"),
    ("enrich",              "admin"),
    ("extract",             "admin"),
    ("gdpr",                "admin"),
    ("chroma",              "admin"),
    ("embedder",            "diagnostics"),
    # entity_mentions is the renamed memory_list_mentions: same impl, but
    # the natural place to look for "given an entity, what mentions it"
    # is alongside entity_search / entity_get.
    ("entity_mentions",     "entity"),
    ("entity",              "entity"),
]


# Tools always exposed at MCP startup. The "read + write essentials" set —
# the 80/20 a typical session needs before reaching for anything else.
# Everything else lives behind `tools_load_domain`.
#
# Includes a small read-only "store overview" set (files_stats / files_index /
# files_corpus_list / chatlog_status / task_list / agent_list / files_get /
# files_health). These answer the extremely common "what's in <store>?" /
# "what's the state of <subsystem>?" question without a tools_load_domain
# round-trip. They earn their ~1K startup-token cost because the load_domain
# fallback is unreliable on clients that don't honor tools.listChanged: such
# clients are handed the new schemas in-band but can't actually invoke the
# freshly-registered tool, so an inspection request dead-ends. Keeping the
# read-only inspectors always-on sidesteps that gap. Anything that MUTATES a
# store stays behind tools_load_domain — only safe, idempotent reads belong
# here.
ESSENTIAL_TOOL_NAMES: frozenset[str] = frozenset({
    "memory_search",
    "memory_write",
    "memory_supersede",
    "memory_get",
    "chatlog_write",
    "chatlog_search",
    "files_search",
    # read-only store/subsystem overview — see note above
    "files_stats",
    "files_index",
    "files_corpus_list",
    "files_get",
    "files_health",
    "chatlog_status",
    "task_list",
    "agent_list",
    # dispatcher — reach the whole catalog by name without loading a domain
    "m3_call",
    "m3_index",
    "m3_help_capabilities",
})


# Human-readable one-liner per domain, used by `tools_list_domains` so the
# agent knows what each domain is for without expanding it first. Keep these
# short — they're paid at session start.
DOMAIN_DESCRIPTIONS: dict[str, str] = {
    "memory":        "Curated long-term memory: write/search/graph/dedup/retention.",
    "chatlog":       "Captured agent-conversation turns with promotion + cost reports.",
    "files":         "Directory ingestion, file-level supersession, hybrid search over docs.",
    "entity":        "Knowledge-graph entities — search, fetch, list mentions.",
    "agent":         "Multi-agent registration, heartbeat, presence.",
    "tasks":         "Task creation, assignment, tree, results.",
    "conversations": "Conversation start/append/search/summarize.",
    "admin":         "Notifications, enrichment, GDPR, Chroma sync.",
    "diagnostics":   "Self-service health probes: embedder_status, memory_doctor (run when search hangs or embeds look wrong).",
}


def domain_of_tool(tool_name: str) -> str:
    """Return the domain for a given tool name, or 'admin' if unmatched."""
    for prefix, domain in _DOMAIN_PREFIXES:
        if tool_name == prefix or tool_name.startswith(prefix + "_") or tool_name.startswith(prefix):
            return domain
    return "admin"


def group_by_domain(tool_names: Iterable[str]) -> dict[str, list[str]]:
    """Group an iterable of tool names by domain. Returns {domain: [names...]}."""
    out: dict[str, list[str]] = defaultdict(list)
    for n in tool_names:
        out[domain_of_tool(n)].append(n)
    return dict(out)


def is_essential(tool_name: str) -> bool:
    """Is this tool always exposed at MCP startup?"""
    return tool_name in ESSENTIAL_TOOL_NAMES


def domain_tool_names(all_tool_names: Iterable[str], domain: str) -> list[str]:
    """All tool names belonging to a given domain, sorted."""
    return sorted(n for n in all_tool_names if domain_of_tool(n) == domain)
