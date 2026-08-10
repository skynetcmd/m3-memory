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
    """Refusing must report FAILURE, not success.

    Returning True here said "all writers handled" while the writer was still
    alive, so the caller re-waited the full quiesce timeout and re-entered the
    same branch forever — 244 iterations x 30s on windows-latest, which IS the
    E2E lane's "hang". An ancestor is unkillable by design: terminal, never
    retryable.
    """
    monkeypatch.setattr(sw, "_import_m3_halt", lambda: None)
    ok = sw._kill_stuck_writers([_Proc(os.getpid())])
    assert os.getpid() not in killed, "the installer killed its own process"
    assert ok is False, "an unkillable ancestor must not report success"


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


def test_ancestor_refusal_is_not_retryable(killed, monkeypatch):
    """The refusal must be TERMINAL, so the caller breaks instead of looping.

    `_kill_stuck_writers` returning False routes the caller to its abort path;
    returning True made it re-wait and re-enter forever. This pins the signal
    the loop depends on.
    """
    fake = types.ModuleType("m3_halt")
    fake._ancestor_pids = lambda pid, **kw: {424242}
    monkeypatch.setattr(sw, "_import_m3_halt", lambda: fake)
    assert sw._kill_stuck_writers([_Proc(424242, role="mcp")]) is False
    assert killed == []


def test_is_own_ancestor_agrees_with_the_guard(monkeypatch):
    """One implementation: the caller's test and the guard must never disagree."""
    fake = types.ModuleType("m3_halt")
    fake._ancestor_pids = lambda pid, **kw: {555555}
    monkeypatch.setattr(sw, "_import_m3_halt", lambda: fake)
    assert sw._is_own_ancestor(555555) is True
    assert sw._is_own_ancestor(os.getpid()) is True
    assert sw._is_own_ancestor(999999) is False


# ── the short-circuit: an ancestor must never reach a retry branch ───────────

def test_ancestor_aborts_the_quiesce_instead_of_looping(monkeypatch, capsys):
    """An unkillable ancestor must abort ONCE, not spin.

    Every branch below the short-circuit is a dead end for this case:
    --force-quiesce refuses it, the interactive "kill" refuses it, and
    "wait"/retry waits on a process that is waiting on us. On windows-latest the
    force path looped 244 times x 30s — the E2E lane's entire "hang".

    Drives the real loop (_quiesce_db_writers) and fails if any retry branch is
    reached at all.
    """
    import argparse

    waits = {"n": 0}

    class _Stuck:
        def __init__(self, pid, role="mcp"):
            self.pid, self.role = pid, role

    class _Result:
        ok = False
        stuck = [_Stuck(777777)]

    class _Halt:
        def raise_halt(self, *a, **k):
            return True

        def clear_halt(self, *a, **k):
            return None

        def list_all_db_writers(self, *a, **k):
            return [_Stuck(777777)]

        def list_blocking_db_writers(self, *a, **k):
            return [_Stuck(777777)]

        def set_halt(self, *a, **k):
            return True

        def elevated_kill_commands(self, *a, **k):
            return []

        def kill_stale_daemons(self, *a, **k):
            return []

        def wait_for_quiesce(self, *a, **k):
            waits["n"] += 1
            if waits["n"] > 4:
                raise AssertionError("looped: the short-circuit did not fire")
            return _Result()

    monkeypatch.setattr(sw, "_import_m3_halt", lambda: _Halt())
    monkeypatch.setattr(sw, "_is_own_ancestor", lambda pid: pid == 777777)
    monkeypatch.setattr(sw, "_quiesce_tick", lambda args: None)
    monkeypatch.setattr(sw, "_quiesce_tick_done", lambda args: None)
    monkeypatch.setattr(sw, "_kill_stuck_writers",
                        lambda *a, **k: pytest.fail("reached the kill branch"))
    monkeypatch.setattr(sw, "_surface_elevated_kill_help",
                        lambda *a, **k: pytest.fail("blamed privilege"))

    args = argparse.Namespace(non_interactive=True, force_quiesce=True,
                              force_kill_mcp=False, gui_child=False,
                              quiesce_timeout=1.0)
    assert sw._quiesce_db_writers(args) is False, (
        "an unkillable ancestor must abort the quiesce, not report success"
    )
    blob = "".join(capsys.readouterr())
    assert "own parent process" in blob
    assert "no privilege level or retry can stop it" in blob
    assert "m3 stop" in blob, "must name the actionable fix"
