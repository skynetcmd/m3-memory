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
