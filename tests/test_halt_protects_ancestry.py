"""`kill_stale_daemons` must never kill the branch it is running on.

On Windows the kill is `taskkill /F /T`, and /T takes the target's whole process
TREE — so killing ANY ancestor kills this process too, however many generations
up it sits. Protecting only {self, parent} was not enough.

Launched from the `m3` console script, `m3 setup` is five generations deep:
    m3.exe -> python (UTF-8 re-exec) -> setup -> install_schedules.py -> child
so a DB-writer registered at the shim or re-exec generation fell outside the
protected set. install_schedules.py killed it, the tree-kill took `m3 setup`
with it, and the command exited 1 with no traceback — while
`python -m m3_memory.cli setup`, which has neither extra generation, exited 0.
Same code, different launcher (2026-07-27).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import m3_halt  # noqa: E402


def _chain(monkeypatch, links: dict):
    """Fake a psutil whose Process(pid).ppid() follows `links`."""
    class _P:
        def __init__(self, pid):
            if pid not in links:
                raise RuntimeError("no such process")
            self._pid = pid

        def ppid(self):
            return links[self._pid]

    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(Process=_P, Error=Exception))


def test_walks_the_full_ancestor_chain(monkeypatch):
    """The real shape: setup is 5 deep. Every generation must be protected."""
    # child <- install_schedules <- setup <- re-exec <- m3.exe <- shell
    _chain(monkeypatch, {114436: 82560, 82560: 117444, 117444: 49524,
                         49524: 108448, 108448: 4, 4: 0})
    got = m3_halt._ancestor_pids(114436)
    assert {82560, 117444, 49524, 108448}.issubset(got), got
    assert 114436 not in got, "self must not appear in the ancestor set"


def test_stops_at_the_top_of_the_chain(monkeypatch):
    _chain(monkeypatch, {10: 5, 5: 0})
    assert m3_halt._ancestor_pids(10) == {5}


def test_cycle_cannot_spin(monkeypatch):
    """A recycled pid that appears to parent itself must terminate the walk."""
    _chain(monkeypatch, {1: 2, 2: 1})
    assert m3_halt._ancestor_pids(1) == {2}


def test_depth_is_bounded(monkeypatch):
    """A pathological chain is truncated, not followed forever."""
    _chain(monkeypatch, {i: i + 1 for i in range(1, 200)})
    assert len(m3_halt._ancestor_pids(1, max_depth=5)) == 5


def test_missing_psutil_degrades_to_the_parent(monkeypatch):
    """No psutil -> still protect the direct parent (best-effort, never worse
    than the old behaviour)."""
    import builtins
    real = builtins.__import__

    def _no_psutil(name, *a, **k):
        if name == "psutil":
            raise ImportError("no psutil")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_psutil)
    monkeypatch.delitem(sys.modules, "psutil", raising=False)
    assert m3_halt._ancestor_pids(os.getpid()) == {os.getppid()}


def test_lookup_failure_returns_partial_not_raises(monkeypatch):
    """A denied/vanished ancestor stops the climb; it must not propagate."""
    _chain(monkeypatch, {7: 8})  # 8 has no entry -> lookup raises
    assert m3_halt._ancestor_pids(7) == {8}


def test_an_ancestor_is_never_targeted(monkeypatch):
    """End-to-end guard: a writer that is an ANCESTOR must be skipped, since a
    /T tree-kill on it would take this process down with it."""
    _chain(monkeypatch, {1000: 900, 900: 800, 800: 0})
    monkeypatch.setattr(os, "getpid", lambda: 1000)
    monkeypatch.setattr(
        m3_halt, "list_all_db_writers",
        lambda root=None: [SimpleNamespace(pid=900, role="setup"),
                           SimpleNamespace(pid=800, role="m3.exe"),
                           SimpleNamespace(pid=555, role="dashboard")],
    )
    killed: list[int] = []
    monkeypatch.setattr(m3_halt, "_pid_is_alive", lambda pid: False)
    results = m3_halt.kill_stale_daemons()
    targeted = {r["pid"] for r in results}
    assert 900 not in targeted, "killed its own parent"
    assert 800 not in targeted, "killed its own grandparent (tree-kill = suicide)"
    assert 555 in targeted, "unrelated writer should still be targeted"
