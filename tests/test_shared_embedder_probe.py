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
