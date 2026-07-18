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


# The real scan function, captured before the autouse stub replaces the module
# attribute — scan-focused tests call this to exercise the genuine implementation.
_REAL_SCAN = m3_halt.scan_db_writer_processes


@pytest.fixture(autouse=True)
def _no_real_cmdline_scan(monkeypatch):
    """Neutralize the psutil cmdline scan by default so registry-focused tests
    are hermetic (the real host may be running actual m3 cognitive-loop / embed
    processes, which the scan would legitimately find). Tests that exercise the
    scan itself call _REAL_SCAN with psutil mocked."""
    monkeypatch.setattr(m3_halt, "scan_db_writer_processes", lambda engine_root=None: [])


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


# ── upgrade-safety: registry-independent cmdline scan (old-version writers) ────
def test_scan_finds_writer_by_cmdline_signature(root, monkeypatch):
    """scan_db_writer_processes finds a process whose cmdline carries a writer
    signature even though it never registered (simulates a pre-HALT old version).
    psutil is mocked so the test is hermetic — no real process spawned."""
    class _FakeProc:
        def __init__(self, pid, cmdline):
            self.info = {"pid": pid, "cmdline": cmdline, "create_time": 0}

    fake_procs = [
        _FakeProc(4242, ["python", "bin/m3_cognitive_loop.py", "--interval", "60"]),
        _FakeProc(4243, ["python", "bin/embed_server_inproc.py", "--port", "8082"]),
        _FakeProc(9999, ["python", "unrelated_thing.py"]),  # must NOT match
    ]

    class _FakePsutil:
        NoSuchProcess = AccessDenied = ZombieProcess = type("E", (Exception,), {})
        @staticmethod
        def process_iter(_attrs):
            return iter(fake_procs)

    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil)
    found = _REAL_SCAN(engine_root=root)
    roles = sorted((p.role, p.pid) for p in found)
    assert roles == [("cognitive-loop", 4242), ("embed-server", 4243)]


def test_scan_no_psutil_returns_empty(root, monkeypatch):
    """No psutil -> empty (fail-open; registry + file-lock probe remain)."""
    import builtins
    real_import = builtins.__import__

    def _no_psutil(name, *a, **k):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *a, **k)

    monkeypatch.delitem(sys.modules, "psutil", raising=False)
    monkeypatch.setattr(builtins, "__import__", _no_psutil)
    assert _REAL_SCAN(engine_root=root) == []


def test_list_all_db_writers_unions_registry_and_scan(root, monkeypatch):
    """The union dedups by pid: a registered writer + a distinct scanned one =
    both; a pid in both = one entry (registry metadata wins)."""
    m3_halt.register_process("cognitive-loop", engine_root=root)  # this pid, registered

    class _FakeProc:
        def __init__(self, pid, cmdline):
            self.info = {"pid": pid, "cmdline": cmdline, "create_time": 0}

    class _FakePsutil:
        NoSuchProcess = AccessDenied = ZombieProcess = type("E", (Exception,), {})
        @staticmethod
        def process_iter(_attrs):
            # a DIFFERENT pid, only discoverable by cmdline (unregistered)
            return iter([_FakeProc(4242, ["python", "embed_server_inproc.py"])])

    # Restore the REAL scan (the autouse fixture stubbed it) so list_all_db_writers
    # exercises the genuine union path against our mocked psutil.
    monkeypatch.setattr(m3_halt, "scan_db_writer_processes", _REAL_SCAN)
    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil)
    allw = m3_halt.list_all_db_writers(engine_root=root)
    pids = sorted(p.pid for p in allw)
    assert os.getpid() in pids and 4242 in pids and len(pids) == 2


# ── elevated-kill help commands (cross-OS) ─────────────────────────────────────
def test_elevated_kill_commands_windows(monkeypatch):
    monkeypatch.setattr(m3_halt.os, "name", "nt")
    cmds = m3_halt.elevated_kill_commands([111, 222])
    assert len(cmds) == 1
    assert "taskkill" in cmds[0] and "/PID 111" in cmds[0] and "/PID 222" in cmds[0]


def test_elevated_kill_commands_posix(monkeypatch):
    monkeypatch.setattr(m3_halt.os, "name", "posix")
    cmds = m3_halt.elevated_kill_commands([111, 222])
    # sudo kill (TERM) then a kill -9 escalation line — same on Linux and macOS
    assert any(c.startswith("sudo kill 111 222") for c in cmds)
    assert any("kill -9" in c for c in cmds)


def test_elevated_kill_commands_empty_and_filtered(monkeypatch):
    monkeypatch.setattr(m3_halt.os, "name", "posix")
    assert m3_halt.elevated_kill_commands([]) == []
    assert m3_halt.elevated_kill_commands([0, -5]) == []


def test_scan_name_fallback_finds_elevated_mcp(root, monkeypatch):
    """When cmdline is unreadable (elevated), a name-identifiable writer
    (mcp-memory) is still reported — flagged elevated? — so it isn't silently
    missed. A bare python with no cmdline is NOT reported (avoids false flood)."""
    class _FakeProc:
        def __init__(self, pid, name, cmdline):
            self.info = {"pid": pid, "name": name, "cmdline": cmdline}

    class _FakePsutil:
        NoSuchProcess = AccessDenied = ZombieProcess = type("E", (Exception,), {})
        @staticmethod
        def process_iter(_attrs):
            return iter([
                _FakeProc(500, "mcp-memory.exe", []),   # elevated: no cmdline, name known
                _FakeProc(501, "pythonw.exe", []),       # elevated bare python: NOT reported
            ])

    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil)
    found = _REAL_SCAN(engine_root=root)
    roles = {p.pid: p.role for p in found}
    assert 500 in roles and "mcp" in roles[500]
    assert 501 not in roles  # bare python with no cmdline is not flagged
