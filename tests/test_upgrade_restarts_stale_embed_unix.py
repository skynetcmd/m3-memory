"""Two upgrade paths a real deploy found and no test reached.

1. Linux: setup re-registers the embed service with `enable --now`, which does
   not restart a running unit, so after a core upgrade the server kept the old
   binary and setup only printed the restart command. Setup now restarts it and
   re-checks; it warns only when the server is still on the old core.
2. `~/.local/bin/m3 upgrade` from a shell without that directory on PATH said
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


def _unix(monkeypatch, stale_sequence, rcs=(0, 0)):
    """Fake a Unix host whose stale list changes between calls, and record the
    embedder commands setup runs."""
    monkeypatch.setattr(setup_wizard, "_on_windows", lambda: False)
    seq = iter(stale_sequence)
    monkeypatch.setattr(embedder_admin, "stale_embed_servers", lambda: next(seq))
    calls: list[str] = []
    codes = iter(rcs)

    def fake_run(cmd, **_k):
        calls.append(cmd[-1])
        return SimpleNamespace(returncode=next(codes), stdout="", stderr="unit failed to start")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_a_stale_unix_embed_server_is_restarted_and_rechecked(monkeypatch, capsys):
    calls = _unix(monkeypatch, [_STALE, []])
    setup_wizard._restart_stale_embed_server()
    out = capsys.readouterr().out
    assert calls == ["stop", "start"]
    assert "restarted on the new native core (was pid 4242)" in out
    assert "still runs the previous native core" not in out


def test_a_restart_that_leaves_the_old_core_running_is_reported(monkeypatch, capsys):
    calls = _unix(monkeypatch, [_STALE, _STALE])
    setup_wizard._restart_stale_embed_server()
    out = capsys.readouterr().out
    assert calls == ["stop", "start"]
    assert "still runs the previous native core" in out
    assert "restarted on the new native core" not in out


def test_a_failed_restart_names_the_command_and_its_error(monkeypatch, capsys):
    calls = _unix(monkeypatch, [_STALE, _STALE], rcs=(0, 1))
    setup_wizard._restart_stale_embed_server()
    out = capsys.readouterr().out
    assert calls == ["stop", "start"]
    assert "`m3 embedder start` exited 1: unit failed to start" in out
    assert "still runs the previous native core" in out


def test_nothing_stale_runs_nothing(monkeypatch, capsys):
    calls = _unix(monkeypatch, [[]])
    setup_wizard._restart_stale_embed_server()
    assert calls == [] and capsys.readouterr().out == ""


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
