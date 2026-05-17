"""Regression test for the lazy domain-loading MCP tool catalog.

Covers the code paths introduced in commit 0cfbb9f (feat(mcp): lazy
domain-loading cuts startup tool catalog 85%) and 73ce2d1's surrounding
plumbing:

  bin/tool_domains.py     — name → domain mapping, essentials allowlist
  bin/tool_loader.py      — meta-tool impls + bridge-callback registry
  bin/mcp_tool_catalog.py — `tools_list_domains`, `tools_load_domain` meta-tools
  bin/memory_bridge.py    — gated registration + the register-domain callback

Goal: catch regressions that would silently re-enable the legacy eager-mode
behavior, drop the meta-tools, or leave catalog tools un-bucketed in any
domain.

This test does NOT spin up a FastMCP server — it works against the catalog
and the helper modules directly. The bridge-side integration is covered by
a separate test (`test_lazy_tool_loading_bridge_integration`) that imports
memory_bridge under controlled env vars.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from typing import Iterable

import pytest

# conftest.py already puts bin/ on sys.path. Belt-and-suspenders so this
# file is also importable in isolation:
_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import mcp_tool_catalog
import tool_domains
import tool_loader


# ── tool_domains.py ──────────────────────────────────────────────────────────

def test_every_catalog_tool_maps_to_a_known_domain():
    """No tool should fall through to the 'admin' catch-all by accident.

    Domain assignment is name-prefix based. Adding a new tool with a name
    that doesn't match any prefix would silently bucket it under 'admin',
    where the agent would never load it on demand for the intuitive
    domain. This test forces the new-tool author to extend
    `_DOMAIN_PREFIXES` explicitly.
    """
    # Build the set of "intentional admin tools" — name prefixes explicitly
    # routed to admin in _DOMAIN_PREFIXES.
    intentional_admin_prefixes = {
        p for p, d in tool_domains._DOMAIN_PREFIXES if d == "admin"
    }

    orphans = []
    for spec in mcp_tool_catalog.TOOLS:
        # Meta-tools (tools_*) intentionally land in admin via the prefix
        # fallback — they're cross-cutting.
        if spec.name.startswith("tools_"):
            continue
        domain = tool_domains.domain_of_tool(spec.name)
        if domain != "admin":
            continue
        # If we got here it's an admin tool — confirm there's an explicit
        # prefix rule (not just the trailing fallback).
        if not any(spec.name == p or spec.name.startswith(p) for p in intentional_admin_prefixes):
            orphans.append(spec.name)

    assert not orphans, (
        f"Tool(s) fell through to the 'admin' catch-all without an explicit "
        f"prefix rule in tool_domains._DOMAIN_PREFIXES: {sorted(orphans)}. "
        f"Add a prefix entry to assign them to the right domain."
    )


def test_essentials_are_real_catalog_tools():
    """Every entry in ESSENTIAL_TOOL_NAMES must exist in the catalog.

    A typo in the essentials set would silently drop a tool from the
    always-on surface, making `m3 setup` startup look unhealthy.
    """
    catalog_names = {t.name for t in mcp_tool_catalog.TOOLS}
    missing = tool_domains.ESSENTIAL_TOOL_NAMES - catalog_names
    assert not missing, (
        f"Essentials set references tool(s) not in the catalog: {sorted(missing)}"
    )


def test_essentials_include_search_for_each_primary_store():
    """The 80/20 promise: every primary store has a search tool in essentials.

    If we ever drop one (say, dropping `chatlog_search`), the README claim
    about "essentials cover what most sessions need" no longer holds. This
    test pins the promise.
    """
    primary_search_tools = {"memory_search", "files_search", "chatlog_search"}
    assert primary_search_tools <= tool_domains.ESSENTIAL_TOOL_NAMES, (
        f"Essentials missing primary-store search tools: "
        f"{sorted(primary_search_tools - tool_domains.ESSENTIAL_TOOL_NAMES)}"
    )


def test_domain_descriptions_cover_every_domain_seen_in_catalog():
    """DOMAIN_DESCRIPTIONS feeds the `tools_list_domains` response. Every
    domain that actually has tools must have a description, or the agent
    will see a domain in the list with no text."""
    seen = set(tool_domains.domain_of_tool(t.name) for t in mcp_tool_catalog.TOOLS)
    described = set(tool_domains.DOMAIN_DESCRIPTIONS.keys())
    missing = seen - described
    assert not missing, (
        f"Domain(s) have catalog tools but no description in "
        f"DOMAIN_DESCRIPTIONS: {sorted(missing)}"
    )


def test_group_by_domain_is_complete():
    """`group_by_domain` over every catalog name should account for every
    catalog tool — no silent drops."""
    names = [t.name for t in mcp_tool_catalog.TOOLS]
    grouped = tool_domains.group_by_domain(names)
    flat = [n for ns in grouped.values() for n in ns]
    assert sorted(flat) == sorted(names), (
        "group_by_domain dropped or duplicated tools — set diff: "
        f"missing={sorted(set(names) - set(flat))}, "
        f"extra={sorted(set(flat) - set(names))}"
    )


# ── tool_loader.py meta-tool impls ───────────────────────────────────────────

def test_list_domains_returns_well_formed_json():
    """`tools_list_domains` impl returns a JSON string with a `domains`
    array, each entry having name + description + tool_count."""
    raw = tool_loader.list_domains()
    data = json.loads(raw)
    assert "domains" in data, f"missing 'domains' key in {data}"
    for entry in data["domains"]:
        assert "name" in entry
        assert "description" in entry
        assert "tool_count" in entry
        assert isinstance(entry["tool_count"], int)
        assert entry["tool_count"] >= 0
    # At least one domain must have tools — otherwise something pruned the catalog.
    assert any(e["tool_count"] > 0 for e in data["domains"])


def test_load_domain_with_unknown_name_returns_error():
    """Calling `tools_load_domain(domain='nope')` shouldn't crash; it
    should return a usable error message the agent can read."""
    out = tool_loader.load_domain("nope")
    assert "Error" in out or "unknown" in out.lower(), out


def test_load_domain_without_bridge_callback_returns_explainer():
    """If the bridge hasn't bound a callback (e.g. running outside the
    MCP server), `tools_load_domain` should explain that rather than
    silently failing."""
    # Save + clear the callback, then restore.
    saved = tool_loader._register_domain_callback
    tool_loader._register_domain_callback = None
    try:
        raw = tool_loader.load_domain("memory")
        data = json.loads(raw)
        assert data.get("status") == "callback-not-bound", data
    finally:
        tool_loader._register_domain_callback = saved


def test_load_domain_with_bound_callback_invokes_it():
    """Bind a stub callback, call `tools_load_domain`, confirm the
    callback was hit and the result is JSON-serialized."""
    seen = []
    def stub(domain):
        seen.append(domain)
        return {"domain": domain, "tools_now_available": ["x", "y"], "tools_total_registered": 8, "schemas": []}

    saved = tool_loader._register_domain_callback
    tool_loader.set_register_domain_callback(stub)
    try:
        raw = tool_loader.load_domain("files")
        data = json.loads(raw)
        assert seen == ["files"], seen
        assert data["tools_now_available"] == ["x", "y"]
        assert data["tools_total_registered"] == 8
    finally:
        tool_loader._register_domain_callback = saved


# ── mcp_tool_catalog.py — meta-tool entries ──────────────────────────────────

def test_meta_tools_registered_in_catalog():
    """Both `tools_list_domains` and `tools_load_domain` must appear in
    `mcp_tool_catalog.TOOLS` — without them, lazy mode has no escape hatch."""
    names = {t.name for t in mcp_tool_catalog.TOOLS}
    assert "tools_list_domains" in names
    assert "tools_load_domain" in names


def test_meta_tools_are_default_allowed():
    """If meta-tools end up `default_allowed=False` they'd be hidden by
    the destructive-tool gate and lazy mode would break."""
    by_name = {t.name: t for t in mcp_tool_catalog.TOOLS}
    for n in ("tools_list_domains", "tools_load_domain"):
        assert by_name[n].default_allowed, f"{n} must be default_allowed=True"


def test_load_domain_spec_requires_domain_arg():
    """The `tools_load_domain` schema must list `domain` as required —
    otherwise the agent might call it with no arg and the impl would
    raise a `KeyError`."""
    by_name = {t.name: t for t in mcp_tool_catalog.TOOLS}
    params = by_name["tools_load_domain"].parameters
    assert "domain" in params.get("required", []), (
        f"tools_load_domain.parameters.required missing 'domain': {params}"
    )


# ── Integration with memory_bridge ───────────────────────────────────────────

def _reload_bridge_with_env(env_overrides: dict) -> object:
    """Import / re-import memory_bridge with controlled env. Returns the
    fresh module. Each call gets a clean FastMCP instance.

    We can't just monkey-patch os.environ because the bridge reads
    M3_TOOLS_LAZY at import time. We pop the cached module so the next
    import picks up the new env.
    """
    saved_env = {k: os.environ.get(k) for k in env_overrides}
    for k, v in env_overrides.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    saved_modules = {}
    for mod in ("memory_bridge", "tool_loader"):
        if mod in sys.modules:
            saved_modules[mod] = sys.modules.pop(mod)

    try:
        import memory_bridge
        return memory_bridge
    finally:
        # Restore env so the rest of the test session is unaffected.
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # Drop the reload so the cached version doesn't leak into other tests.
        for mod in ("memory_bridge",):
            sys.modules.pop(mod, None)
        # Restore any modules we evicted.
        for mod, val in saved_modules.items():
            if mod not in sys.modules:
                sys.modules[mod] = val


def test_bridge_lazy_mode_registers_only_essentials_and_meta():
    """With M3_TOOLS_LAZY=1 (or unset — lazy is the default), only the
    essentials + the two meta-tools should be registered."""
    mb = _reload_bridge_with_env({"M3_TOOLS_LAZY": "1"})
    assert mb._LAZY_MODE is True
    expected = tool_domains.ESSENTIAL_TOOL_NAMES | {"tools_list_domains", "tools_load_domain"}
    assert mb._REGISTERED == expected, (
        f"lazy mode registered the wrong set: "
        f"missing={sorted(expected - mb._REGISTERED)}, "
        f"extra={sorted(mb._REGISTERED - expected)}"
    )


def test_bridge_eager_mode_registers_all_tools():
    """With M3_TOOLS_LAZY=0, every catalog tool should be registered up-front
    (the legacy behavior)."""
    mb = _reload_bridge_with_env({"M3_TOOLS_LAZY": "0"})
    assert mb._LAZY_MODE is False
    all_names = {t.name for t in mcp_tool_catalog.TOOLS}
    assert mb._REGISTERED == all_names, (
        f"eager mode missed: {sorted(all_names - mb._REGISTERED)}"
    )


def test_bridge_lazy_mode_domain_callback_registers_domain():
    """Domain callback should add exactly that domain's tools, return
    schemas, and be idempotent on a second call."""
    mb = _reload_bridge_with_env({"M3_TOOLS_LAZY": "1"})
    baseline = set(mb._REGISTERED)

    # Pick a domain we know has tools and isn't already registered.
    target = "entity"
    assert target in tool_domains.DOMAIN_DESCRIPTIONS

    result = mb._register_domain_callback(target)
    assert result["domain"] == target
    expected_new = set(tool_domains.domain_tool_names(
        [t.name for t in mcp_tool_catalog.TOOLS], target
    )) - baseline
    assert set(result["tools_now_available"]) == expected_new
    assert mb._REGISTERED == baseline | expected_new

    # Schemas list parallels tools_now_available.
    assert len(result["schemas"]) == len(result["tools_now_available"])
    for s in result["schemas"]:
        assert {"name", "description", "inputSchema"} <= set(s.keys())

    # Second call: idempotent — no new tools registered.
    second = mb._register_domain_callback(target)
    assert second["tools_now_available"] == []
    assert mb._REGISTERED == baseline | expected_new
