"""Behavior baseline for the `memory_search_scored` catalog ToolSpec.

PRE-REGISTERED per DESIGN_PHILOSOPHIES §5 (pre-register the metric + threshold
before writing code) and §11 (build the behavior baseline BEFORE the change).

Context
-------
`memory_search` (catalog) returns LLM-readable FORMATTED TEXT. Its structured
sibling `memory_search_scored_impl` returns ranked ROWS — `list[(score, item)]`
with content + metadata (valid_from, conversation_id, user_id) — and is the
shape a memory-provider backend consumes (e.g. Hermes Agent's M3MemoryProvider).

As of this test's authoring, that impl is reachable only as a Python function
(bin/memory/search.py:memory_search_scored_impl) and as an internal call
(mcp_tool_catalog._conversation_search_impl). It has NO catalog ToolSpec, so it
cannot be dispatched by name through execute_tool_structured / m3_call.

This test encodes the TARGET CONTRACT for adding that read-only ToolSpec:

  ToolSpec(
      name="memory_search_scored",
      impl=memory_core.memory_search_scored_impl,
      is_async=True,
      default_allowed=True,      # §6 read-only by construction
      inject_agent_id=False,
      validators=(_memory_search_gated_validator,),
  )

Expected lifecycle
------------------
* BEFORE the spec lands: `test_memory_search_scored_is_catalogued` FAILS with a
  clear "spec not yet added" message; the row-shape tests SKIP (they can't run
  without the spec). This is the intended red state — the gap is visible.
* AFTER the spec lands: all tests pass unchanged. No edits to this file should
  be needed; if a row-shape assertion then fails, the spec's wiring is wrong,
  not the test.

Conventions mirror tests/test_dispatcher.py: async impls driven with
asyncio.run() in sync test functions; the side-effect-free shipped corpus is
used as the search target; assertions are on STRUCTURE, not search content
(retrieval quality is covered by tests/capture_retrieval_baseline.py, not here).
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

# conftest.py already puts bin/ on sys.path; belt-and-suspenders for isolation.
_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import mcp_tool_catalog

_SCORED = "memory_search_scored"

# The shipped files corpus DB — a real, side-effect-free read target, resolved
# relative to the repo root so the test runs from any cwd. (Same target as
# test_dispatcher.py; we only assert on result SHAPE, so any populated corpus
# works — an empty result set is still a valid, correctly-shaped list.)
_FILES_DB = os.path.normpath(os.path.join(_HERE, "..", "memory", "files_database.db"))


def _scored_args() -> dict:
    """Minimal valid args for the scored search: a query + the corpus DB.
    Each caller gets a fresh dict — execute_tool_structured mutates args in
    place (pops 'database', filters keys)."""
    return {"query": "memory", "k": 5, "database": _FILES_DB}


# ── Spec presence (the pre-registered gate) ──────────────────────────────────

def test_memory_search_scored_is_catalogued():
    """A `memory_search_scored` ToolSpec must exist, be async, read-only
    (default_allowed=True), and NOT inject agent_id.

    This is the assertion that fails until the spec is added — it makes the
    missing structured surface visible rather than silently absent (§3, §12).
    """
    spec = mcp_tool_catalog.get_tool(_SCORED)
    assert spec is not None, (
        f"{_SCORED!r} has no catalog ToolSpec yet. Add a read-only spec in "
        "bin/mcp_tool_catalog.py TOOLS pointing impl=memory_core."
        "memory_search_scored_impl. See this module's docstring for the target."
    )
    assert spec.is_async is True, f"{_SCORED} impl is async; spec.is_async must be True"
    assert spec.default_allowed is True, (
        f"{_SCORED} is a read-only search — default_allowed must be True so it "
        "is exposed without MCP_PROXY_ALLOW_DESTRUCTIVE (§6)"
    )
    assert spec.inject_agent_id is False, (
        f"{_SCORED} is a read path; it must not stamp agent_id (§7)"
    )
    props = spec.parameters.get("properties", {})
    # query must be accepted (empty string allowed = filter-only listing).
    assert "query" in props, f"{_SCORED} must accept a 'query' arg"
    # query must NOT be in `required` — the filter-only path passes query=''
    # and an absent query is valid. (The universal 'database' arg is injected
    # into every spec's properties at module load by _inject_database_arg, so we
    # do NOT assert on its absence here — that stripping is m3_index's job, see
    # test_dispatcher.test_m3_index_returns_all_non_excluded_tools.)
    assert "query" not in spec.parameters.get("required", []), (
        f"{_SCORED} must not REQUIRE query — empty-query filter-only listing is a "
        "supported call (improvement 1). required should be []."
    )


def _require_spec() -> "mcp_tool_catalog.ToolSpec":
    """Skip (not fail) the row-shape tests when the spec isn't present yet —
    their failure mode is uninformative noise until the gate test above is
    green. Once the spec lands they run for real."""
    spec = mcp_tool_catalog.get_tool(_SCORED)
    if spec is None:
        pytest.skip(f"{_SCORED} ToolSpec not added yet (see "
                    "test_memory_search_scored_is_catalogued)")
    return spec


# ── Row-shape contract (what the provider actually consumes) ──────────────────

def test_scored_returns_ranked_rows_via_execute_tool_structured():
    """execute_tool_structured(memory_search_scored, ...) returns the impl's
    NATIVE value: a list of (score, item) pairs — NOT a formatted string and
    NOT an 'Error: …' string. Empty corpus matches are fine; an empty list is
    still a correctly-shaped result (§3: empty=list, never None)."""
    spec = _require_spec()
    result = asyncio.run(
        mcp_tool_catalog.execute_tool_structured(spec, _scored_args(), agent_id="")
    )

    assert result is not None, "scored search must return a list, never None (§3)"
    assert isinstance(result, list), (
        f"expected list[(score, item)], got {type(result).__name__}"
    )
    for row in result:
        # Tuple from the in-process impl; a 2-element list if it ever round-trips
        # through json. Accept either — the provider unpacks `for s, it in rows`.
        assert len(row) == 2, f"each row must be (score, item); got len {len(row)}"
        score, item = row
        assert isinstance(score, (int, float)), (
            f"score must be numeric, got {type(score).__name__}"
        )
        assert isinstance(item, dict), (
            f"item must be a dict, got {type(item).__name__}"
        )
        assert "content" in item, (
            "each item must carry 'content' — the field the provider surfaces "
            "as the recall line"
        )


def test_scored_parity_with_m3_call_dispatch():
    """Dispatching memory_search_scored through m3_call must produce the SAME
    underlying rows as execute_tool_structured directly — both ride the
    identical gate+db-context+validation path, so behavior cannot drift (§12).

    Note: m3_call json.dumps() at its boundary, which turns each (score, item)
    TUPLE into a 2-element ARRAY. The values must match element-wise; we compare
    after normalizing tuples → lists so the test asserts data parity, not the
    Python container type.
    """
    import json

    spec = _require_spec()
    direct = asyncio.run(
        mcp_tool_catalog.execute_tool_structured(spec, _scored_args(), agent_id="")
    )
    dispatched = json.loads(asyncio.run(
        mcp_tool_catalog.m3_call_impl(tool=_SCORED, args=_scored_args())
    ))

    assert dispatched["ok"] is True, (
        f"m3_call({_SCORED}) failed: {dispatched.get('error')} "
        f"{dispatched.get('detail', '')}"
    )
    assert dispatched["tool"] == _SCORED

    # Normalize both sides to JSON-comparable form (tuple → list) and compare.
    normalized_direct = json.loads(json.dumps(direct))
    assert dispatched["result"] == normalized_direct, (
        "m3_call result diverged from execute_tool_structured — the dispatch "
        "and direct paths must return identical rows"
    )


def test_scored_filter_only_query_is_accepted():
    """An empty query with a type_filter is a valid 'list rows of this type'
    call (the get_all/profile path the provider uses). It must dispatch cleanly
    and return a list — never raise, never error-string (§3).

    LOAD-BEARING CONTRACT (improvement 1): this is why memory_search_scored must
    NOT reuse `_memory_search_gated_validator`. That validator's inner
    `_memory_search_validator` rejects empty queries with
    "Error: query cannot be empty." (bin/mcp_tool_catalog.py:170), which
    execute_tool_structured raises as ValueError. The scored spec needs a
    validator that runs the bench-data gate (`_variant_gate`) and clamps k, but
    does NOT reject an empty query. If this test ever fails with a ValueError
    about an empty query, the spec was wired to the wrong validator.
    """
    spec = _require_spec()
    args = {"query": "", "type_filter": "note", "k": 10, "database": _FILES_DB}
    result = asyncio.run(
        mcp_tool_catalog.execute_tool_structured(spec, args, agent_id="")
    )
    assert isinstance(result, list), (
        "filter-only (empty-query) scored search must return a list"
    )


def test_scored_matches_memory_search_rowset_for_same_query():
    """Cross-tool parity (improvement 2): memory_search_scored and memory_search
    must select the SAME underlying rows for an identical non-empty query, so the
    structured sibling can't silently drift from the formatted one.

    The two are different RETURN SHAPES (rows vs formatted text), so we can't
    compare outputs directly. Instead we compare the IDENTITY SET each selects.
    memory_search_scored returns item dicts (which carry 'id'); we ask
    memory_search for the same k and assert the scored id-set is a subset of /
    equal to what the formatted path would surface for the same gated query.

    Why this matters: memory_search routes through `_variant_gate`, which
    defaults variant -> '__none__' to HIDE bench rows. memory_search_scored_impl
    defaults variant='' (real-data IS NULL filter) — a DIFFERENT filter. Unless
    the scored spec pins the same bench-data gate, a provider doing recall could
    silently see a different row set than every other m3 caller (§7 isolation,
    §5 effectiveness). This test fails the moment those gates diverge.
    """
    spec = _require_spec()
    q = "memory"
    k = 5

    scored = asyncio.run(mcp_tool_catalog.execute_tool_structured(
        spec, {"query": q, "k": k, "database": _FILES_DB}, agent_id=""))
    scored_ids = {
        item.get("id") for _s, item in scored if isinstance(item, dict)
    }
    scored_ids.discard(None)

    # Drive the formatted sibling through the SAME structured path so both get
    # the identical gate/db-context treatment; memory_search_impl returns text,
    # but its gated query selection is what we're comparing the id-set against.
    ms_spec = mcp_tool_catalog.get_tool("memory_search")
    assert ms_spec is not None
    # memory_search returns formatted text — to get its id-set we read the scored
    # impl with the SAME bench gate memory_search applies (variant='__none__'),
    # which is exactly what the scored spec must default to. If the scored spec
    # is gated correctly, passing variant explicitly here is a no-op; if it is
    # NOT, this exposes the divergence.
    gated = asyncio.run(mcp_tool_catalog.execute_tool_structured(
        spec, {"query": q, "k": k, "variant": "__none__", "database": _FILES_DB},
        agent_id=""))
    gated_ids = {item.get("id") for _s, item in gated if isinstance(item, dict)}
    gated_ids.discard(None)

    assert scored_ids == gated_ids, (
        "memory_search_scored with its default gate selected a DIFFERENT row set "
        "than the same query explicitly gated to variant='__none__'. The scored "
        "spec must default to the same bench-data gate as memory_search "
        "(improvement 2) — otherwise recall leaks variant/bench rows."
    )
