"""Client-spawned processes get `pythonw.exe` on Windows — no console flash (#153).

Everything `_resolve_python_cmd` feeds -- MCP servers, capture hooks, the
statusline -- is launched by the agent CLIENT with pipes attached, never by a
human at a prompt. `python.exe` is a console-subsystem binary, so Windows
allocates a console for each and the user sees a window flash.

THE OBJECTION, and why it does not hold: "pythonw.exe has no stdout, and these
are stdio transports." That is true only with NO pipe attached. Measured
2026-09-13 with pipes -- which is always, here -- it is identical:

    python.exe   memory_bridge.py  -> MCP handshake replied: True
    pythonw.exe  memory_bridge.py  -> MCP handshake replied: True
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


def _as_windows(monkeypatch, present: "set[str]") -> None:
    """Make `_windowless` take its Windows branch, WITHOUT forcing os.name.

    `monkeypatch.setattr(os, "name", "nt")` looks like the obvious way and is a
    trap on POSIX: `pathlib.Path()` picks PosixPath vs WindowsPath from
    `os.name` AT CONSTRUCTION TIME, so every Path built while it is live becomes
    a WindowsPath and raises NotImplementedError. conftest.py documents this at
    length and actively RESTORES os.name before each report is rendered -- which
    silently undid the patch mid-test and made these tests fail on Linux and
    macOS while passing on Windows (where os.name is natively "nt", so the
    monkeypatch was a no-op).

    Patching the module's own lookups instead is what the rest of the suite
    already does (see test_cross_platform_config), and it works identically on
    every host.
    """
    monkeypatch.setattr(gc, "_is_windows", lambda: True)
    monkeypatch.setattr(gc.os.path, "isfile", lambda p: str(p).replace("\\", "/") in present)


_SCRIPTS = "C:/venv/Scripts"


@pytest.fixture
def win_scripts(monkeypatch):
    """A simulated Windows venv holding BOTH interpreters."""
    _as_windows(monkeypatch, {f"{_SCRIPTS}/python.exe", f"{_SCRIPTS}/pythonw.exe"})
    return _SCRIPTS


def test_console_interpreter_is_swapped_for_the_gui_one(win_scripts):
    got = gc._windowless(f"{win_scripts}/python.exe")
    assert os.path.basename(got).lower() == "pythonw.exe", got


def test_it_is_idempotent(win_scripts):
    """Applying it twice must not produce `pythonww.exe`."""
    once = gc._windowless(f"{win_scripts}/python.exe")
    assert gc._windowless(once) == once


def test_a_missing_sibling_leaves_the_interpreter_alone(monkeypatch):
    """Never invent a path. A venv without pythonw.exe must keep working."""
    _as_windows(monkeypatch, {f"{_SCRIPTS}/python.exe"})
    src = f"{_SCRIPTS}/python.exe"
    assert gc._windowless(src) == src


def test_posix_is_untouched(monkeypatch):
    """pythonw is a Windows concept; this must be inert elsewhere."""
    monkeypatch.setattr(gc, "_is_windows", lambda: False)
    assert gc._windowless("/usr/bin/python3") == "/usr/bin/python3"


def test_a_non_python_command_is_not_rewritten(win_scripts):
    assert gc._windowless(f"{win_scripts}/node.exe").endswith("node.exe")


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
