"""`m3 embedder start` must auto-install the service when it isn't registered.

Regression for the P0 where `start` on a never-registered service printed generic
start-troubleshooting (systemd/nohup) instead of the real fix. Now, when the
binary exists but the service isn't registered, `cmd_start` delegates to
`cmd_install` (locate GGUF + register + start) so `m3 embedder start` just works.

Everything that would touch the OS service manager, the network (:8082), or load
a GGUF is mocked — this is a pure control-flow test.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import m3_memory.embedder_admin as ea  # noqa: E402


def _patch_common(monkeypatch, service_calls):
    monkeypatch.setattr(ea, "_binary_and_gguf_or_fail", lambda: (Path("/fake/bin"), Path("/fake.gguf")))
    monkeypatch.setattr(ea, "_warn_if_port_busy", lambda phase: None)
    monkeypatch.setattr(ea, "_locate_gguf_or_explain", lambda: Path("/fake.gguf"))
    monkeypatch.setattr(ea, "_gguf_size_bytes", lambda p: 1024 * 1024)
    monkeypatch.setattr(ea, "_server_binary", lambda: Path("/fake/bin"))
    # Since 2026-09-27 both cmd_start and cmd_install CONFIRM the service is up
    # rather than trusting `start`'s exit code (0 from launchd/systemd/SCM means
    # only that the request was accepted). In this fake world the start succeeds,
    # so the probe says so; without it these control-flow tests would fail on the
    # verification step for reasons that have nothing to do with the flow they
    # pin. The dead-service path is covered in test_embedder_start_verification.
    monkeypatch.setattr(ea, "_service_reports_running", lambda *a, **k: True)

    def fake_service_cmd(binary, gguf, action, *extra):
        service_calls.append((action, list(extra)))
        return 0

    monkeypatch.setattr(ea, "_service_cmd", fake_service_cmd)


def test_start_autoinstalls_when_service_not_registered(monkeypatch):
    calls: list = []
    _patch_common(monkeypatch, calls)
    monkeypatch.setattr(ea, "_service_reports_installed", lambda b, g: False)

    # The real `start` subparser has NO --concurrency; cmd_install must tolerate
    # that via its getattr default (2) rather than raising AttributeError.
    rc = ea.cmd_start(argparse.Namespace(embedder_cmd="start"))

    assert rc == 0
    # cmd_install both registers AND starts, so we expect install then start.
    assert calls == [("install", ["--concurrency", "2"]), ("start", [])]


def test_start_uses_normal_path_when_registered(monkeypatch):
    calls: list = []
    _patch_common(monkeypatch, calls)
    monkeypatch.setattr(ea, "_service_reports_installed", lambda b, g: True)

    rc = ea.cmd_start(argparse.Namespace(embedder_cmd="start"))

    assert rc == 0
    assert calls == [("start", [])]  # no install; straight to start


def test_install_on_a_running_current_service_runs_nothing(monkeypatch, capsys):
    """Setup calls install on every run; a service already registered, running
    and on the current binary must not be re-installed or re-started (that
    printed "nothing to do" twice around a port-in-use notice about itself)."""
    calls: list = []
    _patch_common(monkeypatch, calls)
    monkeypatch.setattr(ea, "_service_reports_installed", lambda b, g: True)
    monkeypatch.setattr(ea, "_service_binary_is_stale", lambda b: False)
    monkeypatch.setattr(ea, "_warn_if_port_busy",
                        lambda phase: pytest.fail("no port notice for our own service"))
    assert ea.cmd_install(argparse.Namespace(concurrency=2)) == 0
    assert calls == []
    assert "running on port" in capsys.readouterr().out


def test_install_on_a_stale_running_service_still_restarts_it(monkeypatch):
    calls: list = []
    _patch_common(monkeypatch, calls)
    monkeypatch.setattr(ea, "_service_reports_installed", lambda b, g: True)
    monkeypatch.setattr(ea, "_service_binary_is_stale", lambda b: True)
    monkeypatch.setattr(ea, "_service_cmd", lambda b, g, action, *e: calls.append(action) or (1 if action == "install" else 0))
    assert ea.cmd_install(argparse.Namespace(concurrency=2)) == 0
    assert calls == ["install", "stop", "start"]


@pytest.fixture(autouse=True)
def _quiet_service_cmd_follows_the_fake(monkeypatch):
    """Route the quiet variant through whatever _service_cmd a test fakes."""
    monkeypatch.setattr(ea, "_service_cmd_quiet", lambda *a, **k: ea._service_cmd(*a, **k))


@pytest.mark.parametrize("registered", [True, False])
def test_reregistering_a_known_service_is_quiet_first_install_is_not(monkeypatch, registered):
    used = []
    _patch_common(monkeypatch, [])
    monkeypatch.setattr(ea, "_service_reports_installed", lambda b, g: registered)
    monkeypatch.setattr(ea, "_service_reports_running", lambda *a, **k: False)
    monkeypatch.setattr(ea, "_verify_started_or_explain", lambda *a, **k: 0)
    monkeypatch.setattr(ea, "_service_cmd", lambda b, g, s, *e: used.append(("loud", s)) or 0)
    monkeypatch.setattr(ea, "_service_cmd_quiet", lambda b, g, s, *e: used.append(("quiet", s)) or 0)
    assert ea.cmd_install(argparse.Namespace(concurrency=2)) == 0
    mode = "quiet" if registered else "loud"
    assert used == [(mode, "install"), (mode, "start")]
