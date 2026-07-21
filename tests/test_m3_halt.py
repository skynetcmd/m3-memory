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


# ── kill_stale_daemons (reap on install/upgrade) ───────────────────────────────
def test_kill_stale_daemons_skips_self_and_parent(root, monkeypatch):
    """Never kill our own pid or our parent — an installer must not saw off the
    branch it sits on. Both are 'discovered' yet neither is targeted."""
    my, parent = os.getpid(), os.getppid()
    monkeypatch.setattr(m3_halt, "list_all_db_writers", lambda engine_root=None: [
        m3_halt.ProcInfo(pid=my, role="cognitive-loop", started_at="",
                         engine_root=root, path=Path()),
        m3_halt.ProcInfo(pid=parent, role="dashboard", started_at="",
                         engine_root=root, path=Path()),
    ])
    # If either were targeted, _pid_is_alive/os.kill would run; assert none did.
    monkeypatch.setattr(m3_halt, "_pid_is_alive",
                        lambda pid: pytest.fail(f"must not touch protected pid {pid}"))
    assert m3_halt.kill_stale_daemons(engine_root=root) == []


def test_kill_stale_daemons_dead_pid_counts_killed(root, monkeypatch):
    """A discovered writer whose pid is already dead is the desired end state —
    reported killed=True without any kill attempt."""
    monkeypatch.setattr(m3_halt, "list_all_db_writers", lambda engine_root=None: [
        m3_halt.ProcInfo(pid=_DEAD_PID, role="cognitive-loop", started_at="",
                         engine_root=root, path=Path())])
    monkeypatch.setattr(m3_halt, "_pid_is_alive", lambda pid: False)
    res = m3_halt.kill_stale_daemons(engine_root=root)
    assert res == [{"pid": _DEAD_PID, "role": "cognitive-loop",
                    "killed": True, "error": None}]


def test_kill_stale_daemons_kills_live_writer_windows(root, monkeypatch):
    """A live discovered writer is killed via taskkill /F /T on Windows; success
    is confirmed by the pid going dead."""
    victim = 4242
    calls = {}
    monkeypatch.setattr(m3_halt, "list_all_db_writers", lambda engine_root=None: [
        m3_halt.ProcInfo(pid=victim, role="cognitive-loop", started_at="",
                         engine_root=root, path=Path())])
    monkeypatch.setattr(m3_halt.os, "name", "nt")
    # alive on the first probe, dead after the kill runs
    state = {"alive": True}
    monkeypatch.setattr(m3_halt, "_pid_is_alive", lambda pid: state["alive"])

    class _CP:
        returncode = 0
        stderr = stdout = ""

    def _fake_run(cmd, **kw):
        calls["cmd"] = cmd
        state["alive"] = False  # taskkill took effect
        return _CP()

    import subprocess as _sp
    monkeypatch.setattr(_sp, "run", _fake_run)
    res = m3_halt.kill_stale_daemons(engine_root=root)
    assert calls["cmd"][:3] == ["taskkill", "/F", "/T"]
    assert str(victim) in calls["cmd"]
    assert res == [{"pid": victim, "role": "cognitive-loop",
                    "killed": True, "error": None}]


def test_kill_stale_daemons_reports_stuck_writer(root, monkeypatch):
    """A writer that survives the kill (e.g. elevated, unprivileged installer) is
    reported killed=False with an error — never a false success."""
    victim = 4243
    monkeypatch.setattr(m3_halt, "list_all_db_writers", lambda engine_root=None: [
        m3_halt.ProcInfo(pid=victim, role="dashboard", started_at="",
                         engine_root=root, path=Path())])
    monkeypatch.setattr(m3_halt.os, "name", "nt")
    monkeypatch.setattr(m3_halt, "_pid_is_alive", lambda pid: True)  # never dies

    class _CP:
        returncode = 1
        stderr = "ERROR: Access is denied."
        stdout = ""

    import subprocess as _sp
    monkeypatch.setattr(_sp, "run", lambda cmd, **kw: _CP())
    res = m3_halt.kill_stale_daemons(engine_root=root, timeout=0.1)
    assert len(res) == 1
    assert res[0]["killed"] is False
    assert "denied" in res[0]["error"].lower()


