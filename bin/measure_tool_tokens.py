#!/usr/bin/env python3
"""measure_tool_tokens.py — quantify token cost of MCP tool schemas.

Usage:
    python bin/measure_tool_tokens.py

Reports the tokenized size of:
  - Full repertoire (every tool the proxy can dispatch)
  - Lazy-mode startup set (essentials + meta-tools — what an agent pays at
    session start under M3_TOOLS_LAZY, the default)
  - Per-domain cost (what `tools_load_domain(domain=…)` adds on demand)

Uses tiktoken if available (matches OpenAI/Claude tokenization closely);
falls back to a 4-chars-per-token approximation otherwise.

Run this whenever the catalog grows or descriptions change so the numbers
in CLAUDE.md / GEMINI.md / README.md / docs/* stay honest.
"""
from __future__ import annotations

import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "bin"))

# Show the full repertoire including destructive tools so the report covers
# everything the proxy *can* expose.
os.environ.setdefault("MCP_PROXY_ALLOW_DESTRUCTIVE", "1")

import mcp_proxy  # noqa: E402

try:
    import tiktoken
    _enc = tiktoken.get_encoding("cl100k_base")

    def tok(s: str) -> int:
        return len(_enc.encode(s))

    METHOD = "tiktoken cl100k_base"
except ImportError:
    def tok(s: str) -> int:
        return max(1, len(s) // 4)

    METHOD = "approximate (1 token ≈ 4 chars)"


def schema_tokens(tools: list) -> int:
    return sum(tok(json.dumps(t, separators=(",", ":"))) for t in tools)


def main() -> int:
    import tool_domains as td

    # Full repertoire: every tool the proxy can dispatch (protocol + debug +
    # the whole catalog), independent of lazy-mode gating.
    full = (
        mcp_proxy.PROTOCOL_TOOLS
        + mcp_proxy.DEBUG_TOOLS
        + mcp_proxy._build_catalog_tools()[0]
    )
    # Schemas are OpenAI function-calling shape: {"type":"function","function":{"name":…}}.
    def _name(t: dict) -> str:
        return t.get("function", {}).get("name") or t.get("name", "")

    by_name = {_name(t): t for t in full if _name(t)}

    # Lazy-mode startup set: only ESSENTIAL_TOOL_NAMES are exposed at session
    # start (M3_TOOLS_LAZY=1, the default). Intersect with the real schemas so
    # an essential without a catalog schema doesn't inflate the count.
    essentials = [by_name[n] for n in sorted(td.ESSENTIAL_TOOL_NAMES) if n in by_name]

    rows: list[tuple[str, int, int]] = [
        ("Full repertoire", len(full), schema_tokens(full)),
        ("Lazy startup (essentials+meta)", len(essentials), schema_tokens(essentials)),
    ]

    # Per-domain on-demand cost: what tools_load_domain(domain) adds, counting
    # only the not-already-essential tools in that domain.
    catalog_names = [_name(t) for t in full if _name(t)]
    for domain in sorted(td.DOMAIN_DESCRIPTIONS):
        names = [
            n for n in td.domain_tool_names(catalog_names, domain)
            if n not in td.ESSENTIAL_TOOL_NAMES and n in by_name
        ]
        if not names:
            continue
        tools = [by_name[n] for n in names]
        rows.append((f"  + domain {domain}", len(tools), schema_tokens(tools)))

    print(f"Token counter: {METHOD}")
    print(f"{'-' * 60}")
    print(f"{'configuration':36s}  {'tools':>6s}  {'tokens':>8s}")
    print(f"{'-' * 60}")
    for name, n_tools, n_tokens in rows:
        print(f"{name:36s}  {n_tools:6d}  {n_tokens:8d}")
    print(f"{'-' * 60}")
    full_tokens = rows[0][2]
    lazy_tokens = rows[1][2]
    saved = full_tokens - lazy_tokens
    pct = 100.0 * saved / max(1, full_tokens)
    print(f"Lazy-startup savings vs. full injection: {saved} tokens ({pct:.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
