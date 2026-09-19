"""Domain-grouped lazy loading for the MCP tool catalog.

Problem this solves
-------------------
m3 ships 115 tools in `mcp_tool_catalog.TOOLS`. Their JSON schemas serialize
to ~29,700 tokens on the MCP wire. Paid up-front at session init, that is
~15 % of a 200K context window spent on schemas the agent may never touch —
for every client (Claude Code, Gemini CLI, OpenCode, OpenClaw, claude.ai
connector). Lazy mode brings it to ~3,900 tokens (~2 %).

Approach
--------
Group the tools into ~8 domains (memory, chatlog, files, entity, agent,
tasks, conversations, admin). At MCP startup, expose only a small
"essentials" set (search + write of the main stores) plus a
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
    # System-diagnostic tools — separate from admin (which is GDPR /
    # notifications) so users grouping "what's wrong with
    # my install" can load just this bucket.
    ("memory_doctor",       "diagnostics"),
    ("embedder_status",     "diagnostics"),
    ("chatlog_status",      "diagnostics"),
    ("files_health",        "diagnostics"),

    # Core domain catch-alls (first prefix match wins)
    ("memory",              "memory"),
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
    ("embedder",            "diagnostics"),
]


# Tools always exposed at MCP startup. MEASURED, not assumed: chosen as the
# tools that actually absorb the observed direct-call traffic, per
# `bin/measure_tool_usage.py` over 88 transcripts (527 direct MCP calls).
#
# These + m3_call + the 2 meta-tools cover 95.4% of all observed direct calls
# for 3,929 tokens. The previous 19-tool set cost 5,170 tokens and covered
# 90.5% -- so this is cheaper AND higher-coverage, not a tradeoff. The old set
# spent 1,672 tokens on six tools with ZERO direct calls (chatlog_write,
# files_search, files_index, files_health, task_list, and m3_call's own
# wrapper) while the top 6 absorbed 495 of 527 calls.
#
# ⚠ WHY THE "read-only inspectors" ARE GONE. The previous rationale kept
# files_stats / files_index / files_corpus_list / files_get / files_health /
# task_list / agent_list always-on because "the load_domain fallback is
# unreliable on clients that don't honor tools.listChanged: such clients are
# handed the new schemas in-band but can't actually invoke the freshly-
# registered tool". That claim was TESTED on 2026-09-17 and is false:
# `_register_one()` calls `mcp.tool(...)`, which registers SERVER-side, and
# execution is gated by that registration -- not by client list state. A gated
# tool was invoked successfully immediately after `tools_load_domain` WITHOUT
# any `list_tools` refresh, which is exactly what a non-listChanged client
# does. Measured usage agreed: those seven inspectors drew 3 direct calls
# between them.
#
# ⚠ MUTATION IS NOT THE CRITERION, frequency is. An earlier version of this
# comment said "anything that MUTATES a store stays behind tools_load_domain --
# only safe, idempotent reads belong here." That rule never described the set
# it guarded: memory_write mutates and is the single most-used tool at 259
# calls. memory_supersede (44 calls, 4th most-used) was briefly gated on that
# reading and is restored here. The real line is COST vs OBSERVED USE -- a
# frequently-used write earns its schema; a rare one does not.
#
# Gating a tool is not the same as making it unreachable, and the two callers
# have different answers:
#   * MCP clients   -- `m3_call` invokes any catalog tool BY NAME without
#                      loading its domain. That is what m3_call is FOR: a
#                      transport concern, a low-token door to the full surface.
#   * code / scripts -- the CLI (`m3 memory memory_supersede ...`). Programmatic
#                      callers should never route through m3_call; there is no
#                      token budget to economise on and no gating to work around.
#     (The CLI is in fact the DOMINANT surface: 966 calls vs 527 direct MCP.)
#
# ⚠ RE-MEASURE BEFORE TRIMMING FURTHER. `measure_tool_usage.py` warns that zero
# calls is a SHAPE signal as much as a need signal -- a tool whose return shape
# forces a Bash fallback gets avoided. Do not demote on a zero count until
# as_records has landed for that tool. These counts also come from ONE machine's
# transcripts and skew toward its workload (heavy memory writes, heavy
# notification polling); a different fleet would shift the top 6.
ESSENTIAL_TOOL_NAMES: frozenset[str] = frozenset({
    # The measured top 6 by direct calls (495 of 527 observed).
    "memory_write",        # 259 calls
    "memory_search",       # 101 calls
    "chatlog_search",      #  57 calls
    "memory_supersede",    #  44 calls
    "chatlog_status",      #  20 calls
    "memory_get",          #  14 calls
    # files_search is here on PRINCIPLE, not on measured use (0 direct calls).
    # m3 has THREE primary stores — memory, chatlog, files — and the README
    # promises search for each in the always-on set; test_lazy_tool_loading.py
    # ::test_essentials_include_search_for_each_primary_store pins that promise.
    # Dropping it saved 283 tokens (0.14% of a 200K window) and broke a
    # documented guarantee, which is the wrong trade. Its zero count is also
    # suspect rather than conclusive: measure_tool_usage.py warns that zero
    # calls can mean the RETURN SHAPE forces a Bash fallback, and the files
    # store is genuinely populated here (20 documents, 2 ingest runs).
    "files_search",
    # Dispatcher: reaches the other 109 tools by name with no domain load.
    # Its own direct-call count reads 0 only because measure_tool_usage.py
    # attributes the delegated tool, not the wrapper -- 95 calls flowed
    # through it across 79 wrappers.
    "m3_call",
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
    "admin":         "Notifications, enrichment, GDPR.",
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
