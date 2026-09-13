"""`python_exe()` must not silently swallow output to avoid a console flash.

Windows has two interpreters: `python.exe` (console subsystem — allocates a
console, so a background spawn FLASHES a window) and `pythonw.exe` (GUI
subsystem — no console, and **no stdout**).

Neither is universally right, which is why this is a parameter and not a
reordered default:

  * every current caller is an installer/wizard step running in the FOREGROUND
    whose output the user is reading, so `pythonw.exe` would swallow install
    diagnostics silently — worse than the cosmetic flash, and harder to notice;
  * a genuinely background spawn has no reader, so the flash is pure noise.

A CORRECTION lives here too. This file originally said `pythonw.exe` must
never be applied to stdio MCP servers, because they speak the protocol over
stdout. That is wrong: `pythonw.exe` has no stdout only when NO pipe is
attached. Measured 2026-09-13 with pipes -- which is always, for a
client-spawned server -- it writes to stdout identically and completes a full
MCP handshake. #153 is fixed on that basis; see
tests/test_windowless_client_spawns.py.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from m3_memory import _platform  # noqa: E402


@pytest.fixture
def fake_shim(tmp_path, monkeypatch):
    """Pretend we were launched by the `m3.exe` console-script shim, with both
    interpreters present beside it -- the only situation where the preference
    order is consulted at all."""
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    for name in ("python.exe", "pythonw.exe"):
        (scripts / name).write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(scripts / "m3.exe"))
    monkeypatch.setattr(_platform, "is_windows", lambda: True)
    return scripts


def test_default_prefers_the_console_interpreter(fake_shim):
    """Foreground callers must keep a usable stdout."""
    assert os.path.basename(_platform.python_exe()).lower() == "python.exe"


def test_windowless_prefers_the_gui_interpreter(fake_shim):
    assert os.path.basename(
        _platform.python_exe(windowless=True)
    ).lower() == "pythonw.exe"


def test_windowless_is_opt_in(fake_shim):
    """A caller that does not think about this gets the SAFE behaviour (output
    preserved), not the quiet one."""
    import inspect

    sig = inspect.signature(_platform.python_exe)
    assert sig.parameters["windowless"].default is False
    assert _platform.python_exe() == _platform.python_exe(windowless=False)


def test_a_real_interpreter_is_returned_unchanged(monkeypatch, tmp_path):
    """Step 1 short-circuits: when sys.executable IS an interpreter we must not
    swap it, or `windowless` would silently relaunch a different binary."""
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    assert _platform.python_exe(windowless=True) == str(tmp_path / "python.exe")


def test_posix_is_unaffected(monkeypatch, tmp_path):
    """pythonw is a Windows concept; the flag must be inert elsewhere."""
    scripts = tmp_path / "bin"
    scripts.mkdir()
    (scripts / "python3").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(scripts / "m3"))
    monkeypatch.setattr(_platform, "is_windows", lambda: False)
    assert _platform.python_exe(windowless=True) == _platform.python_exe()


def test_a_registered_mcp_interpreter_can_serve_stdio():
    """Whatever interpreter the MCP registrations name, it must be able to
    speak the stdio transport.

    This test previously asserted the OPPOSITE -- that no registration may use
    `pythonw.exe`, on the reasoning that "pythonw has no stdout". That
    reasoning was wrong, and I closed #153 as won't-fix on it without checking.
    Measured 2026-09-13: with a pipe attached (which is always, for a
    client-spawned server) pythonw.exe writes to stdout exactly like
    python.exe, and completes a full MCP handshake. `sys.stdout` there is a
    normal TextIOWrapper, not None; it is None only when NO pipe is attached.

    So the invariant is not "which binary" -- it is "can it serve". Asserting
    the binary name pinned a belief; this asserts the behaviour.
    """
    import json
    import pathlib
    import subprocess

    cfg = pathlib.Path.home() / ".claude.json"
    if not cfg.exists():
        pytest.skip("no ~/.claude.json on this machine")
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except Exception:
        pytest.skip("~/.claude.json is not readable JSON")

    interpreters = set()

    def walk(o):
        if isinstance(o, dict):
            for key, val in o.items():
                if key == "mcpServers" and isinstance(val, dict):
                    for spec in val.values():
                        cmd = (spec or {}).get("command") or ""
                        if "python" in str(cmd).lower():
                            interpreters.add(str(cmd))
                else:
                    walk(val)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    if not interpreters:
        pytest.skip("no python-based MCP servers registered here")

    for exe in sorted(interpreters):
        if not os.path.isfile(exe):
            continue
        r = subprocess.run(
            [exe, "-c", "import sys; sys.stdout.write('OK'); sys.stdout.flush()"],
            capture_output=True, text=True, timeout=60,
        )
        assert r.returncode == 0 and r.stdout == "OK", (
            f"{exe} cannot write to a captured pipe (rc={r.returncode}, "
            f"stdout={r.stdout!r}) -- every stdio MCP server registered with it "
            f"is dead"
        )
