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
    """The notice must name the port AND the consequence.

    The wording changed on 2026-09-27: it used to say an existing listener "may
    already be running ... which is idempotent", which made a real bind conflict
    read as benign — a stray process held :8082, the registered service could
    never bind, and the installer still reported success. Assert the meaning
    (port + the bind failure mode) rather than a phrase.
    """
    monkeypatch.setattr(ea, "_port_in_use", lambda *a, **k: True)
    ea._warn_if_port_busy("start")
    out = capsys.readouterr().out
    assert "already in use" in out
    assert "8082" in out
    assert "not be able to bind" in out


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
    # Inject the branch collaborator too: _service_reports_installed SHELLS OUT
    # to the binary, so with a fake path it raises OSError -> False -> cmd_start
    # takes the auto-install branch and re-resolves the REAL binary. That passes
    # only on a machine that happens to have m3-embed-server installed; CI has
    # none, so it failed there ("binary not found") while green locally (§3 —
    # a test that passes only because a live local service exists is not
    # hermetic).
    monkeypatch.setattr(ea, "_service_reports_installed", lambda *a, **k: True)
    # Since 2026-09-27 cmd_start VERIFIES the outcome instead of returning the
    # service manager's exit code: on launchd/systemd/SCM a 0 from `start` means
    # only that the request was accepted, so a daemon that dies on a port
    # conflict also exits 0. The fake therefore has to say the service actually
    # came up — otherwise this test asserts the very "success without evidence"
    # the fix removed. The delegation + warning it exists to check are unchanged.
    monkeypatch.setattr(ea, "_service_reports_running", lambda *a, **k: True)
    called = {}

    def _fake_service(b, g, sub, *e):
        called["sub"] = sub
        return 0

    monkeypatch.setattr(ea, "_service_cmd", _fake_service)
    rc = ea.cmd_start(_ns())
    assert rc == 0
    assert called["sub"] == "start"
    assert "already in use" in capsys.readouterr().out


class _ns:
    """Minimal argparse.Namespace stand-in."""
    def __getattr__(self, name):
        return None


@pytest.mark.parametrize("busy", [True, False])
def test_cmd_start_works_regardless_of_busy(monkeypatch, tmp_path, busy):
    """A busy port must not BLOCK the start path — the service manager owns that.

    What a busy port must no longer do is imply success: see
    test_dead_service_is_never_reported_as_running in
    test_embedder_start_verification.py for the other side of this contract.
    """
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"x")
    monkeypatch.setattr(ea, "_binary_and_gguf_or_fail", lambda: (tmp_path / "bin", gguf))
    monkeypatch.setattr(ea, "_port_in_use", lambda *a, **k: busy)
    monkeypatch.setattr(ea, "_service_cmd", lambda *a, **k: 0)
    # See the note in test_cmd_start_warns_then_delegates: without this the
    # fake binary makes cmd_start branch into cmd_install and hit the host.
    monkeypatch.setattr(ea, "_service_reports_installed", lambda *a, **k: True)
    # The service really comes up; cmd_start now confirms that rather than
    # trusting `start`'s exit code.
    monkeypatch.setattr(ea, "_service_reports_running", lambda *a, **k: True)
    assert ea.cmd_start(_ns()) == 0


@pytest.fixture(autouse=True)
def _quiet_service_cmd_follows_the_fake(monkeypatch):
    """Route the quiet variant through whatever _service_cmd a test fakes."""
    monkeypatch.setattr(ea, "_service_cmd_quiet", lambda *a, **k: ea._service_cmd(*a, **k))
