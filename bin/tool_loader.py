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


def help_capabilities(domain: str = "", query: str = "") -> str:
    """Impl for `m3_help_capabilities` MCP tool."""
    import mcp_tool_catalog
    from tool_domains import DOMAIN_DESCRIPTIONS, domain_of_tool, is_essential

    domain = (domain or "").strip().lower()
    query = (query or "").strip().lower()

    if domain and domain not in DOMAIN_DESCRIPTIONS:
        return (
            f"Error: unknown domain '{domain}'. "
            f"Valid domains: {', '.join(sorted(DOMAIN_DESCRIPTIONS.keys()))}"
        )

    matched_tools = []
    for spec in mcp_tool_catalog.TOOLS:
        td = domain_of_tool(spec.name)
        if domain and td != domain:
            continue
        if query and (query not in spec.name.lower() and query not in spec.description.lower()):
            continue

        # Extract parameter details to make it readable
        props = spec.parameters.get("properties", {})
        required = spec.parameters.get("required", [])
        params_info = []
        for p_name, p_schema in props.items():
            req_str = "required" if p_name in required else "optional"
            p_type = p_schema.get("type", "any")
            p_desc = p_schema.get("description", "")
            params_info.append(f"- **{p_name}** ({p_type}, {req_str}): {p_desc}")

        matched_tools.append({
            "name": spec.name,
            "domain": td,
            "description": spec.description,
            "is_essential": is_essential(spec.name),
            "parameters": params_info
        })

    # Group by domain for clean formatting
    grouped: dict[str, list[dict]] = {}
    for mt in matched_tools:
        grouped.setdefault(str(mt["domain"]), []).append(mt)

    lines = ["# M3-Memory Tool Capabilities Index"]
    if domain:
        lines.append(f"Filtering by domain: **{domain}**")
    if query:
        lines.append(f"Searching for query: *\"{query}\"*")
    lines.append("")

    if not matched_tools:
        lines.append("No matching tools found.")
        return "\n".join(lines)

    for d_name in sorted(grouped.keys()):
        d_desc = DOMAIN_DESCRIPTIONS.get(d_name, "")
        lines.append(f"## Domain: {d_name} ({len(grouped[d_name])} tools)")
        lines.append(f"*{d_desc}*")
        lines.append("")

        for t in grouped[d_name]:
            availability = "Always Available (Essential)" if t["is_essential"] else f"Lazy (requires `tools_load_domain(domain=\"{d_name}\")`)"
            lines.append(f"### `{t['name']}`")
            lines.append(f"**Description:** {t['description']}")
            lines.append(f"**Availability:** {availability}")
            if t["parameters"]:
                lines.append("**Parameters:**")
                lines.extend(t["parameters"])
            else:
                lines.append("**Parameters:** None")
            lines.append("")

    return "\n".join(lines)

