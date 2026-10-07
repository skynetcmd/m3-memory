"""An embed server left on a replaced core, and the launcher lookup.

1. Linux: setup re-registers the embed service with `enable --now`, which does
   not restart a running unit, so after a core upgrade the server kept the old
   binary and setup only printed the restart command. Setup now restarts it
   through embedder_admin.restart_stale_embed_service and re-checks.
2. Windows: the service runs elevated; an unattended upgrade cannot restart
   it. `m3 doctor --fix` at a console restarts it under one UAC prompt, and
   never prompts without a person present.
3. `~/.local/bin/m3 upgrade` from a shell without that directory on PATH said
   "m3 is not on PATH. Install it first" about a working install.
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
from types import SimpleNamespace

from m3_memory import embedder_admin, setup_wizard

_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
_SPEC = importlib.util.spec_from_file_location("m3_upgrade_launcher", _BIN / "m3_upgrade.py")
assert _SPEC and _SPEC.loader
m3u = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(m3u)

_STALE = [{"pid": 4242, "started": 1.0, "installed": 2.0}]


def _host(monkeypatch, stale_sequence, *, windows=False, rcs=(0, 0)):
    """A host with an embed-server binary whose stale list changes between
    calls; records the service verbs run and fakes their exit codes."""
    monkeypatch.setattr(setup_wizard, "_on_windows", lambda: windows)
    monkeypatch.setattr(embedder_admin, "_is_windows", lambda: windows)
    seq = iter(stale_sequence)
    monkeypatch.setattr(embedder_admin, "stale_embed_servers", lambda: next(seq))
    monkeypatch.setattr(embedder_admin, "_server_binary",
                        lambda: pathlib.Path("/opt/core/m3-embed-server"))
    monkeypatch.setattr(embedder_admin, "_ensure_executable", lambda _p: None)
    monkeypatch.setattr(embedder_admin, "_find_bundled_gguf", lambda: None)
    calls: list[str] = []
    codes = iter(rcs)

    def fake_run(cmd, **_k):
        calls.append(cmd[-1])
        return SimpleNamespace(returncode=next(codes), stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


# ── setup on Linux / macOS ────────────────────────────────────────────────────

def test_a_stale_unix_embed_server_is_restarted_and_rechecked(monkeypatch, capsys):
    calls = _host(monkeypatch, [_STALE, _STALE, []])
    setup_wizard._restart_stale_embed_server()
    out = capsys.readouterr().out
    assert calls == ["stop", "start"]
    assert "restarted on the new native core (was pid 4242)" in out
    assert "still runs the previous native core" not in out


def test_a_restart_that_leaves_the_old_core_running_is_reported(monkeypatch, capsys):
    calls = _host(monkeypatch, [_STALE, _STALE, _STALE])
    setup_wizard._restart_stale_embed_server()
    out = capsys.readouterr().out
    assert calls == ["stop", "start"]
    assert "still runs the previous native core" in out
    assert "restarted on the new native core" not in out


def test_a_failed_restart_names_the_command(monkeypatch, capsys):
    calls = _host(monkeypatch, [_STALE, _STALE, _STALE], rcs=(0, 1))
    setup_wizard._restart_stale_embed_server()
    out = capsys.readouterr().out
    assert calls == ["stop", "start"]
    assert "`m3-embed-server start` exited 1" in out
    assert "still runs the previous native core" in out


def test_nothing_stale_runs_nothing(monkeypatch, capsys):
    calls = _host(monkeypatch, [[]])
    setup_wizard._restart_stale_embed_server()
    assert calls == [] and capsys.readouterr().out == ""


# ── the shared helper on Windows (what `m3 doctor --fix` calls) ──────────────

def test_windows_without_a_person_raises_no_prompt(monkeypatch):
    _host(monkeypatch, [_STALE], windows=True)

    class _Boom:
        def __init__(self):
            raise AssertionError("must not build an elevation batch")

    import m3_memory.elevate as elevate
    monkeypatch.setattr(elevate, "ElevationBatch", _Boom)
    res = embedder_admin.restart_stale_embed_service(allow_elevation=False)
    assert res == {"outcome": "needs-admin", "pids": [4242], "detail": ""}


class _FakeBatch:
    result: "dict | None" = None

    def __init__(self):
        self.actions: list = []

    def add(self, label, argv):
        self.actions.append((label, argv))

    def run(self):
        return self.result


def test_windows_restart_runs_stop_and_start_under_one_prompt(monkeypatch):
    _host(monkeypatch, [_STALE, []], windows=True)
    seen: list = []

    class _Batch(_FakeBatch):
        def run(self):
            seen.extend(argv[-1] for _l, argv in self.actions)
            return {label: {"rc": 0, "output": ""} for label, _a in self.actions}

    import m3_memory.elevate as elevate
    monkeypatch.setattr(elevate, "ElevationBatch", _Batch)
    res = embedder_admin.restart_stale_embed_service(allow_elevation=True)
    assert seen == ["stop", "start"]
    assert res["outcome"] == "restarted" and res["pids"] == [4242]


def test_a_declined_prompt_is_reported_as_declined(monkeypatch):
    _host(monkeypatch, [_STALE], windows=True)
    import m3_memory.elevate as elevate
    monkeypatch.setattr(elevate, "ElevationBatch", _FakeBatch)  # run() -> None
    res = embedder_admin.restart_stale_embed_service(allow_elevation=True)
    assert res["outcome"] == "declined"


# ── the launcher lookup ───────────────────────────────────────────────────────

def test_the_launcher_beside_this_interpreter_is_found_without_path(monkeypatch, tmp_path):
    name = "m3.exe" if sys.platform == "win32" else "m3"
    launcher = tmp_path / name
    launcher.write_text("", encoding="utf-8")
    monkeypatch.setattr(m3u.shutil, "which", lambda _n: None)
    monkeypatch.setattr(m3u.sys, "executable", str(tmp_path / "python"))
    assert m3u.find_m3_launcher() == str(launcher)


def test_no_launcher_anywhere_says_where_it_looked(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(m3u.shutil, "which", lambda _n: None)
    monkeypatch.setattr(m3u.sys, "executable", str(tmp_path / "python"))
    assert m3u.find_m3_launcher() is None
    assert m3u.main(["--dry-run"]) == 1
    out = capsys.readouterr().out
    assert "not found on PATH or beside this interpreter" in out and str(tmp_path) in out


# ── `m3 embedder install-gpu`'s closing line ─────────────────────────────────

def _install_gpu(monkeypatch, stale):
    import argparse

    from m3_memory import rust_core_install
    monkeypatch.setattr(rust_core_install, "install_rust_core", lambda **k: 0)
    monkeypatch.setattr(embedder_admin, "stale_embed_servers", lambda: stale)
    return lambda **kw: embedder_admin.cmd_install_gpu(argparse.Namespace(**kw))


def test_install_gpu_from_setup_leaves_the_report_to_setup(monkeypatch, capsys):
    _install_gpu(monkeypatch, _STALE)(from_setup=True)
    assert capsys.readouterr().out == ""


def test_install_gpu_names_a_restart_only_when_a_server_is_stale(monkeypatch, capsys):
    _install_gpu(monkeypatch, _STALE)()
    assert "embed server (pid 4242) still runs the previous core" in capsys.readouterr().out
    _install_gpu(monkeypatch, [])()
    assert capsys.readouterr().out.strip() == "[OK] m3-core-rs installed"
