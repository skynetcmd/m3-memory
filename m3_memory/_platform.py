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


def python_exe() -> str:
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
    """
    exe = sys.executable or ""
    stem = os.path.splitext(os.path.basename(exe))[0].lower()
    # "python", "python3", "python3.14", "pythonw" -> a real interpreter.
    if stem.startswith("python"):
        return exe
    if exe:
        here = os.path.dirname(exe)
        names = ("python.exe", "pythonw.exe") if is_windows() else ("python3", "python")
        for name in names:
            cand = os.path.join(here, name)
            if os.path.isfile(cand):
                return cand
    return exe
