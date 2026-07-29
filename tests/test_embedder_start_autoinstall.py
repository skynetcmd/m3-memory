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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import m3_memory.embedder_admin as ea  # noqa: E402


def _patch_common(monkeypatch, service_calls):
    monkeypatch.setattr(ea, "_binary_and_gguf_or_fail", lambda: (Path("/fake/bin"), Path("/fake.gguf")))
    monkeypatch.setattr(ea, "_warn_if_port_busy", lambda phase: None)
    monkeypatch.setattr(ea, "_locate_gguf_or_explain", lambda: Path("/fake.gguf"))
    monkeypatch.setattr(ea, "_gguf_size_bytes", lambda p: 1024 * 1024)
    monkeypatch.setattr(ea, "_server_binary", lambda: Path("/fake/bin"))

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