def test_kill_stale_daemons_empty_when_nothing_running(root, monkeypatch):
    monkeypatch.setattr(m3_halt, "list_all_db_writers", lambda engine_root=None: [])
    assert m3_halt.kill_stale_daemons(engine_root=root) == []


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


# ── Single-instance lock ────────────────────────────────────────────────────
import json as _json  # noqa: E402


def _lock_file(root, role):
    return Path(root) / ".internal" / f"{role}.lock"


def _owner_file(root, role):
    return Path(root) / ".internal" / f"{role}.lock.owner"


def test_lock_acquire_returns_result_and_writes_owner(root):
    res = m3_halt.acquire_single_instance("dashboard", engine_root=root,
                                          extra={"host": "127.0.0.1", "port": 8088})
    assert res.status is m3_halt.LockStatus.ACQUIRED
    assert res.runnable and res.lock.acquired and res.lock.fd >= 0
    # Owner identity is written to the SEPARATE readable owner file.
    op = _owner_file(root, "dashboard")
    assert op.exists()
    data = _json.loads(op.read_text())
    assert data["pid"] == os.getpid() and data["role"] == "dashboard"
    assert data["extra"]["port"] == 8088
    assert data["create_time"] is not None
    res.lock.release()
    assert not op.exists()  # release removes the owner file


def test_lock_reentrant_returns_same_handle(root):
    first = m3_halt.acquire_single_instance("dashboard", engine_root=root,
                                            extra={"port": 9})
    assert first.status is m3_halt.LockStatus.ACQUIRED
    # Same process re-acquiring must NOT lose (that would make it exit on itself);
    # it gets REENTRANT with the SAME handle.
    second = m3_halt.acquire_single_instance("dashboard", engine_root=root)
    assert second.status is m3_halt.LockStatus.REENTRANT
    assert second.lock is first.lock
    assert second.runnable and not second.should_exit_already_running
    first.lock.release()


def test_lock_context_manager_releases(root):
    op = _owner_file(root, "mcp-proxy")
    res = m3_halt.acquire_single_instance("mcp-proxy", engine_root=root)
    with res.lock:
        assert op.exists()
    assert not op.exists()  # __exit__ released


def test_lock_degraded_config_error_when_dir_unwritable(root, monkeypatch):
    # Fail-safe (§3): if the lock file can't be opened, acquire must NOT crash —
    # it returns a CONFIG_ERROR result with a degraded handle so the service runs.
    def _boom(*a, **k):
        raise OSError("no dir for you")
    monkeypatch.setattr(m3_halt.Path, "mkdir", _boom)
    res = m3_halt.acquire_single_instance("dashboard", engine_root=root)
    assert res.status is m3_halt.LockStatus.CONFIG_ERROR
    assert res.runnable  # run anyway
    assert res.lock is not None and not res.lock.acquired and res.lock.fd == -1
    assert res.status.exit_code == m3_halt.EXIT_LOCK_CONFIG_ERROR  # 5


def test_lock_not_in_pid_registry(root):
    # A held lock file lives in .internal/ but is NOT a PID/ registry entry, so
    # list_live_processes (which reads PID/) does not see it — separate surfaces.
    res = m3_halt.acquire_single_instance("dashboard", engine_root=root)
    assert m3_halt.list_live_processes(engine_root=root) == []
    res.lock.release()


def test_exit_codes_are_distinct_and_high_fidelity():
    # A high-fidelity loser signal: each category has its own code so $? tells the
    # operator WHY. 4/5/6 avoid collisions (argparse=2, embed GGUF-mismatch=3).
    assert m3_halt.EXIT_ALREADY_RUNNING == 4
    assert m3_halt.EXIT_LOCK_CONFIG_ERROR == 5
    assert m3_halt.EXIT_LOCK_ERROR == 6
    assert m3_halt.LockStatus.HELD_BY_PEER.exit_code == 4
    assert m3_halt.LockStatus.CONFIG_ERROR.exit_code == 5
    assert m3_halt.LockStatus.LOCK_ERROR.exit_code == 6
    assert m3_halt.LockStatus.ACQUIRED.exit_code == 0
    assert m3_halt.LockStatus.REENTRANT.exit_code == 0


