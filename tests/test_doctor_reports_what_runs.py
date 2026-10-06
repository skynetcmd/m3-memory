"""Doctor lines must describe the running configuration.

Hazard: a line that contradicts another line of the same run — a native
embedder headline in shared mode, a green tick for a stopped embed server,
child output outside its section, an outdated config.json version labelled
installed — trains readers to ignore doctor.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
for _p in (os.path.join(_ROOT, "bin"), _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from doctor import embed_server_probe as esp  # noqa: E402

from m3_memory import installer  # noqa: E402


class _R:
    def __init__(self, rc, out, err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _status_build(monkeypatch, state):
    """A binary without `doctor` (prints USAGE, rc 2) whose `status` says state."""
    monkeypatch.setattr(esp, "_resolve_binary", lambda: "/bin/m3-embed-server")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(kw)
        if cmd[1] == "doctor":
            return _R(2, "USAGE: m3-embed-server <install|status>")
        return _R(0, state + "\n")

    monkeypatch.setattr(esp.subprocess, "run", fake_run)
    return calls


def test_a_stopped_server_is_not_reported_ok(monkeypatch, capsys):
    _status_build(monkeypatch, "stopped")
    esp.run(brief=True)
    out = capsys.readouterr().out
    assert "✅" not in out and "stopped" in out


def test_verbose_status_is_printed_inside_its_section(monkeypatch, capsys):
    calls = _status_build(monkeypatch, "running")
    esp.run(brief=False)
    out = capsys.readouterr().out
    assert all(kw.get("capture_output") for kw in calls), "child output would bypass the section"
    assert out.index("Rust-side service health") < out.index("running")


def test_headline_names_the_shared_server_in_shared_mode(monkeypatch, tmp_path):
    cfg = tmp_path / ".embed_config.json"
    cfg.write_text(json.dumps({"disable_inproc_embedder": True}), encoding="utf-8")
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    from m3_memory import rust_core_install

    monkeypatch.setattr(rust_core_install, "active_embedder_tier", lambda: {"native": True})
    assert installer.status_summary()["embedder"] == "shared server (:8082)"


def test_a_stale_config_version_is_not_called_installed(monkeypatch, capsys, tmp_path):
    from m3_memory import __version__

    bridge = tmp_path / "memory_bridge.py"
    bridge.write_text("", encoding="utf-8")
    monkeypatch.setattr(installer, "load_config", lambda: {
        "version": "2026.8.19.2", "bridge_path": str(bridge)})
    monkeypatch.setattr(installer, "find_bridge", lambda: bridge)
    assert __version__ != "2026.8.19.2"
    try:
        installer.doctor()
    except Exception:  # noqa: BLE001 — later sections probe the host; only the header matters
        pass
    out = capsys.readouterr().out
    assert "installed version:       2026.8.19.2" not in out
    assert "last fetch version:      2026.8.19.2" in out


def test_subprocess_is_the_module_used_by_the_probe():
    assert esp.subprocess is subprocess


def test_tier_summary_names_the_shared_server_in_shared_mode(monkeypatch, tmp_path):
    """Setup's summary and doctor's embedder section print this summary; a
    native-core install must not read as in-process embedding in shared mode."""
    import types

    from m3_memory import rust_core_install

    cfg = tmp_path / ".embed_config.json"
    cfg.write_text(json.dumps({"disable_inproc_embedder": True}), encoding="utf-8")
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    fake = types.ModuleType("m3_core_rs")
    fake.EmbeddedEmbedder = object
    fake.__version__ = "3.10.1"
    monkeypatch.setitem(sys.modules, "m3_core_rs", fake)
    tier = rust_core_install.active_embedder_tier()
    assert tier["native"] is True
    assert tier["summary"].startswith("shared server (:8082)")
    assert "tier-1 in-process" not in tier["summary"]


def test_zero_entities_names_the_missing_chat_model(monkeypatch, capsys):
    from doctor import cognitive_loop_probe as clp

    monkeypatch.setattr(clp, "_installed_active", lambda: (True, True, "systemd"))
    monkeypatch.setattr(clp, "_process_running", lambda: True)
    monkeypatch.setattr(clp, "_entity_stats", lambda: (12, 0))
    monkeypatch.setattr(clp, "_chat_model_reachable", lambda: False)
    clp.run(brief=True)
    assert "needs a chat model" in capsys.readouterr().out

    monkeypatch.setattr(clp, "_chat_model_reachable", lambda: True)
    clp.run(brief=True)
    assert "hasn't distilled yet" in capsys.readouterr().out
