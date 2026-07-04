#!/usr/bin/env python3
"""gen_capability_matrix.py — generate docs/CAPABILITY_MATRIX.md from the MCP catalog.

A single scannable capability index grouped by domain, serving three audiences at
once (humans scanning for a feature, search engines indexing capabilities, and AI
agents mapping a natural-language request to the right tool). Generated from
docs/tools/MCP_CATALOG.json so it never drifts from the actual tool surface — run
after any catalog change, same as gen_mcp_inventory.py.

    python bin/gen_capability_matrix.py
"""
import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG = os.path.join(BASE_DIR, "docs", "tools", "MCP_CATALOG.json")
OUTPUT = os.path.join(BASE_DIR, "docs", "CAPABILITY_MATRIX.md")

# Domain -> (human capability-group heading, one-line "what this group is for").
# Domains come from the catalog; keep this map in sync when a new domain appears
# (an unmapped domain falls through to a title-cased heading with no blurb).
DOMAIN_GROUPS = {
    "memory":        ("🧠 Memory", "Write, retrieve, version, and reconcile long-term agent memory."),
    "chatlog":       ("💬 Chat Log", "Capture verbatim conversation turns before compaction; audit and replay."),
    "files":         ("📁 Files Memory", "Index, search, and recall project files as memory."),
    "entity":        ("🕸️ Entity Graph", "Extract and query entities and their relationships across sessions."),
    "conversations": ("🗂️ Conversations", "Group and inspect turns by conversation / team session."),
    "agent":         ("👥 Agents", "Register agents, hand off tasks, and route multi-agent work."),
    "tasks":         ("✅ Tasks", "Track and coordinate agent tasks and their state."),
    "diagnostics":   ("🩺 Diagnostics", "Health, cost, and integrity checks for the memory store."),
    "admin":         ("⚙️ Admin & Sync", "Maintenance, cross-store sync, import/export, and lifecycle ops."),
}

# Preferred display order (most user-facing first).
ORDER = ["memory", "chatlog", "files", "entity", "conversations",
         "agent", "tasks", "diagnostics", "admin"]


def _escape(s: str) -> str:
    return (s or "").replace("|", "\\|").strip()


def main() -> int:
    with open(CATALOG, encoding="utf-8") as f:
        catalog = json.load(f)
    tools = catalog["tools"]

    by_domain: dict[str, list] = {}
    for t in tools:
        by_domain.setdefault(t["domain"], []).append(t)

    domains = ORDER + [d for d in sorted(by_domain) if d not in ORDER]

    lines: list[str] = []
    lines.append("# M3 Capability Matrix")
    lines.append("")
    lines.append(
        "> **Generated** by `bin/gen_capability_matrix.py` from "
        "`docs/tools/MCP_CATALOG.json` — do not edit by hand; re-run after any "
        "tool-catalog change. This is the single scannable index of *what M3 can "
        "do* and *which tool does it*, for humans, search engines, and AI agents."
    )
    lines.append("")
    lines.append(
        f"**{len(tools)} tools across {len([d for d in domains if d in by_domain])} "
        "capability groups.** A ⚠️ marks a destructive tool (mutates or deletes)."
    )
    lines.append("")

    # Quick jump index.
    lines.append("## Capability groups")
    lines.append("")
    for d in domains:
        if d not in by_domain:
            continue
        heading, blurb = DOMAIN_GROUPS.get(d, (d.title(), ""))
        anchor = heading.lower()
        # strip emoji + spaces for a github-style anchor
        anchor = "".join(ch for ch in anchor if ch.isalnum() or ch in " -").strip().replace(" ", "-")
        lines.append(f"- [{heading}](#{anchor}) — {blurb} ({len(by_domain[d])} tools)")
    lines.append("")

    for d in domains:
        if d not in by_domain:
            continue
        heading, blurb = DOMAIN_GROUPS.get(d, (d.title(), ""))
        lines.append(f"## {heading}")
        lines.append("")
        if blurb:
            lines.append(f"_{blurb}_")
            lines.append("")
        lines.append("| Tool | Description | Mutates? |")
        lines.append("|---|---|---|")
        for t in sorted(by_domain[d], key=lambda x: x["name"]):
            flag = "⚠️ yes" if t.get("destructive") else "read-only"
            lines.append(f"| `{_escape(t['name'])}` | {_escape(t.get('summary'))} | {flag} |")
        lines.append("")

    content = "\n".join(lines) + "\n"
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(content)
    # relpath raises ValueError across Windows drives (e.g. OUTPUT on a C: tmp
    # dir while the repo is on D:, which is exactly the freshness test's setup).
    # This is a cosmetic log line — never let it abort generation.
    try:
        _shown = os.path.relpath(OUTPUT, BASE_DIR)
    except ValueError:
        _shown = OUTPUT
    print(f"wrote {_shown} "
          f"({len(tools)} tools, {len([d for d in domains if d in by_domain])} groups)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
