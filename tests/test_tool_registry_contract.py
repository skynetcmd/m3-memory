"""Contract test over mcp_tool_catalog.TOOLS protecting all catalog consumers.

Every consumer of the catalog — the FastMCP bridge (memory_bridge), the CLI
codegen, the m3_call/m3_index dispatcher, and the orchestrator dispatch loop —
relies on a handful of structural invariants that no single tool definition is
forced to honor on its own. This file pins them so a malformed ToolSpec (or a
new complex-arg tool the CLI codegen hasn't special-cased) fails CI loudly.
"""
from __future__ import annotations

import os
import sys

# conftest.py already puts bin/ on sys.path; belt-and-suspenders for isolation.
_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import mcp_tool_catalog
import tool_domains


def test_every_spec_has_a_valid_json_schema_parameters_object():
    """parameters must be a JSON-Schema object with a properties dict — the
    bridge, codegen, and dispatcher all index into parameters['properties']."""
    bad = []
    for spec in mcp_tool_catalog.TOOLS:
        params = spec.parameters
        if not isinstance(params, dict):
            bad.append((spec.name, "parameters is not a dict"))
            continue
        if params.get("type") != "object":
            bad.append((spec.name, f"parameters.type={params.get('type')!r} (want 'object')"))
        if not isinstance(params.get("properties"), dict):
            bad.append((spec.name, "parameters.properties is not a dict"))
    assert not bad, f"malformed parameters schemas: {bad}"


def test_every_spec_has_callable_impl_and_nonempty_description():
    bad = []
    for spec in mcp_tool_catalog.TOOLS:
        if not callable(spec.impl):
            bad.append((spec.name, "impl is not callable"))
        if not (spec.description and spec.description.strip()):
            bad.append((spec.name, "description is empty"))
    assert not bad, f"specs failing impl/description contract: {bad}"


def test_every_spec_maps_to_a_known_domain():
    """No tool should silently fall through to the 'admin' catch-all without an
    explicit prefix rule in tool_domains._DOMAIN_PREFIXES. Same logic as
    tests/test_lazy_tool_loading.py::test_every_catalog_tool_maps_to_a_known_domain."""
    intentional_admin_prefixes = {
        p for p, d in tool_domains._DOMAIN_PREFIXES if d == "admin"
    }
    orphans = []
    for spec in mcp_tool_catalog.TOOLS:
        # Meta-tools (tools_*) intentionally land in admin via the prefix fallback.
        if spec.name.startswith("tools_"):
            continue
        if tool_domains.domain_of_tool(spec.name) != "admin":
            continue
        if not any(spec.name == p or spec.name.startswith(p)
                   for p in intentional_admin_prefixes):
            orphans.append(spec.name)
    assert not orphans, (
        f"Tool(s) fell through to the 'admin' catch-all without an explicit "
        f"prefix rule in tool_domains._DOMAIN_PREFIXES: {sorted(orphans)}."
    )


def _is_complex_arg_spec(spec) -> bool:
    """A spec is 'complex-arg' if any property is an object, an array-of-objects,
    or uses oneOf/anyOf — the cases the CLI codegen must special-case."""
    props = (spec.parameters or {}).get("properties", {}) or {}
    for pdef in props.values():
        if not isinstance(pdef, dict):
            continue
        if pdef.get("type") == "object":
            return True
        if pdef.get("type") == "array":
            items = pdef.get("items") or {}
            if isinstance(items, dict) and items.get("type") == "object":
                return True
        if "oneOf" in pdef or "anyOf" in pdef:
            return True
    return False


def test_complex_arg_tool_set_is_pinned():
    """Pin the exact set of complex-arg tools the CLI codegen special-cases.

    A new tool with an object/array-of-object/oneOf/anyOf argument that isn't on
    this list fails here — forcing the author to teach the codegen about it.

    m3_call and m3_index are dispatcher tools (m3_call has an 'args' object + a
    'batch' array by design); they are excluded from this check.
    """
    dispatcher_excluded = {"m3_call", "m3_index"}
    found = {
        spec.name for spec in mcp_tool_catalog.TOOLS
        if spec.name not in dispatcher_excluded and _is_complex_arg_spec(spec)
    }
    expected = {
        "notify", "agent_register", "curate_chatlog_apply", "memory_update_bulk",
        "curate_memory_apply", "memory_link_bulk", "task_create", "task_update",
    }
    assert found == expected, (
        f"complex-arg tool set drifted — extra (teach the CLI codegen, then add "
        f"here): {sorted(found - expected)}; missing (removed/changed a tool? "
        f"drop from the pinned set): {sorted(expected - found)}"
    )
