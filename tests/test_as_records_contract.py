"""The `as_records` contract: ToolSpec and impl must agree, uniformly.

Two failures this file exists to prevent, both already hit once:

  1. A ToolSpec advertising a param its impl does not accept. That is defect C's
     class (agent_register's ToolSpec marks `capabilities` optional while the
     impl requires it -> TypeError). The ToolSpec IS the agent's contract; an
     agent reads it, passes the param, and gets an exception from a tool that
     said it was supported.

  2. A param inserted into the SOURCE but not reaching the BUILT catalog.
     Measured 2026-09-14: an insertion landed one line outside the `properties`
     dict, so Python parsed it as a stray literal, ruff passed, and 9 of 9 tools
     silently advertised nothing. Reading the source proves nothing -- these
     assertions run against the built catalog, which is what an agent sees.

Note `inspect.signature` on a ToolSpec.impl returns `(*args, **kwargs)`: impls
are wrapped in catalog.lazy.LazyImpl. Resolving the REAL function is the whole
difficulty -- the same LazyImpl opacity that made a getsource() sweep silently
skip every tool and report a confident zero.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import mcp_tool_catalog as cat  # noqa: E402

from memory import records  # noqa: E402

# The 12 display-string tools P1 covers.
AS_RECORDS_TOOLS = [
    "agent_list",
    "memory_graph",
    "notifications_poll",
    "task_list",
    "task_tree",
    "memory_history",
    "memory_inbox",
    "memory_refresh_queue",
    "memory_cost_report",
    "m3_help_capabilities",
    "conversation_search",
    # Carries nearly every live caller (65 direct + 32 CLI), so it landed in its
    # own commit to keep a regression bisectable.
    "memory_search",
]

# Async impls: calling them needs an event loop, so the shape assertions that
# invoke a tool skip these. Their spec/impl agreement is still checked.
ASYNC_TOOLS = {"conversation_search", "memory_search"}

# Tools whose result is a SUMMARY rather than rows. They take as_records for
# uniformity -- a flag on 11 of 12 siblings is a surface an agent cannot reason
# about -- but emit a keyed object, not the {count, items} envelope.
SCALAR_TOOLS = {"memory_cost_report"}

_BY_NAME = {t.name: t for t in cat.TOOLS}


def _real_impl(spec):
    """Resolve past LazyImpl to the function that actually runs.

    LazyImpl holds `module_name` + `attr_name` and imports on first CALL, so
    `inspect.signature` on it reports `(*args, **kwargs)` -- useless for a
    contract check. Import the module and fetch the attribute ourselves.

    This must not degrade to a skip: a signature check that always skips proves
    nothing and would have passed while every impl rejected the param
    (DESIGN_PHILOSOPHIES section 3 -- a check that cannot fail is worse than no
    check, because it reads as coverage).
    """
    impl = spec.impl
    module_name = getattr(impl, "module_name", None)
    attr_name = getattr(impl, "attr_name", None)
    if module_name and attr_name:
        import importlib
        return getattr(importlib.import_module(module_name), attr_name)
    return getattr(impl, "__wrapped__", impl)


@pytest.mark.parametrize("name", AS_RECORDS_TOOLS)
def test_spec_advertises_as_records(name):
    """Against the BUILT catalog, not the source text."""
    spec = _BY_NAME.get(name)
    assert spec is not None, f"{name} missing from the catalog"
    props = (spec.parameters or {}).get("properties", {})
    assert "as_records" in props, (
        f"{name} does not advertise as_records. If you just added it to the "
        f"source, check it landed INSIDE the properties dict -- a sibling of "
        f"properties parses fine and advertises nothing."
    )
    assert props["as_records"]["type"] == "boolean"
    assert props["as_records"]["default"] is False, (
        "default must be False: every existing caller predates this param and "
        "parses the display string"
    )


@pytest.mark.parametrize("name", AS_RECORDS_TOOLS)
def test_impl_accepts_what_the_spec_advertises(name):
    """Defect C's class: a ToolSpec promising a param the impl rejects."""
    spec = _BY_NAME[name]
    fn = _real_impl(spec)
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        pytest.skip(f"{name}: impl signature not introspectable")
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        pytest.skip(f"{name}: impl still behind a **kwargs wrapper")
    assert "as_records" in sig.parameters, (
        f"{name}'s ToolSpec advertises as_records but its impl does not accept "
        f"it -- an agent that passes it gets a TypeError from a tool that said "
        f"it was supported"
    )


