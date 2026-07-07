"""doctor's actionable shared-embedder probe (bin/doctor/shared_embedder_probe).

Shared mode is the shipped default: config + a live :8082 server + a keep-alive
(Rust OS service PREFERRED, Python scheduled task FALLBACK). The probe FLAGS any
broken piece (non-zero exit) and, with fix=True, repairs it. These tests pin the
verdict matrix and the keep-alive preference by mocking the three detectors, so
no real server/GPU/scheduler is touched (CI-safe).
"""
import json
import sys
from pathlib import Path

import pytest

_BIN = str(Path(__file__).resolve().parents[1] / "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from doctor import shared_embedder_probe as P  # noqa: E402


@pytest.fixture
def cfg_root(monkeypatch, tmp_path):
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    return tmp_path


def _write_shared_config(root, url="http://127.0.0.1:8082"):
    (root / ".embed_config.json").write_text(
        json.dumps({"disable_inproc_embedder": True, "fallback_url": url})
    )


def _patch(monkeypatch, *, health="ok", rust=False, task=None):
    """Mock the three detectors. task: True/False/None (None = non-Windows)."""
    monkeypatch.setattr(P, "_server_health", lambda url, timeout=3.0: (health, {"model": "bge", "dim": 1024}))
    monkeypatch.setattr(P, "_rust_service_present", lambda: rust)
    monkeypatch.setattr(P, "_task_registered_ok", lambda: task)


def test_all_healthy_rust_service(monkeypatch, cfg_root, capsys):
    _write_shared_config(cfg_root)
    _patch(monkeypatch, health="ok", rust=True)
    rc = P.run(brief=False, fix=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "SHARED" in out
    assert "Rust m3-embed-server OS service" in out  # preferred keep-alive reported


def test_all_healthy_python_task_fallback(monkeypatch, cfg_root, capsys):
    # No Rust binary, but the Python task is registered -> still healthy.
    _write_shared_config(cfg_root)
    _patch(monkeypatch, health="ok", rust=False, task=True)
    rc = P.run(brief=False, fix=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "scheduled task" in out and "fallback" in out


def test_config_missing_is_flagged(monkeypatch, cfg_root, capsys):
    # No .embed_config.json at all -> shared mode not enabled -> non-zero.
    _patch(monkeypatch, health="ok", rust=True)
    rc = P.run(brief=False, fix=False)
    out = capsys.readouterr().out
    assert rc == 1
    assert "MISSING" in out and "m3 setup" in out


def test_server_down_is_flagged(monkeypatch, cfg_root, capsys):
    _write_shared_config(cfg_root)
    _patch(monkeypatch, health="down", rust=True)
    rc = P.run(brief=False, fix=False)
    out = capsys.readouterr().out
    assert rc == 1
    assert "not answering" in out


def test_no_keepalive_is_flagged(monkeypatch, cfg_root, capsys):
    # Config + live server, but NEITHER rust service NOR task -> flag: a manual
    # start works now but won't survive a crash/reboot.
    _write_shared_config(cfg_root)
    _patch(monkeypatch, health="ok", rust=False, task=False)
    rc = P.run(brief=False, fix=False)
    out = capsys.readouterr().out
    assert rc == 1
    assert "nothing keeps" in out.lower() or "FAIL" in out
    # both remedies offered, rust preferred first
    assert "m3 embedder install" in out


def test_keepalive_prefers_rust_over_task(monkeypatch, cfg_root):
    # When both a rust binary AND a task exist, keepalive kind is rust-service
    # (preferred) — the probe never double-counts or picks the task.
    _patch(monkeypatch, health="ok", rust=True, task=True)
    kind, ok = P._keepalive()
    assert kind == "rust-service" and ok is True


def test_keepalive_none_when_neither(monkeypatch):
    _patch(monkeypatch, health="ok", rust=False, task=False)
    kind, ok = P._keepalive()
    assert kind == "none" and ok is False


def test_unix_no_keepalive_points_at_rust_not_windows_task(monkeypatch, cfg_root, capsys):
    # On Unix with no Rust binary, the probe must NOT claim a scheduled-task
    # fallback (schtasks is Windows-only). The --fix path points at the Rust
    # systemd/launchd service instead of pretending to register a task (§1 3-OS).
    monkeypatch.setattr(P.sys, "platform", "linux")
    _write_shared_config(cfg_root)
    _patch(monkeypatch, health="ok", rust=False, task=None)
    rc = P.run(brief=False, fix=True)
    out = capsys.readouterr().out
    assert rc == 1  # still unhealthy — no keep-alive could be established
    assert "m3 embedder install" in out
    # must not falsely claim a Windows task was/should be registered on Linux
    assert "schtasks" not in out.lower()


def test_fix_register_task_refuses_on_unix(monkeypatch, capsys):
    monkeypatch.setattr(P.sys, "platform", "darwin")
    assert P._fix_register_task() is False
    out = capsys.readouterr().out
    assert "m3 embedder install" in out


def test_brief_healthy_and_unhealthy(monkeypatch, cfg_root, capsys):
    _write_shared_config(cfg_root)
    _patch(monkeypatch, health="ok", rust=True)
    assert P.run(brief=True, fix=False) == 0
    assert "✅" in capsys.readouterr().out

    # now break it (no config)
    (cfg_root / ".embed_config.json").unlink()
    _patch(monkeypatch, health="ok", rust=True)
    assert P.run(brief=True, fix=False) == 1
    assert "⚠️" in capsys.readouterr().out


# ── M3_EMBED_GGUF leak: localization + loop-termination + verbose command ──────
#
# These pin the three UX fixes: the probe must (1) localize a shell-rc leak to
# file:line, (2) NOT re-emit "run --fix" for a manual-only leak (the dead-end
# loop), and (3) print the exact removal command under --verbose, not only --fix.
# All hermetic: rc scan is pointed at tmp_path, the Windows registry read is
# stubbed, so no real HOME / registry is touched (CI-safe, §2 hermetic).


@pytest.fixture
def no_env_leak(monkeypatch, tmp_path):
    """Baseline clean environment: no process var, no Windows User-env var, and
    the shell-rc scan pointed at an empty tmp dir so a CI runner's real ~/.zshrc
    can never leak into the assertion."""
    monkeypatch.delenv("M3_EMBED_GGUF", raising=False)
    monkeypatch.setattr(P, "_windows_user_env_has", lambda var: False)
    monkeypatch.setattr(P, "_shell_rc_candidates", lambda: [])
    return tmp_path


def test_shell_rc_leak_localizes_to_file_line(monkeypatch, tmp_path):
    rc = tmp_path / ".zshrc"
    rc.write_text(
        "# a comment\n"
        'export PATH="$PATH:/x"\n'
        'export M3_EMBED_GGUF="/models/bge-m3.gguf"\n'
    )
    monkeypatch.setattr(P, "_shell_rc_candidates", lambda: [str(rc)])
    hits = P._find_env_in_shell_rc("M3_EMBED_GGUF")
    assert hits == [f"{rc}:3"]  # exact file:line, not just "shell rc"


def test_commented_export_is_not_a_leak(monkeypatch, tmp_path):
    rc = tmp_path / ".zshrc"
    rc.write_text('# export M3_EMBED_GGUF="/old.gguf"\n')
    monkeypatch.setattr(P, "_shell_rc_candidates", lambda: [str(rc)])
    assert P._find_env_in_shell_rc("M3_EMBED_GGUF") == []


def test_detect_leak_reports_rc_file_line(monkeypatch, no_env_leak):
    rc = no_env_leak / ".zshrc"
    rc.write_text('export M3_EMBED_GGUF="/models/bge-m3.gguf"\n')
    monkeypatch.setenv("M3_EMBED_GGUF", "/models/bge-m3.gguf")  # process sees it too
    monkeypatch.setattr(P, "_shell_rc_candidates", lambda: [str(rc)])
    locs = P._detect_inproc_env_leak()
    assert any(str(rc) in loc and ":1" in loc for loc in locs)


def test_manual_only_brief_does_not_loop_to_fix(monkeypatch, cfg_root, capsys, no_env_leak):
    # Shared config + live server + keep-alive: the ONLY problem is a process-env
    # leak that --fix can't remove. Brief output must guide to the manual step,
    # NOT re-print "run `m3 doctor --fix`" (the dead-end loop).
    _write_shared_config(cfg_root)
    _patch(monkeypatch, health="ok", rust=True)
    monkeypatch.setenv("M3_EMBED_GGUF", "/models/bge-m3.gguf")
    rc = P.run(brief=True, fix=False)
    out = capsys.readouterr().out
    assert rc == 1
    assert "remove it by hand" in out.lower()
    assert "run `m3 doctor --fix`" not in out  # the loop is broken


def test_manual_only_verbose_prints_removal_command(monkeypatch, cfg_root, capsys, no_env_leak):
    # --verbose (fix=False) must print the actual removal command, not merely
    # claim "the fix prints the exact command".
    _write_shared_config(cfg_root)
    _patch(monkeypatch, health="ok", rust=True)
    monkeypatch.setattr(P.sys, "platform", "darwin")
    rc_file = no_env_leak / ".zshrc"
    rc_file.write_text('export M3_EMBED_GGUF="/models/bge-m3.gguf"\n')
    monkeypatch.setenv("M3_EMBED_GGUF", "/models/bge-m3.gguf")
    monkeypatch.setattr(P, "_shell_rc_candidates", lambda: [str(rc_file)])
    rc = P.run(brief=False, fix=False)
    out = capsys.readouterr().out
    assert rc == 1
    assert str(rc_file) in out  # the exact file is named
    assert "new shell" in out.lower() and "restart the mcp client" in out.lower()


def test_manual_only_verbose_prints_windows_command(monkeypatch, cfg_root, capsys, no_env_leak):
    _write_shared_config(cfg_root)
    _patch(monkeypatch, health="ok", rust=True)
    monkeypatch.setattr(P.sys, "platform", "win32")
    monkeypatch.setattr(P, "_windows_user_env_has", lambda var: True)
    rc = P.run(brief=False, fix=False)
    out = capsys.readouterr().out
    assert rc == 1
    assert "SetEnvironmentVariable('M3_EMBED_GGUF', $null, 'User')" in out


def test_no_leak_verbose_stays_clean(monkeypatch, cfg_root, capsys, no_env_leak):
    _write_shared_config(cfg_root)
    _patch(monkeypatch, health="ok", rust=True)
    rc = P.run(brief=False, fix=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "no M3_EMBED_GGUF leak" in out
