"""Tests for the universal per-call MCP tool timeout (§6 hardening).

Every tool call is bounded: per-call `timeout` arg > M3_TOOL_TIMEOUT env > 30s
default; <= 0 disables. Only async impls are bounded. A timeout fails loud (§3)
with a ToolTimeout naming the tool and budget — never a silent hang.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import mcp_tool_catalog as C  # noqa: E402
from mcp_tool_catalog import ToolSpec  # noqa: E402


def _spec(impl, is_async=True):
    return ToolSpec(
        name="fake_tool",
        description="",
        parameters={"properties": {}},
        impl=impl,
        is_async=is_async,
    )


async def _slow(**_):
    await asyncio.sleep(5)
    return "done"


async def _fast(**_):
    return "quick"


def test_resolve_precedence_and_pop(monkeypatch):
    monkeypatch.delenv("M3_TOOL_TIMEOUT", raising=False)
    # default
    assert C._resolve_tool_timeout({}) == C._DEFAULT_TOOL_TIMEOUT
    # per-call arg wins and is popped
    d = {"timeout": 3, "x": 1}
    assert C._resolve_tool_timeout(d) == 3.0
    assert "timeout" not in d and d == {"x": 1}
    # env override
    monkeypatch.setenv("M3_TOOL_TIMEOUT", "2")
    assert C._resolve_tool_timeout({}) == 2.0
    # per-call still beats env
    assert C._resolve_tool_timeout({"timeout": 7}) == 7.0
    # <= 0 disables
    assert C._resolve_tool_timeout({"timeout": 0}) is None
    assert C._resolve_tool_timeout({"timeout": -1}) is None
    # malformed -> safe default (fail safe)
    monkeypatch.delenv("M3_TOOL_TIMEOUT", raising=False)
    assert C._resolve_tool_timeout({"timeout": "abc"}) == C._DEFAULT_TOOL_TIMEOUT


def test_per_tool_timeout_s_precedence(monkeypatch):
    monkeypatch.delenv("M3_TOOL_TIMEOUT", raising=False)
    slow_spec = ToolSpec(
        name="slow_tool", description="", parameters={"properties": {}},
        impl=_fast, is_async=True, timeout_s=120,
    )
    # spec.timeout_s is used when no per-call arg and no env
    assert C._resolve_tool_timeout({}, slow_spec) == 120.0
    # per-call arg still beats the per-tool default
    assert C._resolve_tool_timeout({"timeout": 5}, slow_spec) == 5.0
    # env still beats the per-tool default
    monkeypatch.setenv("M3_TOOL_TIMEOUT", "45")
    assert C._resolve_tool_timeout({}, slow_spec) == 45.0
    monkeypatch.delenv("M3_TOOL_TIMEOUT", raising=False)
    # a spec without timeout_s falls through to the global default
    plain = ToolSpec(name="plain", description="", parameters={"properties": {}},
                     impl=_fast, is_async=True)
    assert C._resolve_tool_timeout({}, plain) == C._DEFAULT_TOOL_TIMEOUT


def test_known_slow_tools_have_generous_ceiling():
    # The injected per-tool map must reach the real specs (regression against a
    # cold-start search being clipped by the 30s default).
    for name in ("chatlog_search", "memory_search", "m3_index", "enrich_pending"):
        spec = C._BY_NAME[name]
        assert spec.timeout_s is not None and spec.timeout_s > C._DEFAULT_TOOL_TIMEOUT, name


def test_execute_tool_times_out_loud():
    r = asyncio.run(C.execute_tool(_spec(_slow), {"timeout": 1}, "agent"))
    assert "ToolTimeout" in r
    assert "exceeded 1s" in r
    assert "fake_tool" in r


def test_execute_tool_fast_under_budget():
    r = asyncio.run(C.execute_tool(_spec(_fast), {"timeout": 5}, "agent"))
    assert r == "quick"


def test_structured_timeout_disabled_runs_to_completion():
    async def med(**_):
        await asyncio.sleep(0.1)
        return {"ok": True}

    r = asyncio.run(C.execute_tool_structured(_spec(med), {"timeout": 0}, "agent"))
    assert r == {"ok": True}


def test_timeout_injected_into_every_spec():
    """The universal timeout param is present on real catalog tools so agents
    can discover it (§12 tool-shape)."""
    for spec in C.TOOLS[:20]:
        props = spec.parameters.get("properties", {})
        assert "timeout" in props, f"{spec.name} missing timeout param"
        assert props["timeout"]["type"] == "number"
