"""Startup tool-set probe — which tools load at startup, and WHY that set?

The MCP startup surface is user-configurable (`tools_config`): an env var, a
`.tools_config.json`, or the shipped default. A resolved set the operator cannot
explain is a set they cannot fix, so this probe prints both the set AND the
precedence level it came from (DESIGN §3 — observable, not guessed).

It exists because the failure it catches is silent by nature. A startup set that
is quietly wrong looks, from inside a session, exactly like a tool that does not
exist: the agent finds no such tool, concludes the capability is missing, and
routes around it to a shell. Nothing errors. The only way to notice is to ask.

FAILS the doctor run (returns 1) when the config cannot be honored — an unknown
tool name, an empty set, or a set missing the escape hatch. Those are not
degraded states to report and move past: m3 would start with a surface the user
did not ask for, which is the one outcome tools_config exists to prevent.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("memory.doctor.startup_tools_probe")


def run(brief: bool = False) -> int:
    """Report the resolved startup tool set and its source. 0 ok, 1 unusable."""
    def _b(line: str) -> None:
        if brief:
            print(line)

    try:
        import tools_config
    except ImportError as e:  # pragma: no cover - import surface guard
        print(f"[startup-tools] tools_config unavailable: {e}")
        return 0

    try:
        names, source = tools_config.resolve_startup_tools()
    except tools_config.ToolsConfigError as e:
        # The config names a set we cannot deliver. Loud, and fail the phase.
        print("[startup-tools] FAIL — configured startup set cannot be used:")
        for line in str(e).splitlines():
            print(f"   {line}")
        return 1

    if names is None:
        _b("[startup-tools] all tools (lazy loading disabled)")
        if not brief:
            print(f"[startup-tools] ALL tools registered at startup  (source: {source})")
            print("   Every schema loads eagerly. This is the pre-slim behavior and "
                  "costs the full catalog in tokens.")
        return 0

    # Validate against the real catalog here rather than in tools_config: that
    # module must not import the catalog (memory_bridge imports IT at startup,
    # so an import back would invert the dependency).
    try:
        import mcp_tool_catalog
        catalog_names = {t.name for t in mcp_tool_catalog.TOOLS}
    except Exception as e:  # noqa: BLE001 — report, don't mask
        print(f"[startup-tools] could not load the catalog to validate names: {e}")
        catalog_names = set()

    if catalog_names:
        try:
            tools_config.validate_against_catalog(names, catalog_names)
        except tools_config.ToolsConfigError as e:
            print("[startup-tools] FAIL — configured startup set names unknown tools:")
            for line in str(e).splitlines():
                print(f"   {line}")
            return 1

    _b(f"[startup-tools] {len(names)} tools (source: {source})")
    if brief:
        return 0

    print(f"[startup-tools] {len(names)} tools registered at startup "
          f"(source: {source})")
    for name in names:
        marker = "  (escape hatch)" if name in tools_config.ESCAPE_HATCH else ""
        print(f"   - {name}{marker}")

    if catalog_names:
        print(f"   catalog holds {len(catalog_names)} tools; the rest are reachable "
              f"via m3_call or tools_load_domain.")
    if source == "shipped default":
        print("   No .tools_config.json — tracking this version's default. "
              "`m3 tools config --init` pins it (and stops tracking upgrades).")
    return 0
