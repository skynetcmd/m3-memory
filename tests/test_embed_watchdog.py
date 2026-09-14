"""Tests for bin/m3_embed_watchdog.py — the shared-embedder liveness self-heal.

Regression origin (2026-09-13): the tier-2 server crashed repeatedly inside the
GPU driver, Windows SCM exhausted its RESTART x3 budget and gave up, the driver
was fixed the next day, and nothing ever tried again — 30 hours down. The
property under test is the one that closes that gap: a liveness check has NO
budget, so it recovers on the first tick after the environment heals.

Hermetic by construction (§3): no HTTP, no subprocess, no service manager, no
database. Every boundary is mocked at the layer the call actually happens.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(cfg_root: Path):
    """Import m3_embed_watchdog with CONFIG_ROOT pinned to a tmp dir.

    The module resolves its roots at import time (module-level CONFIG_ROOT), so
    each test gets a freshly-imported copy under a monkeypatched env rather
    than mutating a shared singleton.
    """
    if str(_BIN) not in sys.path:
        sys.path.insert(0, str(_BIN))
    spec = importlib.util.spec_from_file_location(
        "m3_embed_watchdog_t", _BIN / "m3_embed_watchdog.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    mod.CONFIG_ROOT = cfg_root
    mod.STATE = cfg_root / ".embed_watchdog_state.json"
    mod.EMBED_CONFIG = cfg_root / ".embed_config.json"
    mod.log = lambda *_a, **_k: None  # silence; assertions target behaviour
    return mod


@pytest.fixture
def cfg_root(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    return tmp_path


def _write_shared(cfg_root: Path, *, enabled=True, url="http://127.0.0.1:8082"):
    (cfg_root / ".embed_config.json").write_text(json.dumps({
        "disable_inproc_embedder": enabled, "fallback_url": url,
    }), encoding="utf-8")


class _Calls:
    """Records subprocess invocations instead of running them."""

    def __init__(self, returncode=0):
        self.cmds: list[list[str]] = []
        self.returncode = returncode

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)

        class R:
            pass
        r = R()
        r.returncode = self.returncode
        r.stdout = ""
        r.stderr = "" if self.returncode == 0 else "Access is denied."
        return r


# ── the core property: no restart budget ──────────────────────────────────────
def test_recovers_after_supervisor_budget_would_be_exhausted(cfg_root, monkeypatch):
    """THE regression test. A liveness check must still act on tick N+100,
    long after any OS supervisor would have stopped retrying."""
    m = _load(cfg_root)
    _write_shared(cfg_root)
    monkeypatch.setattr(m, "server_healthy", lambda url: False)
    calls = _Calls()
    monkeypatch.setattr(m.subprocess, "run", calls)

    # Advance a synthetic clock past the backoff window between ticks instead
    # of stubbing recently_restarted(): the real function MUST run, or a
    # budget planted inside it would go undetected (§12c — a guard that
    # cannot fail is blind).
    clock = {"t": time.time()}
    monkeypatch.setattr(m.time, "time", lambda: clock["t"])

    for _ in range(100):
        m.main()
        clock["t"] += m._MIN_RESTART_GAP_S + 1

    # Exactly 50: _FAILED_CHECKS=2 means one start per two ticks, and the
    # counter resets after each attempt. The POINT is that the 50th attempt
    # happens at all — an SCM-style budget would have stopped after 3.
    assert len(calls.cmds) == 100 // m._FAILED_CHECKS, (
        f"liveness check made {len(calls.cmds)} attempts over 100 ticks — "
        "expected one per _FAILED_CHECKS ticks, forever")
    assert len(calls.cmds) > 3, (
        "stopped retrying like a budgeted supervisor — that reintroduces "
        "the exhausted-budget bug")


def test_first_miss_does_not_act(cfg_root, monkeypatch):
    """One slow /health during a model load must not bounce the server (§3:
    a false alarm is a violation, not a safe default)."""
    m = _load(cfg_root)
    _write_shared(cfg_root)
    monkeypatch.setattr(m, "server_healthy", lambda url: False)
    calls = _Calls()
    monkeypatch.setattr(m.subprocess, "run", calls)

    assert m.main() == 0
    assert calls.cmds == [], "acted on a single missed health check"


def test_second_consecutive_miss_acts(cfg_root, monkeypatch):
    m = _load(cfg_root)
    _write_shared(cfg_root)
    monkeypatch.setattr(m, "server_healthy", lambda url: False)
    calls = _Calls()
    monkeypatch.setattr(m.subprocess, "run", calls)

    m.main()
    m.main()
    assert len(calls.cmds) == 1


def test_recovery_resets_the_failure_counter(cfg_root, monkeypatch):
    m = _load(cfg_root)
    _write_shared(cfg_root)
    healthy = {"v": False}
    monkeypatch.setattr(m, "server_healthy", lambda url: healthy["v"])
    monkeypatch.setattr(m.subprocess, "run", _Calls())

    m.main()                     # miss 1
    healthy["v"] = True
    m.main()                     # recovered
    state = json.loads((cfg_root / ".embed_watchdog_state.json").read_text())
    assert state["consecutive_failures"] == 0


def test_healthy_server_is_never_touched(cfg_root, monkeypatch):
    m = _load(cfg_root)
    _write_shared(cfg_root)
    monkeypatch.setattr(m, "server_healthy", lambda url: True)
    calls = _Calls()
    monkeypatch.setattr(m.subprocess, "run", calls)

    for _ in range(5):
        assert m.main() == 0
    assert calls.cmds == [], "started a server that was already serving"


def test_backoff_prevents_fork_bomb(cfg_root, monkeypatch):
    """A server that crashes on startup must not be restarted every tick."""
    m = _load(cfg_root)
    _write_shared(cfg_root)
    monkeypatch.setattr(m, "server_healthy", lambda url: False)
    calls = _Calls()
    monkeypatch.setattr(m.subprocess, "run", calls)

    m.main()
    m.main()                     # acts, stamps last_restart_ts
    assert len(calls.cmds) == 1
    m.main()
    m.main()                     # inside the backoff window
    assert len(calls.cmds) == 1, "backoff did not suppress a rapid re-start"


def test_backoff_expires(cfg_root, monkeypatch):
    m = _load(cfg_root)
    _write_shared(cfg_root)
    monkeypatch.setattr(m, "server_healthy", lambda url: False)
    calls = _Calls()
    monkeypatch.setattr(m.subprocess, "run", calls)

    m.main(); m.main()
    assert len(calls.cmds) == 1
    state = json.loads((cfg_root / ".embed_watchdog_state.json").read_text())
    state["last_restart_ts"] = time.time() - (m._MIN_RESTART_GAP_S + 1)
    (cfg_root / ".embed_watchdog_state.json").write_text(json.dumps(state))
    m.main(); m.main()
    assert len(calls.cmds) == 2, "backoff never expired"


# ── shared-mode gating: never guard a port nobody uses ────────────────────────
def test_unshared_host_is_a_noop(cfg_root, monkeypatch):
    m = _load(cfg_root)
    _write_shared(cfg_root, enabled=False)
    monkeypatch.setattr(m, "server_healthy", lambda url: False)
    calls = _Calls()
    monkeypatch.setattr(m.subprocess, "run", calls)

    for _ in range(5):
        assert m.main() == 0
    assert calls.cmds == [], "acted on a host that does not use a shared server"


def test_missing_config_is_a_noop(cfg_root, monkeypatch):
    m = _load(cfg_root)
    calls = _Calls()
    monkeypatch.setattr(m.subprocess, "run", calls)
    assert m.main() == 0
    assert calls.cmds == []


def test_malformed_config_is_loud_and_stands_down(cfg_root, monkeypatch):
    """§3: a malformed config warns; it never silently reverts to defaults."""
    m = _load(cfg_root)
    (cfg_root / ".embed_config.json").write_text("{not json", encoding="utf-8")
    said: list[str] = []
    m.log = lambda msg: said.append(msg)
    calls = _Calls()
    monkeypatch.setattr(m.subprocess, "run", calls)

    assert m.main() == 0
    assert any("unreadable" in s for s in said), "malformed config failed silently"
    assert calls.cmds == []


def test_custom_fallback_url_is_honored(cfg_root, monkeypatch):
    """The port is configuration, not a constant — a host that moved the
    server must still be guarded."""
    m = _load(cfg_root)
    _write_shared(cfg_root, url="http://127.0.0.1:9999")
    seen: list[str] = []

    def _probe(url):
        seen.append(url)
        return False
    monkeypatch.setattr(m, "server_healthy", _probe)
    monkeypatch.setattr(m.subprocess, "run", _Calls())
    m.main()
    assert seen == ["http://127.0.0.1:9999"]


# ── §1: three OSes, one detector, per-OS action only ──────────────────────────
@pytest.mark.parametrize("osname,expect", [
    ("Windows", "sc.exe"),
    ("Linux", "systemctl"),
    ("Darwin", "launchctl"),
])
def test_restart_backend_per_os(cfg_root, monkeypatch, osname, expect):
    m = _load(cfg_root)
    monkeypatch.setattr(m, "_os_name", lambda: osname)
    cmds, what = m._restart_backend()
    assert cmds and cmds[0][0] == expect, f"{osname} uses the wrong supervisor"
    assert what


def test_darwin_never_sigkills(cfg_root, monkeypatch):
    """kickstart -k SIGKILLs a live server — the damage a watchdog exists to
    avoid. Mirrors the same rule in m3_loop_watchdog._restart_backend."""
    m = _load(cfg_root)
    monkeypatch.setattr(m, "_os_name", lambda: "Darwin")
    cmds, _ = m._restart_backend()
    assert not any("-k" in c for cmd in cmds for c in cmd)


def test_unelevated_failure_is_reported_not_swallowed(cfg_root, monkeypatch):
    """Windows: an unelevated watchdog cannot start a LocalSystem service.
    That must be stated with what was observed, never silently ignored (§3)."""
    m = _load(cfg_root)
    _write_shared(cfg_root)
    monkeypatch.setattr(m, "_os_name", lambda: "Windows")
    monkeypatch.setattr(m, "server_healthy", lambda url: False)
    monkeypatch.setattr(m.subprocess, "run", _Calls(returncode=5))
    said: list[str] = []
    m.log = lambda msg: said.append(msg)

    m.main(); m.main()
    assert any("observed:" in s for s in said), "no observed-level evidence logged"
    assert any("inspect:" in s for s in said), "no knob named to inspect"


def test_health_probe_survives_a_dead_port(cfg_root):
    """server_healthy must return False, never raise, on a closed port —
    the watchdog is least useful if it dies when the thing it guards is down."""
    m = _load(cfg_root)
    assert m.server_healthy("http://127.0.0.1:1") is False


def test_subprocess_failure_to_spawn_is_non_fatal(cfg_root, monkeypatch):
    """A missing sc.exe/systemctl must not take the watchdog down."""
    m = _load(cfg_root)
    _write_shared(cfg_root)
    monkeypatch.setattr(m, "server_healthy", lambda url: False)

    def _boom(cmd, **kw):
        raise OSError("not found")
    monkeypatch.setattr(m.subprocess, "run", _boom)
    m.main()
    assert m.main() in (0, 1)  # did not raise
