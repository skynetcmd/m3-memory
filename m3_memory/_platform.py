"""WMI-safe OS detection.

On Windows + CPython 3.14, ``platform.system()`` / ``platform.machine()`` route
through ``platform.uname()`` → a WMI query (``_win32_ver`` / ``_wmi_query``) that
can hang indefinitely on a slow or contended WMI service. That freezes any code
path calling them — installer, setup wizard, scheduled-task entrypoints, the
dashboard. ``os.name`` / ``sys.platform`` are module constants that give the same
OS distinction with no WMI, no stall.

Use ``os_name()`` anywhere the legacy code called ``platform.system()`` for a
Windows/Linux/Darwin comparison.
"""

from __future__ import annotations

import os
import sys


def os_name() -> str:
    """Return 'Windows', 'Darwin', or 'Linux' without touching WMI.

    Drop-in for ``platform.system()`` in OS-branch comparisons. Derived from
    ``os.name`` (nt/posix) and ``sys.platform`` (win32/darwin/linux*).
    """
    if os.name == "nt":
        return "Windows"
    if sys.platform == "darwin":
        return "Darwin"
    return "Linux"


def is_windows() -> bool:
    return os.name == "nt"


def hidden_window_kwargs() -> dict:
    """subprocess kwargs that hide the child's console window on Windows while
    KEEPING its stdout/stderr. No-op off Windows, so call sites stay portable.

        subprocess.run([...], **hidden_window_kwargs())

    Use this — not ``creationflags=CREATE_NO_WINDOW`` — for anything whose
    output matters. Measured 2026-09-13, emitting on both streams and exiting 7:

        python.exe + SW_HIDE, capture_output=True   -> stdout, stderr, rc intact
        python.exe + SW_HIDE, inherited handles     -> output still reaches the
                                                       parent's console
        CREATE_NO_WINDOW,     inherited handles     -> output LOST (see
                                                       bin/_task_runtime.py)

    That difference is why this exists as its own helper. ``no_window_kwargs``
    in ``bin/_task_runtime.py`` is the right tool for a *background* scheduled
    task that always captures; this is the right tool for a *foreground* step a
    user is watching. PR #154 nearly shipped the former into the installer,
    where a failing ``pip install`` would have printed "OS setup failed (code 1)"
    with the reason gone — a §3 silent failure.

    Why hiding is needed at all: a child spawned from a console-less parent gets
    its OWN console, which steals focus mid-keystroke. Five orphaned
    ``install_os.py`` consoles were found on one box 2026-09-13 (16 h old, 0.1 s
    CPU, every parent exited), each parked on a prompt behind a window nobody
    asked for.
    """
    if sys.platform != "win32":
        return {}
    import subprocess

    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": si}


def python_exe(windowless: bool = False) -> str:
    """Return a real Python interpreter path, never a console-script shim.

    Use this instead of ``sys.executable`` whenever spawning a child process,
    because ``sys.executable`` is NOT always an interpreter. Under a pip/pipx
    console script (``m3.exe``, ``mcp-memory.exe``) it is the SHIM, so

        subprocess.run([sys.executable, "script.py", "--flag"])

    re-enters the m3 CLI with a .py path as its subcommand. argparse rejects it
    ("invalid choice: 'script.py'"), the child exits 2, and — because the shim
    is the m3 CLI — it can also spawn stray ``m3.exe`` processes that outlive
    the run.

    That is not hypothetical: it silently truncated `m3 setup`. The dashboard
    boot-task step ran ``[sys.executable, install_schedules.py, ...]``, which
    under ``m3 setup`` failed, aborted the wizard before Step 5, and exited 1 —
    while ``python -m m3_memory.cli setup`` completed and exited 0. Same code,
    same venv, different launcher.

    Resolution order:
      1. ``sys.executable`` when it really is an interpreter (the common case:
         ``python -m ...``, and every ``bin/*.py`` script).
      2. The interpreter beside the shim — a console script always lives in the
         environment's ``Scripts``/``bin`` dir next to ``python``.
      3. ``sys.executable`` unchanged, so callers still get the old behaviour
         rather than an exception if neither check resolves.

    ``windowless=True`` (Windows only) prefers ``pythonw.exe`` in step 2, for a
    BACKGROUND spawn where a console flash is pure noise. It is opt-in, NOT the
    default.

    CORRECTED 2026-09-13 — the two claims this docstring used to make for that
    default were both wrong, and neither is the real reason:

    * "pythonw.exe has no stdout" is imprecise; it has none only when no handle
      is attached. Measured with a child printing to both streams and exiting 7:
      ``pythonw.exe`` + ``capture_output=True`` returned stdout, stderr and rc
      identically to ``python.exe``. (Consistent with the #153 note below.)
    * "every caller today is a foreground step whose output the user is reading"
      is false: of the 11 ``python_exe()`` spawns in installer.py and
      setup_wizard.py, 9 capture output and only 2 inherit.

    The default stands for a different reason: **this flag does not control the
    window.** It only reorders which binary is found beside a console-script
    shim, so when ``sys.executable`` is already a real interpreter — the common
    case — passing ``windowless=True`` changes NOTHING. Hiding the window is a
    property of the SPAWN, not the launcher: use ``hidden_window_kwargs()``
    above, which works regardless of which interpreter is resolved and keeps the
    pipes. Reach for ``windowless=True`` only when you genuinely want the
    GUI-subsystem binary (a detached background process that must never own a
    console), not as a flash fix.

    Not needed for stdio MCP servers -- `generate_configs._windowless` already
    resolves those (and the capture hooks and statusline) to pythonw.exe at
    registration time. NB the earlier claim here, that pythonw.exe would kill a
    stdio transport, was WRONG: it has no stdout only when no pipe is attached.
    Measured 2026-09-13 -- with pipes it serves MCP identically (#153).
    """
    exe = sys.executable or ""
    stem = os.path.splitext(os.path.basename(exe))[0].lower()
    # "python", "python3", "python3.14", "pythonw" -> a real interpreter.
    if stem.startswith("python"):
        return exe
    if exe:
        here = os.path.dirname(exe)
        if is_windows():
            # python.exe FIRST by default: every caller today is an installer or
            # wizard step that runs in the FOREGROUND and shows the child's
            # output to the user. pythonw.exe has no stdout, so defaulting to it
            # would silently swallow install diagnostics -- a worse bug than the
            # cosmetic flash, and a silent one.
            #
            # windowless=True flips the order for a genuinely BACKGROUND spawn,
            # where no one is reading stdout and a console flash is pure noise.
            names = ("pythonw.exe", "python.exe") if windowless else (
                "python.exe", "pythonw.exe")
        else:
            names = ("python3", "python")
        for name in names:
            cand = os.path.join(here, name)
            if os.path.isfile(cand):
                return cand
    return exe
