"""`m3 upgrade` — one OS-agnostic command, and it must stay a LAUNCHER.

The upgrade replaces the very package the CLI is running from. On Windows that
is a file-locking failure, not a theoretical one: pip uninstalls before it
installs, Windows holds an open `.exe` against deletion, and the upgrade deletes
the package and then fails -- leaving NO m3 installed.

So `bin/m3_upgrade.py` deliberately does NOT import `m3_memory`, and this
command must only RELAUNCH it in a fresh process. Doing the work inline would
reintroduce exactly the hazard the standalone script exists to avoid.

Why the command exists at all: the script shipped in the payload but nothing
told anyone it was there. `m3 --help`, README and HOW-TO-UPGRADE all pointed at
bare `pipx upgrade m3-memory` -- which, against a pip install, exits 0 having
upgraded NOTHING and reads as success. A correct tool nobody can find does not
prevent that.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from m3_memory import cli  # noqa: E402


def test_the_subcommand_is_discoverable_in_help():
    """It must appear in `m3 --help`. An undiscoverable tool IS the bug this
    command exists to fix -- bin/m3_upgrade.py shipped for a release with
    nothing pointing at it.

    Runs the CLI in a subprocess rather than introspecting a parser factory
    (there isn't one; the parser is built inline in main()). That also tests
    what the user actually sees, not a proxy for it."""
    import subprocess

    out = subprocess.run(
        [sys.executable, "-m", "m3_memory.cli", "--help"],
        capture_output=True, text=True, timeout=120, cwd=_ROOT,
    )
    text = (out.stdout or "") + (out.stderr or "")

    # Ask the CLI to RUN it, not whether the word appears. A substring check
    # passed even when the subcommand was renamed to `upgradeXX` -- because
    # "upgrade" also appears in `m3 stop`'s help ("run this before `pipx
    # upgrade`"). A guard that cannot fail is worse than none (§12c).
    probe = subprocess.run(
        [sys.executable, "-m", "m3_memory.cli", "upgrade", "--help"],
        capture_output=True, text=True, timeout=120, cwd=_ROOT,
    )
    assert probe.returncode == 0, (
        f"`m3 upgrade --help` exited {probe.returncode}; the subcommand is not "
        f"registered, so users cannot find it -- exactly how the standalone "
        f"script went unused for a release. {probe.stderr[:400]}"
    )
    assert "--dry-run" in (probe.stdout or ""), probe.stdout[:300]
    assert "upgrade" in text, "`m3 --help` does not mention the subcommand"


def test_help_does_not_send_users_to_a_bare_pipx_upgrade():
    """`pipx upgrade m3-memory` against a PIP install exits 0 having upgraded
    NOTHING. Any help text that recommends it without qualification recreates
    the failure `m3 upgrade` exists to prevent."""
    import re
    import subprocess

    out = subprocess.run(
        [sys.executable, "-m", "m3_memory.cli", "upgrade", "--help"],
        capture_output=True, text=True, timeout=120, cwd=_ROOT,
    )
    text = (out.stdout or "") + (out.stderr or "")
    assert not re.search(r"use `?pipx upgrade", text), (
        f"the upgrade command's own help points at bare pipx upgrade: {text}"
    )


def test_upgrade_does_not_import_the_package_it_replaces():
    """THE safety property. `bin/m3_upgrade.py` must not import m3_memory, or
    the package cannot be swapped underneath it."""
    import ast
    import pathlib

    src = pathlib.Path(_ROOT, "bin", "m3_upgrade.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bad += [a.name for a in node.names if a.name.split(".")[0] == "m3_memory"]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "m3_memory":
                bad.append(node.module)
    assert not bad, (
        f"bin/m3_upgrade.py imports {bad} -- it runs WHILE that package is being "
        f"replaced; on Windows this is a file-locking failure that can leave no "
        f"m3 installed"
    )


def test_the_cli_handler_only_launches(monkeypatch):
    """The handler must shell out, never do the upgrade inline."""
    import argparse

    seen = {}

    class _Res:
        returncode = 0

    def fake_run(cmd, *a, **k):
        seen["cmd"] = list(cmd)
        return _Res()

    monkeypatch.setattr("subprocess.run", fake_run)
    rc = cli._cmd_upgrade(argparse.Namespace(dry_run=True, yes=False, skip_stop=False))

    assert rc == 0
    assert seen.get("cmd"), "the handler did not launch a subprocess"
    assert seen["cmd"][-1] == "--dry-run"
    assert seen["cmd"][1].endswith("m3_upgrade.py"), seen["cmd"]


@pytest.mark.parametrize(
    "flag,expected",
    [("dry_run", "--dry-run"), ("yes", "--yes"), ("skip_stop", "--skip-stop")],
)
def test_every_flag_reaches_the_script(flag, expected, monkeypatch):
    """A flag accepted by the CLI but dropped before the script is a silent
    no-op -- the user asked for --dry-run and got a real upgrade."""
    import argparse

    seen = {}

    class _Res:
        returncode = 0

    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, *a, **k: (seen.__setitem__("cmd", list(cmd)), _Res())[1],
    )
    ns = argparse.Namespace(dry_run=False, yes=False, skip_stop=False)
    setattr(ns, flag, True)
    cli._cmd_upgrade(ns)
    assert expected in seen["cmd"], f"{flag} did not reach the script: {seen['cmd']}"


def test_a_missing_helper_fails_loud_and_names_the_path(monkeypatch, capsys):
    """Never silently no-op: the user thinks they upgraded."""
    import argparse

    monkeypatch.setattr("m3_memory.installer.bin_dir", lambda: None)
    rc = cli._cmd_upgrade(argparse.Namespace(dry_run=True, yes=False, skip_stop=False))
    err = capsys.readouterr().err
    assert rc == 1
    assert "missing" in err.lower()
    assert "m3 stop" in err, "the error must state the manual fallback"


def test_the_interpreter_is_never_the_console_shim(monkeypatch, tmp_path):
    """`sys.executable` is `m3.exe` under a pipx/pip install. Handing that to
    subprocess re-enters this CLI with a .py path as its subcommand; argparse
    rejects it and the child exits 2."""
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    for name in ("python.exe", "python3", "python"):
        (scripts / name).write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(scripts / "m3.exe"))
    resolved = cli._interpreter()
    assert "m3.exe" not in resolved
    assert os.path.basename(resolved).lower().startswith("python")


def test_a_real_interpreter_is_returned_unchanged(monkeypatch, tmp_path):
    exe = tmp_path / "python.exe"
    monkeypatch.setattr(sys, "executable", str(exe))
    assert cli._interpreter() == str(exe)
