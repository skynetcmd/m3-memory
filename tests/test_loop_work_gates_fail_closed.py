"""Every cognitive-loop work gate must fail CLOSED when its probe errors.

WHY THIS EXISTS
---------------
`has_entity_work` and `has_enrich_work` returned **True** on any probe
exception, commented "Default to True to be safe". The other eight gates
return False and say "conservative". That divergence is the bug: the SAME
outage (store unreachable, migration half-applied, a bad DSN) produced
opposite behaviour depending on which gate happened to hit it.

Returning True is not the safe direction here. The gates feed `more_work` in
the idle-backlog drain: while it is true the loop re-ticks at a ~1s floor
instead of sleeping `--interval`. A probe that keeps failing therefore pins
the loop to a hot spin — burst spent, full interval, re-arm, burst again —
forever, on work it can never confirm exists. And both sites logged at
DEBUG, so the cause was invisible while it happened (§3: fail loud, never
silent).

A gate that cannot read its store does not know that there is work. It must
say "no work this cycle" and let the next cycle find the backlog once the
store answers.
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BIN = os.path.join(_ROOT, "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)


# (gate name, kwargs) for every work gate whose probe can raise. Each is called
# with arguments that reach the probe, which we then force to blow up.
_GATES = [
    ("has_entity_work", (None, None)),
    ("has_enrich_work", (None,)),
    ("has_classify_work", (None,)),
]


def _boom(*_a, **_kw):
    raise RuntimeError("probe exploded (simulated store outage)")


@pytest.mark.parametrize("gate_name,args", _GATES)
def test_work_gate_fails_closed(gate_name, args, monkeypatch):
    """A gate whose probe raises must return False, never True."""
    import m3_cognitive_loop as L

    # Break the shared probe every gate funnels through.
    monkeypatch.setattr(L, "_probe_core", _boom, raising=False)
    # has_entity_work reaches a second store through the seam; break that too so
    # the exception is guaranteed regardless of which leg runs first.
    monkeypatch.setattr(L, "_probe_entity_work", _boom, raising=False)

    gate = getattr(L, gate_name)
    assert gate(*args) is False, (
        f"{gate_name} returned True when its probe raised. A gate that cannot "
        f"read its store does not know there is work -- returning True pins the "
        f"idle-backlog drain to its ~1s floor on unconfirmed work."
    )


@pytest.mark.parametrize("gate_name,args", _GATES)
def test_work_gate_says_why_at_warning(gate_name, args, monkeypatch, caplog):
    """The failure must be visible, not swallowed at DEBUG.

    Both offenders logged at DEBUG, so a store that would not answer looked
    exactly like a healthy idle loop. §3: state what you OBSERVED.
    """
    import m3_cognitive_loop as L

    monkeypatch.setattr(L, "_probe_core", _boom, raising=False)
    monkeypatch.setattr(L, "_probe_entity_work", _boom, raising=False)

    with caplog.at_level("WARNING", logger=L.logger.name):
        getattr(L, gate_name)(*args)

    assert caplog.records, (
        f"{gate_name} swallowed a probe failure with no WARNING. A silent "
        f"fail-closed is a gate that never reports why it stopped working."
    )
    assert any("observed:" in r.getMessage() for r in caplog.records), (
        "the warning must mark measured fact with 'observed:' (§3: state what "
        "you OBSERVED, mark what you INFERRED)"
    )