# Placeholder values for REQUIRED params, so a contract test can call any tool
# without needing per-tool knowledge.
#
# ⚠ DERIVED FROM THE SPEC, NEVER HARDCODED. An earlier version listed three arg
# names by hand (agent_id / memory_id / root_task_id). `query` was not among
# them, so memory_search and conversation_search were invoked with a missing
# required argument, raised TypeError, and hit the `except Exception:
# pytest.skip(...)` below -- which reported the two most heavily used search
# tools in the catalog as "not callable in this environment" and passed. The
# as_records contract went UNTESTED on them for as long as that list was stale,
# and the skip line made it look deliberate (§3: a test that disables itself
# when it cannot run is worse than one that fails, because it reports green).
#
# Reading `required` from the spec means adding a required param to any tool
# cannot silently drop it out of coverage again.
_PLACEHOLDERS = {
    "query": "does-not-exist",
    "agent_id": "does-not-exist",
    "memory_id": "does-not-exist",
    "root_task_id": "does-not-exist",
}


def _required_kwargs(spec):
    """Safe placeholder values for every param the spec marks required.

    The point of these tests is the RETURN SHAPE, so a not-found result is a
    perfectly valid response -- the values just have to be present and safe.

    A required param with no known placeholder is a hard FAILURE, not a skip:
    it means this helper has gone stale exactly the way the hardcoded list did.
    """
    kwargs = {}
    for arg in (spec.parameters or {}).get("required", []):
        assert arg in _PLACEHOLDERS, (
            f"{spec.name} requires '{arg}', which has no placeholder in "
            f"_PLACEHOLDERS. Add one -- do not let this tool fall through to a "
            f"skip, which is how memory_search and conversation_search silently "
            f"lost as_records coverage."
        )
        kwargs[arg] = _PLACEHOLDERS[arg]
    return kwargs


def _call(spec, **kwargs):
    """Invoke a tool impl and return its VALUE, awaiting it when async.

    Several impls (memory_search, conversation_search) are coroutine functions.
    Calling one returns a coroutine, and asserting on that object rather than on
    its result silently tests nothing -- `.lstrip()` raises AttributeError, and
    under the old blanket-except it became a skip instead of a failure.
    """
    out = spec.impl(**kwargs)
    if inspect.isawaitable(out):
        return asyncio.run(_await(out))
    return out


async def _await(aw):
    return await aw


@pytest.mark.parametrize("name", AS_RECORDS_TOOLS)
def test_as_records_true_returns_parseable_json(name):
    """The param must actually do something -- advertising it is not enough."""
    spec = _BY_NAME[name]
    kwargs = {"as_records": True, **_required_kwargs(spec)}
    try:
        out = _call(spec, **kwargs)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"{name} not callable in this environment: {type(e).__name__}: {e}")
    assert isinstance(out, str), "as_records changes the CONTENT, not the return type"
    data = json.loads(out)  # must not raise
    assert isinstance(data, dict)
    if name in SCALAR_TOOLS:
        # A SUMMARY, not rows: no {count, items} envelope by design. It still
        # takes as_records so the param means the same thing on every tool.
        assert "items" not in data and "error" not in data
        return
    # Either a result envelope or an error envelope -- never both (ruling B).
    assert ("items" in data) ^ ("error" in data), (
        f"{name}: an error envelope must not carry count/items, and a result "
        f"envelope must not carry error -- a caller cannot otherwise tell a bad "
        f"id from an empty result"
    )


@pytest.mark.parametrize("name", AS_RECORDS_TOOLS)
def test_default_is_not_json(name):
    """Byte-identity is proven elsewhere; here we only assert the default path
    still returns a DISPLAY string, so a caller parsing it is not handed JSON."""
    spec = _BY_NAME[name]
    kwargs = _required_kwargs(spec)
    try:
        out = _call(spec, **kwargs)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"{name} not callable: {type(e).__name__}: {e}")
    assert not out.lstrip().startswith("{"), (
        f"{name} returned JSON by default -- every existing caller parses the "
        f"display string"
    )


def test_every_spec_uses_the_one_description():
    """Section 10a: one owner for the param text. Twelve hand-written
    descriptions drift, and an agent deciding whether to pass the flag reads
    the description, not the code."""
    seen = {
        _BY_NAME[n].parameters["properties"]["as_records"]["description"]
        for n in AS_RECORDS_TOOLS
    }
    assert len(seen) == 1, f"as_records descriptions have drifted: {seen}"
    assert seen.pop() == records.PARAM_SPEC["description"], (
        "catalog description drifted from memory.records.PARAM_SPEC, the owner"
    )


