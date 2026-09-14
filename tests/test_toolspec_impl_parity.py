"""ToolSpec / impl parity — the contract an agent reads must be the one it gets.

Defect C, generalized. `agent_register`'s ToolSpec listed only `agent_id` as
required while the impl demanded `role`, `capabilities` and `metadata`
positionally, so an agent that read the contract and called with just an id got:

    TypeError: agent_register_impl() missing 3 required positional arguments

Sweeping the catalog found it was a CLASS, not a one-off -- three tools drifted
the same way (`agent_register`, `memory_handoff`, `memory_search_scored`). That
is why this is a guard rather than three patches: a fourth would otherwise land
the same way and be found the same way, by an agent hitting it at runtime.

Section 12a: the ToolSpec IS the agent's environment. When the two disagree the
SPEC is authoritative and the impl moves to meet it -- widening a signature
breaks no existing caller, while narrowing the spec breaks every agent already
relying on the documented shape.

The sweep must resolve past `catalog.lazy.LazyImpl`, whose signature is
`(*args, **kwargs)`. Taking that at face value is what lets drift hide: it makes
every tool look compatible with everything.
"""
from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import mcp_tool_catalog as cat  # noqa: E402

# Injected into every spec after aggregation; the impls never declare them.
_INJECTED = {"database", "timeout"}

# Parameters a VALIDATOR consumes before the impl is called, so the impl
# legitimately has no slot for them. `_variant_gate` pops `include_bench_data`
# and translates it into `variant`; the impl never sees the original name.
#
# This set is deliberately narrow and must stay that way: every entry is a hole
# in the guard, so adding one to silence a failure would hide the very drift
# this file exists to catch. Verify the validator really does pop the parameter
# before listing it here.
_VALIDATOR_CONSUMED = {"include_bench_data"}


def _resolve(impl):
    """Get the real function behind a LazyImpl, or None if not resolvable."""
    module_name = getattr(impl, "module_name", None)
    attr_name = getattr(impl, "attr_name", None)
    if module_name and attr_name:
        try:
            return getattr(importlib.import_module(module_name), attr_name)
        except Exception:  # noqa: BLE001
            return None
    return impl


def _impl_required(fn) -> set[str] | None:
    """Parameters the impl demands. None when it cannot be determined."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return None  # **kwargs accepts anything
    return {
        name
        for name, p in sig.parameters.items()
        if p.default is inspect.Parameter.empty
        and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        and not name.startswith("_")
    }


_CASES = [(s.name, s) for s in cat.TOOLS]


@pytest.mark.parametrize("name,spec", _CASES, ids=[n for n, _ in _CASES])
def test_impl_requires_nothing_the_spec_calls_optional(name, spec):
    """The defect exactly: an impl demanding an argument the contract says is
    optional. An agent that honors the contract gets a TypeError."""
    fn = _resolve(spec.impl)
    if fn is None:
        pytest.skip(f"{name}: impl not resolvable")
    impl_req = _impl_required(fn)
    if impl_req is None:
        pytest.skip(f"{name}: impl accepts **kwargs")
    spec_req = set((spec.parameters or {}).get("required", []))
    extra = impl_req - spec_req - _INJECTED
    assert not extra, (
        f"{name}: impl requires {sorted(extra)} but the ToolSpec marks them "
        f"optional (required={sorted(spec_req)}). An agent that reads the "
        f"contract and omits them gets a TypeError. Give them defaults -- the "
        f"spec is the contract, so the impl moves to meet it."
    )


@pytest.mark.parametrize("name,spec", _CASES, ids=[n for n, _ in _CASES])
def test_spec_advertises_nothing_the_impl_cannot_accept(name, spec):
    """The mirror image: a spec offering a parameter the impl has no slot for.
    An agent that passes it gets a TypeError from a tool that advertised it."""
    fn = _resolve(spec.impl)
    if fn is None:
        pytest.skip(f"{name}: impl not resolvable")
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        pytest.skip(f"{name}: signature not introspectable")
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        pytest.skip(f"{name}: impl accepts **kwargs")
    declared = (set((spec.parameters or {}).get("properties", {}))
                - _INJECTED - _VALIDATOR_CONSUMED)
    accepted = set(sig.parameters)
    unaccepted = declared - accepted
    assert not unaccepted, (
        f"{name}: ToolSpec advertises {sorted(unaccepted)} but the impl has no "
        f"such parameter(s). An agent that passes one gets a TypeError from a "
        f"tool that said it was supported."
    )


@pytest.mark.parametrize("name,spec", _CASES, ids=[n for n, _ in _CASES])
def test_required_parameters_exist_in_properties(name, spec):
    """A `required` entry with no matching property is unsatisfiable: the agent
    is told to pass something the schema never describes."""
    params = spec.parameters or {}
    props = set(params.get("properties", {}))
    missing = [r for r in params.get("required", []) if r not in props]
    assert not missing, (
        f"{name}: required={missing} but those are not in properties"
    )


def test_the_three_known_drifts_stay_fixed():
    """Pins defect C's specific cases so a refactor cannot quietly reintroduce
    them. Each was a real TypeError reachable from a contract-honoring call."""
    import memory_core as mc
    from memory.orchestration import agent_register_impl
    from memory.search import memory_search_scored_impl

    for fn, kwargs in (
        (agent_register_impl, {"agent_id": "x"}),
        (mc.memory_handoff_impl,
         {"from_agent": "a", "to_agent": "b", "task": "t"}),
        (memory_search_scored_impl, {}),
    ):
        sig = inspect.signature(fn)
        try:
            sig.bind(**kwargs)  # must not raise
        except TypeError as e:  # pragma: no cover - the assertion is the message
            pytest.fail(f"{fn.__name__} rejects its documented minimal call: {e}")


def test_validator_consumed_params_really_are_consumed():
    """Guards the exemption above. Every name in _VALIDATOR_CONSUMED must
    actually be popped by a validator -- otherwise the entry is not an
    exemption, it is a hole that hides real drift."""
    from catalog import validators
    for name in _VALIDATOR_CONSUMED:
        args = {name: True, "query": "x", "k": 5}
        out = validators._variant_gate(dict(args))
        assert name not in out, (
            f"_VALIDATOR_CONSUMED lists {name!r} but no validator pops it. "
            f"Remove the exemption or fix the validator."
        )


def test_bench_gate_translates_rather_than_drops():
    """The exemption is only safe because the parameter is TRANSLATED, not
    discarded: include_bench_data=True must actually widen the variant filter."""
    from catalog import validators
    on = validators._variant_gate({"include_bench_data": True})
    off = validators._variant_gate({"include_bench_data": False})
    assert on["variant"] == "", "include_bench_data=True must drop the variant filter"
    assert off["variant"] == "__none__", "default must hide bench rows"
