"""Tests for the embed-server port already-running check (embedder_admin)."""
from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from m3_memory import embedder_admin as ea  # noqa: E402


def test_port_in_use_true_when_listening():
    # Bind a real ephemeral listener and probe it.
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert ea._port_in_use(port) is True
    finally:
        srv.close()


def test_port_in_use_false_when_closed():
    # Grab a port then close it so nothing is listening.
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()
    assert ea._port_in_use(port) is False


def test_embed_server_port_default(monkeypatch):
    monkeypatch.delenv("M3_EMBED_SERVER_PORT", raising=False)
    assert ea._embed_server_port() == 8082


def test_embed_server_port_env_override(monkeypatch):
    monkeypatch.setenv("M3_EMBED_SERVER_PORT", "9099")
    assert ea._embed_server_port() == 9099


def test_embed_server_port_bad_env_falls_back(monkeypatch):
    monkeypatch.setenv("M3_EMBED_SERVER_PORT", "not-a-number")
    assert ea._embed_server_port() == 8082


def test_warn_if_port_busy_prints_when_busy(monkeypatch, capsys):
    monkeypatch.setattr(ea, "_port_in_use", lambda *a, **k: True)
    ea._warn_if_port_busy("start")
    out = capsys.readouterr().out
    assert "already listening" in out
    assert "8082" in out


def test_warn_if_port_busy_silent_when_free(monkeypatch, capsys):
    monkeypatch.setattr(ea, "_port_in_use", lambda *a, **k: False)
    ea._warn_if_port_busy("start")
    assert capsys.readouterr().out == ""


def test_cmd_start_warns_then_delegates(monkeypatch, capsys, tmp_path):
    """cmd_start emits the busy warning and still hands off to the service cmd."""
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"x")
    monkeypatch.setattr(ea, "_binary_and_gguf_or_fail", lambda: (tmp_path / "bin", gguf))
    monkeypatch.setattr(ea, "_port_in_use", lambda *a, **k: True)
    called = {}

    def _fake_service(b, g, sub, *e):
        called["sub"] = sub
        return 0

    monkeypatch.setattr(ea, "_service_cmd", _fake_service)
    rc = ea.cmd_start(_ns())
    assert rc == 0
    assert called["sub"] == "start"
    assert "already listening" in capsys.readouterr().out


class _ns:
    """Minimal argparse.Namespace stand-in."""
    def __getattr__(self, name):
        return None


@pytest.mark.parametrize("busy", [True, False])
def test_cmd_start_works_regardless_of_busy(monkeypatch, tmp_path, busy):
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"x")
    monkeypatch.setattr(ea, "_binary_and_gguf_or_fail", lambda: (tmp_path / "bin", gguf))
    monkeypatch.setattr(ea, "_port_in_use", lambda *a, **k: busy)
    monkeypatch.setattr(ea, "_service_cmd", lambda *a, **k: 0)
    assert ea.cmd_start(_ns()) == 0
