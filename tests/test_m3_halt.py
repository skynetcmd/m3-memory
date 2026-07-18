"""Tests for the cooperative quiesce protocol (bin/m3_halt.py).

Covers the PID registry (register / list / stale-reap), the HALT_m3 semaphore
(active-while-owner-alive, self-void on dead owner, malformed→inactive), and
wait_for_quiesce's empty-registry invariant. Filesystem-isolated via tmp_path;
never spawns real processes — a "dead" PID is a forged high number that isn't
running.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import m3_halt  # noqa: E402

# A PID that is (essentially certainly) not a live process, for stale tests.
_DEAD_PID = 2_000_000_000


@pytest.fixture()
def root(tmp_path):
    """An isolated engine root."""
    return str(tmp_path / "engine")


# ── PID registry ──────────────────────────────────────────────────────────
def test_register_then_list_returns_self(root):
    m3_halt.register_process("cognitive-loop", engine_root=root)
    live = m3_halt.list_live_processes(engine_root=root)
    assert len(live) == 1
    assert live[0].role == "cognitive-loop"
    assert live[0].pid == os.getpid()


def test_deregister_removes_entry(root):
    m3_halt.register_process("cognitive-loop", engine_root=root)
    m3_halt.deregister("cognitive-loop", engine_root=root)
    assert m3_halt.list_live_processes(engine_root=root) == []


def test_list_reaps_dead_pid_entry(root):
    # Live self + a forged dead-pid entry → only self survives, dead one reaped.
    m3_halt.register_process("cognitive-loop", engine_root=root)
    pdir = Path(root) / ".internal" / "PID"
    dead = pdir / f"embed-server.{_DEAD_PID}"
    dead.write_text(json.dumps({
        "pid": _DEAD_PID, "role": "embed-server", "started_at": "x",
        "engine_root": root, "protocol": 1}), encoding="utf-8")
    live = m3_halt.list_live_processes(engine_root=root)
    assert [p.role for p in live] == ["cognitive-loop"]
    assert not dead.exists()  # reaped


def test_list_reaps_malformed_entry(root):
    pdir = Path(root) / ".internal" / "PID"
    pdir.mkdir(parents=True)
    bad = pdir / "cognitive-loop.123"
    bad.write_text("{not json", encoding="utf-8")
    assert m3_halt.list_live_processes(engine_root=root) == []
    assert not bad.exists()


def test_list_empty_when_no_registry(root):
    assert m3_halt.list_live_processes(engine_root=root) == []


# ── HALT semaphore ─────────────────────────────────────────────────────────
def test_halt_inactive_when_absent(root):
    assert m3_halt.halt_is_active(engine_root=root) is False


def test_halt_active_while_owner_alive(root):
    # Owner = this test process (alive) → active.
    m3_halt.set_halt("installer", "migration", engine_root=root)
    assert m3_halt.halt_is_active(engine_root=root, role="cognitive-loop") is True


def test_halt_self_voids_on_dead_owner(root):
    halt = Path(root) / ".internal" / "HALT_m3"
    halt.parent.mkdir(parents=True)
    halt.write_text(json.dumps({
        "owner_pid": _DEAD_PID, "owner": "x", "reason": "y",
        "created_at": "z", "protocol": 1}), encoding="utf-8")
    assert m3_halt.halt_is_active(engine_root=root) is False
    assert not halt.exists()  # voided + reaped


def test_halt_malformed_is_inactive(root):
    halt = Path(root) / ".internal" / "HALT_m3"
    halt.parent.mkdir(parents=True)
    halt.write_text("{broken", encoding="utf-8")
    assert m3_halt.halt_is_active(engine_root=root) is False


def test_clear_halt_is_idempotent(root):
    m3_halt.set_halt("installer", "x", engine_root=root)
    m3_halt.clear_halt(engine_root=root)
    m3_halt.clear_halt(engine_root=root)  # second call must not raise
    assert m3_halt.halt_is_active(engine_root=root) is False


def test_set_halt_rejects_per_role_targets(root):
    with pytest.raises(ValueError):
        m3_halt.set_halt("installer", "x", engine_root=root, targets="cognitive-loop")


# ── wait_for_quiesce ───────────────────────────────────────────────────────
def test_wait_for_quiesce_ok_when_empty(root):
    r = m3_halt.wait_for_quiesce(engine_root=root, timeout=0.2, poll=0.05)
    assert r.ok is True
    assert r.stuck == []


def test_wait_for_quiesce_reports_stuck_holder(root):
    m3_halt.register_process("cognitive-loop", engine_root=root)
    r = m3_halt.wait_for_quiesce(engine_root=root, timeout=0.2, poll=0.05)
    assert r.ok is False
    assert len(r.stuck) == 1
    assert r.stuck[0].role == "cognitive-loop"


def test_wait_for_quiesce_ok_after_deregister(root):
    m3_halt.register_process("cognitive-loop", engine_root=root)
    m3_halt.deregister("cognitive-loop", engine_root=root)
    r = m3_halt.wait_for_quiesce(engine_root=root, timeout=0.2, poll=0.05)
    assert r.ok is True


def test_wait_for_quiesce_ignores_dead_holder(root):
    # A dead-pid registry entry must not count as a holder → quiesce succeeds.
    pdir = Path(root) / ".internal" / "PID"
    pdir.mkdir(parents=True)
    (pdir / f"mcp.{_DEAD_PID}").write_text(json.dumps({
        "pid": _DEAD_PID, "role": "mcp", "started_at": "x",
        "engine_root": root, "protocol": 1}), encoding="utf-8")
    r = m3_halt.wait_for_quiesce(engine_root=root, timeout=0.2, poll=0.05)
    assert r.ok is True


# ── security: role sanitization + pid validation ───────────────────────────
@pytest.mark.parametrize("bad_role", ["../evil", "a/b", "", "x.y", "rolewith space"])
def test_register_rejects_unsafe_role(root, bad_role):
    with pytest.raises(ValueError):
        m3_halt.register_process(bad_role, engine_root=root)


def test_deregister_rejects_unsafe_role(root):
    with pytest.raises(ValueError):
        m3_halt.deregister("../evil", engine_root=root)


def test_list_reaps_nonpositive_pid(root):
    pdir = Path(root) / ".internal" / "PID"
    pdir.mkdir(parents=True)
    bad = pdir / "cognitive-loop.0"
    bad.write_text(json.dumps({
        "pid": 0, "role": "cognitive-loop", "started_at": "x",
        "engine_root": root, "protocol": 1}), encoding="utf-8")
    assert m3_halt.list_live_processes(engine_root=root) == []
    assert not bad.exists()
