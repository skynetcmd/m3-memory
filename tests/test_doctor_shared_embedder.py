"""doctor's shared-embedder awareness (_shared_embedder_status).

Reports whether m3 is in shared mode (.embed_config.json) and, if so, health-
checks the shared server. A shared config pointing at a DEAD endpoint is a
silent-failure trap (§3) — doctor must warn loudly. Read-only; never raises.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from m3_memory.install import sections as S  # noqa: E402


def _run(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    S._shared_embedder_status()
    return capsys.readouterr().out


def test_no_config_reports_per_process(monkeypatch, tmp_path, capsys):
    out = _run(monkeypatch, tmp_path, capsys)
    assert "per-process" in out
    assert "m3 embedder shared" in out  # the enable tip


def test_shared_but_server_down_warns_loud(monkeypatch, tmp_path, capsys):
    (tmp_path / ".embed_config.json").write_text(json.dumps(
        {"disable_inproc_embedder": True, "fallback_url": "http://127.0.0.1:9"}))
    out = _run(monkeypatch, tmp_path, capsys)
    assert "SHARED" in out
    # the trap must be surfaced, not silent
    assert "WARN" in out and "UNREACHABLE" in out
    assert "unshared" in out  # the revert instruction


def test_config_present_but_not_disabled_is_per_process(monkeypatch, tmp_path, capsys):
    (tmp_path / ".embed_config.json").write_text(json.dumps(
        {"disable_inproc_embedder": False}))
    out = _run(monkeypatch, tmp_path, capsys)
    assert "per-process" in out


def test_malformed_config_is_reported_not_raised(monkeypatch, tmp_path, capsys):
    (tmp_path / ".embed_config.json").write_text("{ not json")
    # must not raise
    out = _run(monkeypatch, tmp_path, capsys)
    assert "UNKNOWN" in out or "unreadable" in out