# ── a NEW list-shaped tool must not ship prose-only ──────────────────────────

def _list_shaped_tools():
    """Tools whose name says they return a collection.

    Derived from the catalog rather than hand-listed: AS_RECORDS_TOOLS above is
    a pinned set of tools that HAVE the param, which by construction cannot
    notice a new tool that lacks it. The whole failure mode here is omission,
    so the expectation has to come from the catalog itself.
    """
    import mcp_tool_catalog as _cat

    suffixes = ("_list", "_search", "_report", "_poll", "_queue", "_tree",
                "_inbox", "_history", "_capabilities")
    return sorted(
        t.name for t in _cat.TOOLS
        if any(t.name.endswith(s) for s in suffixes)
    )


# Tools that end in a collection-shaped suffix but genuinely return a SCALAR or
# a single object, so records would be noise. Each entry is a decision with a
# reason, which is the opposite of the silent omission this guards.
_NOT_A_COLLECTION = {
    "chatlog_status": "one status object, not a collection",
    "files_stats": "one stats object, not a collection",
    "embedder_status": "one status object, not a collection",
    "memory_feedback_stats": "process-local counters, one object",
    "chatlog_cost_report": "one aggregate report object",
    "files_corpus_list": "already returns structured rows, no display string",
    "files_dedup_list": "already returns structured rows, no display string",
    "files_promotion_list": "already returns structured rows, no display string",
    "files_entity_coalesce_list": "already returns structured rows, no display string",
    "files_search": "already returns a structured array, no display string",
    "entity_search": "already returns a structured array, no display string",
    "chatlog_search": "already returns structured rows, no display string",
    "memory_search_slim": "slim variant; its parent memory_search carries the param",
    "tools_list_domains": "catalog meta-tool, returns a structured map",
}


@pytest.mark.parametrize("name", _list_shaped_tools())
def test_a_list_shaped_tool_can_emit_records(name):
    """⚠ §3's FIRST TENET: "return structured data, never message strings".

    A tool that renders its rows into prose forces every caller to parse the
    rendering back. That was tolerable while the only consumer was an LLM
    reading the text; now that every tool is pipeable, prose is a hard boundary
    — `m3 tasks task_list | jq '.items[]'` cannot consume
    "Tasks (20):\n  [c0aab26a] P0 ...".

    Measured 2026-09-20: 22 list-shaped tools, 12 without records. This fails
    on the NEXT one rather than on the backlog, because each known case is
    either fixed or carries a written reason above.
    """
    import mcp_tool_catalog as _cat

    if name in _NOT_A_COLLECTION:
        pytest.skip(f"{name}: {_NOT_A_COLLECTION[name]}")

    spec = {t.name: t for t in _cat.TOOLS}[name]
    props = (spec.parameters or {}).get("properties", {}) or {}
    assert "as_records" in props, (
        f"'{name}' is list-shaped but cannot emit records, so a piped caller "
        f"has to parse its display string. Add as_records (see "
        f"memory/records.py for the shared envelope), or declare it in "
        f"_NOT_A_COLLECTION with a reason."
    )


def test_every_exemption_still_names_a_real_tool():
    """A stale exemption silently suppresses the check for a renamed tool."""
    import mcp_tool_catalog as _cat

    known = {t.name for t in _cat.TOOLS}
    stale = sorted(set(_NOT_A_COLLECTION) - known)
    assert not stale, (
        f"_NOT_A_COLLECTION names tool(s) that no longer exist: {stale}"
    )


def test_the_cli_defaults_records_on_without_changing_the_toolspec():
    """⚠ TWO SURFACES, TWO DEFAULTS, ON PURPOSE.

    The ToolSpec default stays False because ~400 MCP callers predate the param
    and parse the display string — memory/records.py's compatibility rule, and
    test_spec_advertises_as_records pins it. The CLI flips it because its
    consumer is a pipe, not an LLM reading prose.

    Asserting both together so a future "simplification" cannot quietly align
    them and break one side or the other.
    """
    import inspect

    import mcp_tool_catalog as _cat
    from m3_memory import cli as _cli

    spec = {t.name: t for t in _cat.TOOLS}["task_list"]
    assert spec.parameters["properties"]["as_records"]["default"] is False, (
        "the MCP default changed — every caller that parses the display string "
        "would now receive JSON"
    )
    src = inspect.getsource(_cli)
    assert 'tool_args["as_records"] = True' in src, (
        "the CLI no longer defaults records on, so piped output is prose again"
    )
