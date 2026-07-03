"""`m3 embedder shared` / `unshared` — the shared-GPU-embedder on-ramp.

`shared` writes <config_root>/.embed_config.json so every m3 process disables
its own in-process embedder and defers to one shared server (one CUDA context,
~9-10 GB reclaimed). `unshared` removes it. The config file is the headless-safe
mechanism (a scheduled-task daemon doesn't inherit shell env — §3).
"""
import json
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from m3_memory import embedder_admin as EA  # noqa: E402


def test_config_path_honors_config_root(monkeypatch, tmp_path):
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    assert EA._embed_config_path() == str(tmp_path / ".embed_config.json")


def test_config_path_falls_back_to_memory_root(monkeypatch, tmp_path):
    monkeypatch.delenv("M3_CONFIG_ROOT", raising=False)
    monkeypatch.setenv("M3_MEMORY_ROOT", str(tmp_path))
    assert EA._embed_config_path() == str(tmp_path / "config" / ".embed_config.json")


def test_shared_writes_config(monkeypatch, tmp_path):
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    rc = EA.cmd_shared(types.SimpleNamespace(port=8082))
    assert rc == 0
    cfg = json.loads((tmp_path / ".embed_config.json").read_text())
    assert cfg["disable_inproc_embedder"] is True
    assert cfg["fallback_url"] == "http://127.0.0.1:8082"


def test_shared_honors_custom_port(monkeypatch, tmp_path):
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    EA.cmd_shared(types.SimpleNamespace(port=8091))
    cfg = json.loads((tmp_path / ".embed_config.json").read_text())
    assert cfg["fallback_url"] == "http://127.0.0.1:8091"


def test_unshared_removes_config(monkeypatch, tmp_path):
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    EA.cmd_shared(types.SimpleNamespace(port=8082))
    assert (tmp_path / ".embed_config.json").exists()
    rc = EA.cmd_unshared(types.SimpleNamespace())
    assert rc == 0
    assert not (tmp_path / ".embed_config.json").exists()


def test_unshared_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    # no config present -> clean no-op, rc 0, no raise
    assert EA.cmd_unshared(types.SimpleNamespace()) == 0


def test_shared_config_is_read_by_embed_cascade(monkeypatch, tmp_path):
    """End-to-end: the file `shared` writes actually disables tier-1 in embed.py."""
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    monkeypatch.delenv("M3_EMBED_GGUF", raising=False)
    monkeypatch.delenv("M3_EMBED_GGUF_AUTODETECT", raising=False)
    EA.cmd_shared(types.SimpleNamespace(port=8082))
    # import embed.py fresh with bin/ on path so it reads the just-written config
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
    import importlib
    from memory import embed as E
    importlib.reload(E)
    assert E._EMBED_GGUF_AUTODETECT is False
    assert E._EMBED_FALLBACK_URL == "http://127.0.0.1:8082"