def test_lock_events_logged(root):
    # The append-only audit trail records ownership changes.
    res = m3_halt.acquire_single_instance("dashboard", engine_root=root,
                                          extra={"port": 8088})
    res.lock.release()
    evs = m3_halt.read_lock_events(engine_root=root, role="dashboard")
    kinds = [e["event"] for e in evs]
    assert "acquired" in kinds and "released" in kinds
    acq = next(e for e in evs if e["event"] == "acquired")
    assert acq["pid"] == os.getpid() and acq.get("port") == 8088


# ── Registry reuse-safe reaping (FG6) ───────────────────────────────────────
def test_list_reaps_reused_pid_entry(root):
    # A registry entry for OUR (live) pid but a wrong create_time = the pid was
    # reused; list_live_processes must reap it, not report it as a live writer.
    pdir = Path(root) / ".internal" / "PID"
    pdir.mkdir(parents=True)
    entry = pdir / f"embed-server.{os.getpid()}"
    entry.write_text(_json.dumps({
        "pid": os.getpid(), "role": "embed-server", "started_at": "x",
        "create_time": 1.0,  # bogus
        "engine_root": root, "protocol": 1}))
    live = m3_halt.list_live_processes(engine_root=root)
    assert live == [], "a reused-pid entry must be reaped, not counted live"
    assert not entry.exists()


# ── Cross-process mutual exclusion (the CORE guarantee — real subprocesses) ──
import subprocess as _subprocess  # noqa: E402
import textwrap as _textwrap  # noqa: E402
import time as _time  # noqa: E402

# A tiny holder program: acquire the lock for `role` under `engine_root`, print a
# ready line, then hold for `hold_s` seconds. Run as a real separate process so
# the OS advisory lock is genuinely tested across process boundaries (an
# in-process second acquire is REENTRANT and would not exercise the mutex).
_HOLDER_SRC = _textwrap.dedent(
    """
    import sys, os, time
    sys.path.insert(0, sys.argv[4])          # bin dir
    os.environ["M3_ENGINE_ROOT"] = sys.argv[1]
    import m3_halt
    r = m3_halt.acquire_single_instance(sys.argv[2], extra={"port": 8088})
    assert r.status is m3_halt.LockStatus.ACQUIRED, r.status
    print("READY " + str(os.getpid()), flush=True)
    time.sleep(float(sys.argv[3]))
    """
)


def _spawn_holder(root, role, hold_s, bin_dir):
    p = _subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SRC, str(root), role, str(hold_s), str(bin_dir)],
        stdout=_subprocess.PIPE, stderr=_subprocess.PIPE, text=True,
    )
    # Wait for the READY line so we know it holds the lock before we contend.
    line = p.stdout.readline()
    assert line.startswith("READY "), f"holder failed to start: {line!r} / {p.stderr.read()!r}"
    return p, int(line.split()[1])


def _cleanup_holder(p):
    """Reap the holder and CLOSE its pipes so no FileIO is left to be finalized
    by the GC (which pytest flags as an unraisable-exception warning)."""
    try:
        if p.poll() is None:
            p.kill()
        p.wait(timeout=10)
    finally:
        for stream in (p.stdout, p.stderr, p.stdin):
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass


