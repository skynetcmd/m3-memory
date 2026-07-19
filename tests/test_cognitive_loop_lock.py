"""Tests for the single-instance lock (acquire_lock) in the cognitive loop.

Two live loops double-dispatch to the local LLM (observed 2026-07-19: PIDs
6460 + 37096 both running). The lock must:
  - use an ATOMIC exclusive-create so a check-then-write race can't let two
    launches both proceed;
  - reclaim a genuinely STALE lock (dead PID, or a reused PID now owned by a
    non-loop process);
  - REFUSE to start when a live loop already holds the lock.

_pid_is_live_loop is monkeypatched so these are hermetic (no real processes).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import m3_cognitive_loop as cl  # noqa: E402


@pytest.fixture
def lockfile(tmp_path, monkeypatch):
    """Point the module's PID_FILE at a tmp path and reset atexit noise."""
    p = tmp_path / "cognitive_loop.pid"
    monkeypatch.setattr(cl, "PID_FILE", p)
    # release_lock reads PID_FILE; keep atexit registration harmless in tests.
    monkeypatch.setattr(cl, "atexit", type("A", (), {"register": staticmethod(lambda *a, **k: None)})())
    return p


def test_acquire_on_clean_slate_writes_our_pid(lockfile):
    cl.acquire_lock()
    assert lockfile.read_text().strip() == str(cl.os.getpid())


def test_acquire_refuses_when_live_loop_holds_lock(lockfile, monkeypatch):
    lockfile.write_text("99999")  # some other PID holds it
    monkeypatch.setattr(cl, "_pid_is_live_loop", lambda pid: True)
    with pytest.raises(SystemExit) as exc:
        cl.acquire_lock()
    assert exc.value.code == 0
    # Lock file is left untouched (still the live holder's PID).
    assert lockfile.read_text().strip() == "99999"


def test_acquire_reclaims_dead_pid(lockfile, monkeypatch):
    lockfile.write_text("99999")  # stale: PID is dead / not a loop
    monkeypatch.setattr(cl, "_pid_is_live_loop", lambda pid: False)
    cl.acquire_lock()
    assert lockfile.read_text().strip() == str(cl.os.getpid())


def test_acquire_reclaims_garbage_lockfile(lockfile, monkeypatch):
    lockfile.write_text("not-a-pid")  # corrupt content -> treat as stale
    # _pid_is_live_loop shouldn't even be consulted for garbage, but stub it safe.
    monkeypatch.setattr(cl, "_pid_is_live_loop", lambda pid: False)
    cl.acquire_lock()
    assert lockfile.read_text().strip() == str(cl.os.getpid())


def test_acquire_is_idempotent_for_our_own_pid(lockfile, monkeypatch):
    # If the lock already records OUR pid (e.g. a re-entrant call), don't refuse.
    lockfile.write_text(str(cl.os.getpid()))
    # Even if the liveness probe would say "live", old_pid == my_pid short-circuits.
    monkeypatch.setattr(cl, "_pid_is_live_loop", lambda pid: True)
    cl.acquire_lock()  # must not SystemExit
    assert lockfile.read_text().strip() == str(cl.os.getpid())
