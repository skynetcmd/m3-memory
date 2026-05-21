"""Domain-grouped lazy loading for the MCP tool catalog.

Problem this solves
-------------------
m3 ships 85 tools in `mcp_tool_catalog.TOOLS`. Their JSON schemas serialize
to ~15,800 tokens on the MCP wire. That cost is paid up-front at session
init — every client (Claude Code, Gemini CLI, OpenCode, OpenClaw, claude.ai
connector) burns ~8 % of a 200K context window on schemas the agent may
never touch.

Approach
--------
Group the 85 tools into ~8 domains (memory, chatlog, files, entity, agent,
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

Disable lazy mode with `M3_TOOLS_LAZY=0` to restore the legacy "all 85
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
    ("curate_memory",       "memory"),
    ("curate_chatlog",      "chatlog"),
    ("chatlog",             "chatlog"),
    ("files",               "files"),
    ("entity",              "entity"),
    ("agent",               "agent"),
    ("task",                "tasks"),
    ("conversation",        "conversations"),
    # cross-cutting / system tools
    ("notify",              "admin"),
    ("notifications",       "admin"),
    ("enrich",              "admin"),
    ("extract",             "admin"),
    ("gdpr",                "admin"),
    ("chroma",              "admin"),
    ("embedder",            "admin"),
]


# Tools always exposed at MCP startup. The "read + write essentials" set —
# the 80/20 a typical session needs before reaching for anything else.
# Everything else lives behind `tools_load_domain`.
ESSENTIAL_TOOL_NAMES: frozenset[str] = frozenset({
    "memory_search",
    "memory_write",
    "memory_get",
    "chatlog_write",
    "chatlog_search",
    "files_search",
})


# Human-readable one-liner per domain, used by `tools_list_domains` so the
# agent knows what each domain is for without expanding it first. Keep these
# short — they're paid at session start.
DOMAIN_DESCRIPTIONS: dict[str, str] = {
    "memory":        "Curated long-term memory: write/search/graph/dedup/retention.",
    "chatlog":       "Captured agent-conversation turns with promotion + cost reports.",
    "files":         "Directory ingestion, file-level supersession, hybrid search over docs.",
    "entity":        "Knowledge-graph entities — search + fetch.",
    "agent":         "Multi-agent registration, heartbeat, presence.",
    "tasks":         "Task creation, assignment, tree, results.",
    "conversations": "Conversation start/append/search/summarize.",
    "admin":         "Notifications, enrichment, GDPR, Chroma sync, embedder status.",
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
