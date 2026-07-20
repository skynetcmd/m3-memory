"""Tests for the cognitive loop's single-instance locking (integration).

Two live loops double-dispatch to the local LLM (observed 2026-07-19). The loop
delegates to the shared system-wide OS-advisory lock via m3_halt.acquire_or_exit.
These pin the INTEGRATION: cl.acquire_lock()
  - acquires and holds the engine-root lock on a clean slate;
  - runs (degraded) rather than crashing if the lock subsystem can't function.
The lock's own guarantees (cross-process mutual exclusion, exit codes, event log)
are pinned in test_m3_halt.py.

Cross-process HELD_BY_PEER can't be exercised in-process (a re-acquire by the
same process is REENTRANT), so that path is covered by the primitive's suite.

Filesystem-isolated via M3_ENGINE_ROOT → tmp.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import m3_cognitive_loop as cl  # noqa: E402
import m3_halt  # noqa: E402

_ROLE = "cognitive-loop"


@pytest.fixture
def engine_root(tmp_path, monkeypatch):
    """Redirect the shared lock to an isolated tmp engine root."""
    root = tmp_path / "engine"
    monkeypatch.setenv("M3_ENGINE_ROOT", str(root))
    monkeypatch.setattr(cl, "_INSTANCE_LOCK", None, raising=False)
    yield root
    lk = getattr(cl, "_INSTANCE_LOCK", None)
    if lk is not None:
        lk.release()


def _owner_file(root):
    return Path(root) / ".internal" / f"{_ROLE}.lock.owner"


def test_acquire_on_clean_slate_holds_lock(engine_root):
    cl.acquire_lock()
    assert cl._INSTANCE_LOCK is not None and cl._INSTANCE_LOCK.acquired
    op = _owner_file(engine_root)
    assert op.exists()
    assert json.loads(op.read_text())["pid"] == os.getpid()


def test_acquire_runs_degraded_on_lock_subsystem_failure(engine_root, monkeypatch):
    # Fail-safe: if the lock file can't be created, the loop must still RUN
    # (degraded), not crash or refuse — a coordination glitch must not take the
    # loop down.
    def _boom(*a, **k):
        raise OSError("no dir")
    monkeypatch.setattr(m3_halt.Path, "mkdir", _boom)
    cl.acquire_lock()  # must not raise SystemExit
    assert cl._INSTANCE_LOCK is not None and not cl._INSTANCE_LOCK.acquired
    assert cl._INSTANCE_LOCK.status is m3_halt.LockStatus.CONFIG_ERROR
