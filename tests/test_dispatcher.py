"""Tests for the generic MCP tool dispatcher (m3_call / m3_index).

Covers the code paths added in bin/mcp_tool_catalog.py:

  m3_index_impl            — structured catalog metadata (name/domain/summary/args)
  m3_call_impl             — generic single/batch dispatch by tool name
  execute_tool_structured  — native-return execution path m3_call routes through
  _DISPATCH_EXCLUDE        — meta/dispatcher tools that can't be reached via m3_call
  _DESTRUCTIVE_ALLOWED     — destructive-gate on default_allowed=False tools

These tests work directly against the catalog — no FastMCP server. Async impls
are driven with asyncio.run() inside sync test functions (the pattern the rest
of the suite uses, e.g. tests/test_memory_supersede.py) so there's no plugin
dependency on a particular pytest-asyncio mode.

The read tool used throughout is files_stats against the shipped
memory/files_database.db — a real, side-effect-free corpus read.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

# conftest.py already puts bin/ on sys.path; belt-and-suspenders for isolation.
_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import mcp_tool_catalog
import tool_domains


# Path to the shipped files corpus DB, resolved relative to the repo root so
# the test runs from any cwd.
_FILES_DB = os.path.normpath(os.path.join(_HERE, "..", "memory", "files_database.db"))


def _files_stats_args() -> dict:
    return {"database": _FILES_DB}


# ── m3_index ──────────────────────────────────────────────────────────────────

def test_m3_index_returns_all_non_excluded_tools():
    """m3_index() (no domain) lists every catalog tool except the four
    dispatcher/meta tools in _DISPATCH_EXCLUDE."""
    data = json.loads(mcp_tool_catalog.m3_index_impl())
    assert "count" in data and "tools" in data
    names = {t["name"] for t in data["tools"]}

    expected = {
        s.name for s in mcp_tool_catalog.TOOLS
        if s.name not in mcp_tool_catalog._DISPATCH_EXCLUDE
    }
    assert names == expected, (
        f"m3_index drift: extra={sorted(names - expected)}, "
        f"missing={sorted(expected - names)}"
    )
    assert data["count"] == len(data["tools"])

    # Excluded tools must never appear.
    for excluded in ("m3_call", "m3_index", "tools_load_domain", "tools_list_domains"):
        assert excluded not in names, f"{excluded} leaked into m3_index"

    # The universal injected "database" arg is omitted from every tool's args.
    for t in data["tools"]:
        arg_names = {a["name"] for a in t["args"]}
        assert "database" not in arg_names, f"{t['name']} exposes the injected 'database' arg"


def test_m3_index_domain_filter_is_strict_subset():
    """m3_index('files') is a strict, non-empty subset of the full index,
    and every row is domain == 'files'."""
    full = {t["name"] for t in json.loads(mcp_tool_catalog.m3_index_impl())["tools"]}
    files = json.loads(mcp_tool_catalog.m3_index_impl("files"))["tools"]
    files_names = {t["name"] for t in files}

    assert files_names, "expected at least one tool in the 'files' domain"
    assert files_names < full, "domain-filtered set must be a strict subset of the full index"
    for t in files:
        assert t["domain"] == "files", f"{t['name']} has domain {t['domain']!r}, expected 'files'"


# ── m3_call: single read tool ───────────────────────────────────────────────────

def test_m3_call_single_read_tool_ok():
    """A single files_stats dispatch returns ok with a structured result that
    carries the corpus counters."""
    out = asyncio.run(mcp_tool_catalog.m3_call_impl(
        tool="files_stats", args=_files_stats_args()))
    data = json.loads(out)
    assert data["ok"] is True
    assert data["tool"] == "files_stats"
    assert isinstance(data["result"], dict)
    assert "file_nodes_total" in data["result"]


def test_m3_call_parity_with_execute_tool_structured():
    """m3_call(tool, args) must produce the SAME underlying result as calling
    execute_tool_structured(spec, args, "") directly — both go through the
    identical gate+db-context+validation path. (We do NOT compare against a
    bare spec.impl() call, which would skip db-context and validation.)"""
    spec = mcp_tool_catalog.get_tool("files_stats")
    assert spec is not None

    # execute_tool_structured mutates the args dict in place (pops 'database',
    # filters keys), so hand each call its own copy.
    direct = asyncio.run(mcp_tool_catalog.execute_tool_structured(
        spec, _files_stats_args(), ""))

    dispatched = json.loads(asyncio.run(mcp_tool_catalog.m3_call_impl(
        tool="files_stats", args=_files_stats_args())))

    assert dispatched["ok"] is True
    assert dispatched["result"] == direct, (
        "m3_call result payload diverged from execute_tool_structured"
    )


def test_m3_call_dry_run_does_not_execute():
    """dry_run=True returns ok with the dry_run marker and never calls the impl
    (validation + gate only)."""
    out = asyncio.run(mcp_tool_catalog.m3_call_impl(
        tool="files_stats", args=_files_stats_args(), dry_run=True))
    data = json.loads(out)
    assert data["ok"] is True
    assert data["tool"] == "files_stats"
    assert isinstance(data["result"], dict)
    assert data["result"].get("dry_run") is True
    # The dry-run marker shape is the sentinel, NOT the real files_stats output.
    assert "file_nodes_total" not in data["result"]


# ── m3_call: error shapes ────────────────────────────────────────────────────────

def test_m3_call_unknown_tool():
    out = asyncio.run(mcp_tool_catalog.m3_call_impl(tool="no_such_tool", args={}))
    data = json.loads(out)
    assert data["ok"] is False
    assert data["error"] == "unknown_tool"
    assert "did_you_mean" in data  # present (possibly empty list)


def test_m3_call_destructive_gated_when_env_unset():
    """memory_delete is default_allowed=False. With MCP_PROXY_ALLOW_DESTRUCTIVE
    unset (the test process default), dispatching it is gated BEFORE the impl
    runs — so we can safely pass a real-looking full UUID."""
    # Guard against a polluted environment: _DESTRUCTIVE_ALLOWED is read at
    # import time, so the relevant knob is the module global, not os.environ.
    assert mcp_tool_catalog._DESTRUCTIVE_ALLOWED is False, (
        "this test assumes destructive dispatch is disabled; "
        "MCP_PROXY_ALLOW_DESTRUCTIVE appears to have been set at import time"
    )
    out = asyncio.run(mcp_tool_catalog.m3_call_impl(
        tool="memory_delete",
        args={"id": "11111111-2222-3333-4444-555555555555"}))
    data = json.loads(out)
    assert data["ok"] is False
    assert data["error"] == "destructive_gated"


def test_m3_call_destructive_allowed_when_flag_monkeypatched(monkeypatch):
    """With _DESTRUCTIVE_ALLOWED monkeypatched True, the gate is lifted and the
    call proceeds past it (it may still fail in the impl because the UUID isn't
    a real row — but it is NOT destructive_gated)."""
    monkeypatch.setattr(mcp_tool_catalog, "_DESTRUCTIVE_ALLOWED", True)
    out = asyncio.run(mcp_tool_catalog.m3_call_impl(
        tool="memory_delete",
        args={"id": "11111111-2222-3333-4444-555555555555",
              "database": _FILES_DB}))
    data = json.loads(out)
    # Past the gate: either it ran (ok True) or the impl reported call_failed —
    # the one thing it must NOT be is destructive_gated.
    assert data.get("error") != "destructive_gated"
    assert "ok" in data


def test_m3_call_missing_tool():
    """Neither tool nor batch given → missing_tool."""
    out = asyncio.run(mcp_tool_catalog.m3_call_impl())
    data = json.loads(out)
    assert data["ok"] is False
    assert data["error"] == "missing_tool"


def test_m3_call_excluded_tool_not_dispatchable():
    """The dispatcher must refuse to recurse into itself / the meta-tools."""
    out = asyncio.run(mcp_tool_catalog.m3_call_impl(tool="m3_index", args={}))
    data = json.loads(out)
    assert data["ok"] is False
    assert data["error"] == "not_dispatchable"


# ── m3_call: batch ───────────────────────────────────────────────────────────────

def test_m3_call_batch_preserves_order_and_isolates_failures():
    """A two-item batch [good read tool, unknown tool] returns both results in
    order: [0] ok, [1] unknown_tool. One failure does not abort the rest."""
    out = asyncio.run(mcp_tool_catalog.m3_call_impl(batch=[
        {"tool": "files_stats", "args": _files_stats_args()},
        {"tool": "no_such_tool", "args": {}},
    ]))
    data = json.loads(out)
    assert "batch" in data
    results = data["batch"]
    assert len(results) == 2
    assert results[0]["ok"] is True
    assert results[0]["tool"] == "files_stats"
    assert results[1]["ok"] is False
    assert results[1]["error"] == "unknown_tool"


def test_m3_call_batch_too_large():
    """A batch over the 100-item cap is rejected wholesale."""
    big = [{"tool": "files_stats", "args": {}} for _ in range(101)]
    out = asyncio.run(mcp_tool_catalog.m3_call_impl(batch=big))
    data = json.loads(out)
    assert data["ok"] is False
    assert data["error"] == "batch_too_large"


# ── m3_call: async tool dispatch ────────────────────────────────────────────────

def test_m3_call_async_tool_dispatches():
    """memory_search is an async impl. Dispatching it through m3_call must not
    raise — it returns valid JSON with an 'ok' key (True if the DB/embedder are
    available, or a structured call_failed otherwise). We don't assert on search
    content, only that the async path round-trips cleanly."""
    out = asyncio.run(mcp_tool_catalog.m3_call_impl(
        tool="memory_search",
        args={"query": "x", "database": _FILES_DB}))
    data = json.loads(out)  # must be valid JSON
    assert "ok" in data
    if data["ok"] is False:
        # If it failed, it must be a structured impl failure, not a dispatch bug.
        assert data["error"] == "call_failed"
    else:
        assert data["tool"] == "memory_search"
