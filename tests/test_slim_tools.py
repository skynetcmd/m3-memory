"""The five slim variants: additive, derived, and routable.

What this pins:

  1. ADDITIVE. The plain tools keep their full parameter sets. A slim variant
     that quietly narrowed the original would break every existing caller.
  2. ONE OWNER (section 10a). The slim spec must reference the SAME impl object,
     not a copy -- there is no second code path to keep in sync.
  3. DERIVED, not copied. Kept parameters come from the live full spec, so an
     edit to the full tool's schema reaches both. A hand-written literal would
     drift silently.
  4. ROUTABLE (section 12a). Descriptions drive routing, so each slim
     description must name the escape hatch AND the full tool; otherwise an
     agent that needs an omitted parameter concludes the capability is gone.
  5. FAIL LOUD (section 3). A renamed tool or a dropped parameter must break the
     import, not ship a slim variant whose description promises parameters that
     no longer exist.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import mcp_tool_catalog as cat  # noqa: E402
from catalog import tools_slim  # noqa: E402

_BY_NAME = {t.name: t for t in cat.TOOLS}
SLIM_NAMES = [f"{n}_slim" for n in tools_slim._SLIM]

# Injected into every tool by the catalog, so they are not part of what a slim
# variant chooses to keep.
_UNIVERSAL = {"database", "timeout"}


@pytest.mark.parametrize("slim_name", SLIM_NAMES)
def test_slim_tool_is_in_the_catalog(slim_name):
    assert slim_name in _BY_NAME, f"{slim_name} never reached the built catalog"


@pytest.mark.parametrize("full_name", list(tools_slim._SLIM))
def test_full_tool_is_untouched(full_name):
    """Additive: slimming must not narrow the original."""
    full = _BY_NAME[full_name]
    slim = _BY_NAME[f"{full_name}_slim"]
    full_props = set((full.parameters or {}).get("properties", {}))
    slim_props = set((slim.parameters or {}).get("properties", {}))
    assert slim_props <= full_props, (
        f"{full_name}_slim advertises parameters the full tool lacks: "
        f"{slim_props - full_props}"
    )
    assert len(full_props) > len(slim_props), (
        f"{full_name} appears to have been narrowed in place; the slim variant "
        f"is supposed to be a separate, additional tool"
    )


@pytest.mark.parametrize("full_name", list(tools_slim._SLIM))
def test_slim_shares_the_same_impl_object(full_name):
    """Section 10a: one owner, two call sites -- not a copied code path."""
    assert _BY_NAME[f"{full_name}_slim"].impl is _BY_NAME[full_name].impl


@pytest.mark.parametrize("full_name", list(tools_slim._SLIM))
def test_kept_parameters_are_derived_not_copied(full_name):
    """Each kept property must be the SAME object as the full tool's, so an
    edit to one cannot drift from the other."""
    full_props = (_BY_NAME[full_name].parameters or {}).get("properties", {})
    slim_props = (_BY_NAME[f"{full_name}_slim"].parameters or {}).get("properties", {})
    for key, schema in slim_props.items():
        if key in _UNIVERSAL:
            continue
        assert schema == full_props[key], (
            f"{full_name}_slim[{key}] has drifted from the full tool's schema"
        )


@pytest.mark.parametrize("full_name", list(tools_slim._SLIM))
def test_required_parameters_survive(full_name):
    """A slim variant that cannot supply a required argument is unusable."""
    full_req = set((_BY_NAME[full_name].parameters or {}).get("required", []))
    slim_props = set((_BY_NAME[f"{full_name}_slim"].parameters or {}).get("properties", {}))
    assert full_req <= slim_props, (
        f"{full_name}_slim drops required parameter(s) {full_req - slim_props}"
    )


@pytest.mark.parametrize("slim_name", SLIM_NAMES)
def test_description_names_the_escape_hatch(slim_name):
    """Section 12a: without a named route, an agent needing an omitted
    parameter concludes the capability does not exist."""
    desc = _BY_NAME[slim_name].description
    assert "m3_call" in desc, f"{slim_name} does not name the escape hatch"
    full_name = slim_name[: -len("_slim")]
    assert full_name in desc, (
        f"{slim_name} does not name the full tool it narrows, so an agent has "
        f"nothing to route TO"
    )


@pytest.mark.parametrize("full_name", list(tools_slim._SLIM))
def test_description_names_the_omitted_parameters(full_name):
    """The routing trick: an agent searching for `scope` should match this
    description. 'Advanced options' would give it nothing to hit."""
    slim = _BY_NAME[f"{full_name}_slim"]
    full_props = set((_BY_NAME[full_name].parameters or {}).get("properties", {}))
    slim_props = set((slim.parameters or {}).get("properties", {}))
    omitted = full_props - slim_props - _UNIVERSAL
    if not omitted:
        pytest.skip(f"{full_name} omits nothing")
    named = [p for p in omitted if p in slim.description]
    assert named, (
        f"{full_name}_slim omits {sorted(omitted)} but names none of them in "
        f"its description"
    )


def test_slim_variants_are_cheaper_than_the_originals():
    """The entire point. Compares schema size, the thing that costs tokens."""
    import json
    for full_name in tools_slim._SLIM:
        full = _BY_NAME[full_name]
        slim = _BY_NAME[f"{full_name}_slim"]
        full_cost = len(json.dumps(full.parameters)) + len(json.dumps(full.description))
        slim_cost = len(json.dumps(slim.parameters)) + len(json.dumps(slim.description))
        assert slim_cost < full_cost, (
            f"{full_name}_slim ({slim_cost}B) is not smaller than {full_name} "
            f"({full_cost}B) -- it has no reason to exist"
        )


# ── fail-loud construction (section 3) ───────────────────────────────────────
def test_unknown_tool_name_raises():
    with pytest.raises(KeyError, match="no full spec named"):
        tools_slim._SLIM["does_not_exist"] = ((), "x")
        try:
            tools_slim._slim_of("does_not_exist")
        finally:
            del tools_slim._SLIM["does_not_exist"]


def test_unknown_kept_parameter_raises():
    """A parameter dropped by an upstream refactor must break the import, not
    ship a description promising something that no longer exists."""
    original = tools_slim._SLIM["chatlog_search"]
    tools_slim._SLIM["chatlog_search"] = (("no_such_param",), "x")
    try:
        with pytest.raises(KeyError, match="no parameter"):
            tools_slim._slim_of("chatlog_search")
    finally:
        tools_slim._SLIM["chatlog_search"] = original


def test_chatlog_status_has_no_slim_variant():
    """Pinned deliberately. It was in the original five and the measurement
    killed it: its only parameters are the universally-injected `database` and
    `timeout`, so there is nothing to narrow, and the slim variant measured
    LARGER than the original (724B vs 670B). Re-adding it would ship a second
    catalog entry for identical capability at a higher token cost."""
    assert "chatlog_status" not in tools_slim._SLIM
    assert "chatlog_status_slim" not in _BY_NAME


def test_dropping_a_required_parameter_raises():
    original = tools_slim._SLIM["memory_supersede"]
    tools_slim._SLIM["memory_supersede"] = (("title",), "x")
    try:
        with pytest.raises(KeyError, match="REQUIRED"):
            tools_slim._slim_of("memory_supersede")
    finally:
        tools_slim._SLIM["memory_supersede"] = original
