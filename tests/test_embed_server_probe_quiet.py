"""embed_server_probe quiets the llama.cpp/GGML backend's stderr chatter.

The Rust `m3-embed-server doctor` subprocess loads a GGUF via llama.cpp, which
prints INFO-level model-load notices ("vocab missing newline token ...") and
Metal teardown lines ("ggml_metal_free", "llama_context ...") to stderr. With
inherited stderr these interleave with the probe's stdout and scroll the
readable summary off-screen. The probe sets GGML_LOG_LEVEL=4 (error-only) in the
subprocess env to suppress them — while respecting an operator-set value.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from doctor import embed_server_probe  # noqa: E402


class _FakeCompleted:
    returncode = 0


def _capture_env(monkeypatch):
    """Stub which() + subprocess.run; return a dict that records the env passed."""
    captured = {}
    monkeypatch.setattr(embed_server_probe.shutil, "which", lambda _name: "/usr/bin/m3-embed-server")

    def fake_run(argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return _FakeCompleted()

    monkeypatch.setattr(embed_server_probe.subprocess, "run", fake_run)
    return captured


def test_sets_ggml_log_level_quiet_by_default(monkeypatch):
    monkeypatch.delenv("GGML_LOG_LEVEL", raising=False)
    captured = _capture_env(monkeypatch)
    rc = embed_server_probe.run()
    assert rc == 0
    assert captured["env"] is not None
    # error-only => suppresses the harmless model-load / Metal-teardown chatter
    assert captured["env"]["GGML_LOG_LEVEL"] == "4"


def test_respects_operator_override(monkeypatch):
    # A power user who WANTS the verbose llama.cpp logs can set it themselves.
    monkeypatch.setenv("GGML_LOG_LEVEL", "1")
    captured = _capture_env(monkeypatch)
    embed_server_probe.run()
    assert captured["env"]["GGML_LOG_LEVEL"] == "1"


def test_skips_cleanly_when_binary_absent(monkeypatch):
    monkeypatch.setattr(embed_server_probe.shutil, "which", lambda _name: None)
    # Should not call subprocess.run at all; returns 0 (not a Python-side failure).
    called = {"run": False}

    def boom(*a, **k):
        called["run"] = True
        raise AssertionError("subprocess.run should not be called when binary absent")

    monkeypatch.setattr(embed_server_probe.subprocess, "run", boom)
    assert embed_server_probe.run() == 0
    assert called["run"] is False
