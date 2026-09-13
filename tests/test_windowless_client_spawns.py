"""Client-spawned processes get `pythonw.exe` on Windows — no console flash (#153).

Everything `_resolve_python_cmd` feeds -- MCP servers, capture hooks, the
statusline -- is launched by the agent CLIENT with pipes attached, never by a
human at a prompt. `python.exe` is a console-subsystem binary, so Windows
allocates a console for each and the user sees a window flash.

THE OBJECTION, and why it does not hold: "pythonw.exe has no stdout, and these
are stdio transports." That is true only with NO pipe attached. Measured
2026-09-13 with pipes -- which is always, here -- it is identical:

    python.exe   grok_bridge.py  -> MCP handshake replied: True
    pythonw.exe  grok_bridge.py  -> MCP handshake replied: True
    python.exe   session_start_capture_check.py -> rc=0, 97B stdout
    pythonw.exe  session_start_capture_check.py -> rc=0, 97B stdout (identical)

I closed #153 as won't-fix on that objection without measuring it. The
measurement took under a minute and disproved it (§12c: prefer the cheap
measurement over the plausible model).

Verified across the supported matrix by EXECUTION, not inference (§0.4):

    Windows  py3.14           generator emits pythonw.exe; MCP handshake rc=0
    Linux    py3.13.5         Debian 13 guest -- POSIX no-op holds, 5 passed
    macOS    py3.12/3.13/3.14 macOS 27.0 arm64 -- POSIX no-op holds on all
                              three, 5 passed on 3.12

The DB axis does not apply: this function is pure path manipulation and never
touches the seam (no dialect/backend/execute references in the module).
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import generate_configs as gc  # noqa: E402


@pytest.fixture
def win_scripts(tmp_path, monkeypatch):
    d = tmp_path / "Scripts"
    d.mkdir()
    for n in ("python.exe", "pythonw.exe"):
        (d / n).write_bytes(b"")
    monkeypatch.setattr(os, "name", "nt")
    return d


def test_console_interpreter_is_swapped_for_the_gui_one(win_scripts):
    got = gc._windowless(str(win_scripts / "python.exe"))
    assert os.path.basename(got).lower() == "pythonw.exe", got


def test_it_is_idempotent(win_scripts):
    """Applying it twice must not produce `pythonww.exe`."""
    once = gc._windowless(str(win_scripts / "python.exe"))
    assert gc._windowless(once) == once


def test_a_missing_sibling_leaves_the_interpreter_alone(tmp_path, monkeypatch):
    """Never invent a path. A venv without pythonw.exe must keep working."""
    d = tmp_path / "Scripts"
    d.mkdir()
    (d / "python.exe").write_bytes(b"")
    monkeypatch.setattr(os, "name", "nt")
    assert gc._windowless(str(d / "python.exe")) == str(d / "python.exe")


def test_posix_is_untouched(monkeypatch):
    """pythonw is a Windows concept; this must be inert elsewhere."""
    monkeypatch.setattr(os, "name", "posix")
    assert gc._windowless("/usr/bin/python3") == "/usr/bin/python3"


def test_a_non_python_command_is_not_rewritten(win_scripts):
    assert gc._windowless(str(win_scripts / "node.exe")).endswith("node.exe")


@pytest.mark.skipif(os.name != "nt", reason="Windows-only behaviour")
def test_pythonw_really_can_write_to_a_captured_pipe():
    """THE premise the won't-fix rested on, tested rather than assumed.

    If this ever fails, the flash fix must be reverted -- stdout IS the MCP
    transport. Skips cleanly when no pythonw.exe is available.
    """
    import subprocess

    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.isfile(pyw):
        pytest.skip("no pythonw.exe beside this interpreter")

    r = subprocess.run(
        [pyw, "-c", "import sys; sys.stdout.write('OK'); sys.stdout.flush()"],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout == "OK", (
        f"pythonw.exe could not write to a captured pipe (got {r.stdout!r}); "
        f"stdio MCP servers would be dead -- revert the windowless resolver"
    )
