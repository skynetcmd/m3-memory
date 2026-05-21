"""Bridge-callback registry for lazy domain expansion.

The meta-tools `tools_list_domains` and `tools_load_domain` live in
`mcp_tool_catalog.py` (so they appear in the unified catalog) but their
impls need to call back into the live FastMCP bridge to register tools
on demand. That callback is injected by `memory_bridge.py` at startup.

If the bridge never injects a callback (e.g. running the catalog outside
the bridge, like in tests), the meta-tool impls fall back to returning
the schemas as JSON in their result. That's the same behavior we use
when the client doesn't advertise `tools.listChanged`.
"""
from __future__ import annotations

import json
from typing import Callable, Optional

# Set by memory_bridge.py at startup. Signature:
#   register_domain(domain: str) -> dict
# Returns a small dict:
#   {
#     "domain": <name>,
#     "tools_now_available": [<list of newly-registered tool names>],
#     "tools_total": <count after registration>,
#     "method": "list_changed" | "schemas_in_result",
#     "schemas": <list-of-schema-dicts, only if method=='schemas_in_result'>,
#   }
_register_domain_callback: Optional[Callable[[str], dict]] = None


def set_register_domain_callback(fn: Callable[[str], dict]) -> None:
    """Called by memory_bridge.py at startup."""
    global _register_domain_callback
    _register_domain_callback = fn


def load_domain(domain: str) -> str:
    """Impl for `tools_load_domain` MCP tool. Returns a human-readable string
    (matching every other catalog impl's return shape — bridge wraps non-str
    in str())."""
    from tool_domains import DOMAIN_DESCRIPTIONS

    if domain not in DOMAIN_DESCRIPTIONS:
        return (
            f"Error: unknown domain '{domain}'. "
            f"Valid domains: {', '.join(sorted(DOMAIN_DESCRIPTIONS.keys()))}"
        )

    if _register_domain_callback is None:
        # Catalog-only mode (no live bridge) — just describe what would
        # have happened.
        return json.dumps({
            "domain": domain,
            "status": "callback-not-bound",
            "hint": "Called outside the MCP bridge; tools are static in this context.",
        })

    result = _register_domain_callback(domain)
    return json.dumps(result)


def list_domains() -> str:
    """Impl for `tools_list_domains` MCP tool."""
    import mcp_tool_catalog
    from tool_domains import DOMAIN_DESCRIPTIONS, group_by_domain

    all_names = [t.name for t in mcp_tool_catalog.TOOLS]
    grouped = group_by_domain(all_names)

    out = {
        "domains": [
            {
                "name": dname,
                "description": ddesc,
                "tool_count": len(grouped.get(dname, [])),
            }
            for dname, ddesc in sorted(DOMAIN_DESCRIPTIONS.items())
        ],
        "essentials_loaded": True,
        "hint": (
            "Call `tools_load_domain(domain=<name>)` to expose a domain's "
            "tools. Essentials (memory/files/chatlog search + memory_write) "
            "are always available."
        ),
    }
    return json.dumps(out)
