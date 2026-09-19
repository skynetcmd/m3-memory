"""`_META_TOOLS` has exactly ONE owner, and it is importable.

The lazy-mode startup surface is "meta-tools + essentials". `is_essential()` in
bin/tool_domains.py owns the essentials half and is already imported by every
consumer. The meta half lived as a LOCAL inside
`memory_bridge._register_initial_tools()`, so the documented cross-module read --
`getattr(memory_bridge, "_META_TOOLS", None)` in bin/measure_tool_tokens.py:104 --
always returned None and silently fell back to a hardcoded literal in that script.

That is the failure mode measure_tool_tokens.py's own comment warns about ("a
second hand-maintained list would drift from it silently"). The two lists agreed,
so nothing was broken -- but the seam was undefended, which is §3's "a declared
limit with no enforcement site is a lie the tests cannot see".

Hoisting it to module scope makes the seam real. These tests make it stay real:
a future author who re-localizes the constant, or adds a meta-tool to one list
only, gets a red test instead of a silent divergence.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

_BRIDGE_PATH = os.path.join(os.path.dirname(__file__), "..", "bin", "memory_bridge.py")


def test_meta_tools_is_importable_at_module_scope():
    """The cross-module read documented in measure_tool_tokens.py must work.

    This is the assertion that was silently false: `getattr` returned None.
    """
    import memory_bridge

    meta = getattr(memory_bridge, "_META_TOOLS", None)
    assert meta is not None, (
        "_META_TOOLS is not reachable at module scope -- measure_tool_tokens.py's "
        "getattr() will silently fall back to its own hardcoded literal."
    )
    assert isinstance(meta, (set, frozenset)), f"expected a set, got {type(meta)!r}"
    assert meta == {"tools_list_domains", "tools_load_domain"}


def test_meta_tools_is_not_shadowed_by_a_local():
    """A module-level constant is worthless if the function redefines it.

    Greps the source rather than the object because a shadowing local is
    invisible from the imported module -- the exact reason this drifted.
    """
    with open(_BRIDGE_PATH, encoding="utf-8") as fh:
        src = fh.read()

    assignments = re.findall(r"^(\s*)_META_TOOLS\s*=", src, flags=re.MULTILINE)
    assert assignments, "_META_TOOLS assignment vanished from memory_bridge.py"
    assert len(assignments) == 1, (
        f"_META_TOOLS is assigned {len(assignments)} times; it must have exactly "
        "one owner. A second assignment (especially an indented one inside "
        "_register_initial_tools) re-creates the silent-drift bug."
    )
    assert assignments[0] == "", (
        "_META_TOOLS is assigned at an indented scope (a function local). It must "
        "be module-level so external consumers can read it."
    )


def test_meta_tools_are_real_catalog_tools():
    """A meta-tool that isn't in the catalog would be registered as nothing.

    Guards a typo in the constant: the names are used for an identity check
    (`spec.name in _META_TOOLS`), which fails silently on a misspelling.
    """
    import memory_bridge
    import mcp_tool_catalog

    catalog = {spec.name for spec in mcp_tool_catalog.TOOLS}
    missing = memory_bridge._META_TOOLS - catalog
    assert not missing, f"_META_TOOLS names absent from the catalog: {sorted(missing)}"


def test_meta_tools_are_not_also_essentials():
    """The two halves of the startup set must be disjoint.

    Not a correctness bug if they overlap (the union is taken), but an overlap
    means one of the two lists is lying about what it owns.
    """
    import memory_bridge
    import tool_domains

    overlap = {n for n in memory_bridge._META_TOOLS if tool_domains.is_essential(n)}
    assert not overlap, (
        f"{sorted(overlap)} is claimed by BOTH _META_TOOLS and is_essential(); "
        "one of the two owners is wrong."
    )


def test_startup_surface_matches_the_documented_count():
    """The startup set is 10 tools: 2 meta + 8 essentials.

    Pins the number quoted in _register_initial_tools()'s docstring and measured
    by bin/measure_tool_tokens.py (3,929 MCP-wire tokens). If this count moves,
    that docstring and the OpenClaw toolFilter both need re-deriving -- which is
    the point of failing here.
    """
    import memory_bridge
    import mcp_tool_catalog
    import tool_domains

    essentials = {s.name for s in mcp_tool_catalog.TOOLS if tool_domains.is_essential(s.name)}
    assert len(essentials) == 8, f"expected 8 essentials, got {len(essentials)}: {sorted(essentials)}"
    assert len(memory_bridge._META_TOOLS) == 2
    assert len(essentials | memory_bridge._META_TOOLS) == 10
