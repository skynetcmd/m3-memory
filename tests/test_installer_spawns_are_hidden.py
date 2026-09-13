"""No install/setup subprocess may flash a console window on Windows.

A child spawned from a console-less parent gets its OWN console, which steals
focus mid-keystroke. Five orphaned install_os.py consoles were found on one box
2026-09-13 (16h old, 0.1s CPU, every parent exited), each parked on a prompt
behind a window nobody asked for.

The fix must use STARTUPINFO/SW_HIDE, never CREATE_NO_WINDOW: the latter also
suppresses INHERITED stdout/stderr, so a failing `pip install` would print
"OS setup failed (code 1)" with the reason gone (§3 silent failure; see
bin/_task_runtime.py::no_window_kwargs for the measurement, and PR #154 which
nearly shipped exactly that).
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_FILES = ["m3_memory/installer.py", "m3_memory/setup_wizard.py"]

# Commands that own a console on Windows. POSIX-only ones (sudo, kill) cannot
# flash and are deliberately out of scope.
_WINDOWED = re.compile(
    r'powershell|schtasks|tasklist|taskkill|setx|"claude"|sys\.executable'
    r'|"git"|python_exe\(\)')


def _iter_run_calls(src: str):
    """Yield (lineno, call_text) for every subprocess.run( … ) with balanced parens."""
    for m in re.finditer(r"subprocess\.run\(", src):
        seg = src[m.start():m.start() + 600]
        depth = 0
        for i, ch in enumerate(seg):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    yield src[:m.start()].count("\n") + 1, seg[:i + 1]
                    break


@pytest.mark.parametrize("relpath", _FILES)
def test_every_windowed_spawn_is_hidden(relpath):
    src = (_ROOT / relpath).read_text(encoding="utf-8")
    exposed = [
        (ln, " ".join(call.split())[:90])
        for ln, call in _iter_run_calls(src)
        if _WINDOWED.search(call) and "hidden_window_kwargs" not in call
    ]
    assert not exposed, (
        f"{relpath}: spawn(s) would flash a console window on Windows.\n"
        "Add **hidden_window_kwargs() (m3_memory._platform):\n  "
        + "\n  ".join(f"line {ln}: {c}" for ln, c in exposed))


@pytest.mark.parametrize("relpath", _FILES)
def test_no_create_no_window_on_the_install_path(relpath):
    """CREATE_NO_WINDOW here would silently swallow installer diagnostics."""
    src = (_ROOT / relpath).read_text(encoding="utf-8")
    for ln, call in _iter_run_calls(src):
        assert "CREATE_NO_WINDOW" not in call, (
            f"{relpath}:{ln} uses CREATE_NO_WINDOW on a user-visible install "
            "step; it suppresses inherited stdout/stderr. Use "
            "hidden_window_kwargs() (STARTUPINFO/SW_HIDE) instead.")


def test_helper_hides_window_and_keeps_output():
    """MEASURED, not asserted: the helper must preserve stdout, stderr and the
    exit code. A guard that cannot demonstrate the property is worthless (§12c)."""
    from m3_memory._platform import hidden_window_kwargs

    child = (
        "import sys; print('OUT'); print('ERR', file=sys.stderr); sys.exit(7)")
    proc = subprocess.run([sys.executable, "-c", child],
                          capture_output=True, text=True,
                          **hidden_window_kwargs())
    assert proc.returncode == 7, "exit code must survive"
    assert "OUT" in (proc.stdout or ""), "stdout must survive"
    assert "ERR" in (proc.stderr or ""), "stderr must survive"


def test_helper_is_a_noop_off_windows():
    from m3_memory._platform import hidden_window_kwargs
    kw = hidden_window_kwargs()
    if sys.platform == "win32":
        assert "startupinfo" in kw
    else:
        assert kw == {}, "must stay portable: empty dict off Windows"


def test_guard_can_actually_fail():
    """Plant a violation and watch the detector trip (§12c: 'a guard must be
    able to FAIL'). Without this, a regex that silently stops matching reads as
    coverage while providing none."""
    planted = 'subprocess.run(["powershell", "-Command", ps], check=False)'
    hits = [c for _, c in _iter_run_calls(planted)
            if _WINDOWED.search(c) and "hidden_window_kwargs" not in c]
    assert hits, "the detector failed to flag an obviously-exposed spawn"