def test_cross_process_mutual_exclusion(tmp_path):
    """THE core guarantee: while a real OTHER process holds the lock, this process
    gets HELD_BY_PEER (never a second ACQUIRED); after it exits, this process
    wins. Untested elsewhere — an in-process re-acquire is REENTRANT."""
    root = str(tmp_path / "engine")
    bin_dir = str(_BIN)
    holder, holder_pid = _spawn_holder(root, "embed-server", 4.0, bin_dir)
    try:
        # Contend while the holder is alive → must LOSE, and identify the holder.
        res = m3_halt.acquire_single_instance("embed-server", engine_root=root)
        assert res.status is m3_halt.LockStatus.HELD_BY_PEER, res.status
        assert res.lock is None
        assert res.owner is not None and res.owner.pid == holder_pid
    finally:
        _cleanup_holder(holder)
    # Holder gone → we must now WIN. On Windows the OS releases a killed process's
    # file lock ASYNCHRONOUSLY — there is a few-ms window after wait() returns
    # where the lock is still held even though the pid is dead. Retry-settle for
    # it (the same pattern test_cross_process_lock_released_on_holder_death uses),
    # rather than assuming the POSIX-style synchronous release (§1: 3 OSes).
    for _ in range(20):
        res2 = m3_halt.acquire_single_instance("embed-server", engine_root=root)
        if res2.status is m3_halt.LockStatus.ACQUIRED:
            res2.lock.release()
            break
        _time.sleep(0.1)
    else:
        raise AssertionError(f"did not ACQUIRE after holder death: {res2.status}")


def test_cross_process_lock_released_on_holder_death(tmp_path):
    """OS-advisory-lock property that PID files lack: when the holder is KILLED
    (no clean release), the OS drops the lock, so the next acquirer wins — no
    stale lock, no reclaim needed."""
    root = str(tmp_path / "engine")
    holder, holder_pid = _spawn_holder(root, "dashboard", 30.0, str(_BIN))
    try:
        # Confirm it's held.
        assert m3_halt.acquire_single_instance("dashboard", engine_root=root).status \
            is m3_halt.LockStatus.HELD_BY_PEER
        # HARD KILL (no atexit / SIGTERM cleanup runs) — the OS must still free it.
        holder.kill()
        holder.wait(timeout=10)
        # Small settle for the OS to reclaim the handle.
        for _ in range(20):
            res = m3_halt.acquire_single_instance("dashboard", engine_root=root)
            if res.status is m3_halt.LockStatus.ACQUIRED:
                res.lock.release()
                break
            _time.sleep(0.1)
        else:
            raise AssertionError("lock not released after holder was killed")
    finally:
        _cleanup_holder(holder)


# ── Thread-safety + timeout (concurrency correctness) ────────────────────────
def test_lock_thread_safe_single_winner(tmp_path):
    """Many threads acquiring the same role in ONE process must yield exactly one
    ACQUIRED and the rest REENTRANT — never two ACQUIRED. Without per-process
    serialization, fcntl.flock (per-process on Linux) would let multiple threads
    both satisfy the lock."""
    import threading as _t
    root = str(tmp_path / "engine")
    outcomes = []
    barrier = _t.Barrier(12)

    def worker():
        barrier.wait()  # release all threads at once → max contention
        r = m3_halt.acquire_single_instance("svc", engine_root=root)
        outcomes.append(r.status)

    threads = [_t.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    acquired = sum(1 for s in outcomes if s is m3_halt.LockStatus.ACQUIRED)
    reentrant = sum(1 for s in outcomes if s is m3_halt.LockStatus.REENTRANT)
    assert acquired == 1, f"exactly one ACQUIRED expected, got {acquired}"
    assert acquired + reentrant == 12, "all others must be REENTRANT (no lost/errored)"
    # cleanup
    lk = m3_halt._HELD_LOCKS.get(("svc", str(m3_halt._engine_root(root))))
    if lk:
        lk.release()


def test_lock_timeout_param_accepted_and_acquires(tmp_path):
    # timeout>0 must still ACQUIRE a free lock (and not hang). Cross-process
    # waiting is covered by the subprocess tests; here we pin the param + no-hang.
    root = str(tmp_path / "engine")
    res = m3_halt.acquire_single_instance("svc", engine_root=root, timeout=1.0)
    assert res.status is m3_halt.LockStatus.ACQUIRED
    res.lock.release()
