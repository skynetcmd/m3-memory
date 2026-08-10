"""`_kill_stuck_writers` must never kill this install's own ancestry.

A DB-writer can legitimately register at a generation ABOVE the wizard. Launched
from the `m3` console script, setup sits several generations deep:

    m3.exe -> python (UTF-8 re-exec) -> setup -> ...

so a writer registered at the shim or re-exec generation is an ANCESTOR of the
running install. Killing it ends the run mid-install — the 2026-07-27 signature
was console output stopping after agent wiring, exit 1, no traceback, and ONLY
under the console-script launcher (`python -m m3_memory.cli setup`, which lacks
those two generations, completed fine).

m3_halt.kill_stale_daemons learned that and protects the full ancestor chain.
The wizard's own second kill path never got the same guard. This is
cross-platform: POSIX kills via os.kill, where killing an ancestor is equally
fatal — not a Windows-only hazard.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from m3_memory import setup_wizard as sw  # noqa: E402


class _Proc:
    def __init__(self, pid, role="mcp"):
        self.pid = pid
        self.role = role


@pytest.fixture
def killed(monkeypatch):
    """Record kill attempts instead of performing them."""
    seen = []
    monkeypatch.setattr(sw, "_kill_process_windows", lambda pid: (seen.append(pid), True)[1])
    monkeypatch.setattr(sw, "_kill_process_posix", lambda pid: (seen.append(pid), True)[1])
    return seen


def test_never_kills_self(killed, monkeypatch):
    monkeypatch.setattr(sw, "_import_m3_halt", lambda: None)
    ok = sw._kill_stuck_writers([_Proc(os.getpid())])
    assert os.getpid() not in killed, "the installer killed its own process"
    assert ok is True


def test_never_kills_the_parent(killed, monkeypatch):
    monkeypatch.setattr(sw, "_import_m3_halt", lambda: None)
    sw._kill_stuck_writers([_Proc(os.getppid())])
    assert os.getppid() not in killed, "the installer killed its own parent"


def test_protects_the_full_ancestor_chain(killed, monkeypatch):
    """Not just {self, parent} — the launcher can be several generations up."""
    fake = types.ModuleType("m3_halt")
    fake._ancestor_pids = lambda pid, **kw: {111111, 222222, 333333}
    monkeypatch.setattr(sw, "_import_m3_halt", lambda: fake)

    sw._kill_stuck_writers([_Proc(p) for p in (111111, 222222, 333333)])
    assert killed == [], f"killed an ancestor: {killed}"


def test_still_kills_a_genuine_stranger(killed, monkeypatch):
    """The guard must not neuter the kill path it protects."""
    fake = types.ModuleType("m3_halt")
    fake._ancestor_pids = lambda pid, **kw: {111111}
    monkeypatch.setattr(sw, "_import_m3_halt", lambda: fake)

    ok = sw._kill_stuck_writers([_Proc(999999, role="cognitive-loop")])
    assert killed == [999999], "a non-ancestor writer must still be stopped"
    assert ok is True


def test_ancestor_lookup_failure_still_protects_self(killed, monkeypatch):
    """A broken m3_halt must degrade to {self, parent}, never to 'kill anything'."""
    def boom():
        raise RuntimeError("payload unavailable")
    monkeypatch.setattr(sw, "_import_m3_halt", boom)

    sw._kill_stuck_writers([_Proc(os.getpid())])
    assert os.getpid() not in killed
