"""The one way m3 runs pip inside its own environment.

Hazard: pipx 1.17+ creates application venvs without pip (it runs pip from a
shared environment), so ``python -m pip`` fails with "No module named pip" and
every install step that relies on it fails after it — the native core, its
embed-server binary and the dashboard extras. ``ensurepip`` installs the pip
bundled with the interpreter into the venv.
"""
from __future__ import annotations

import subprocess
import sys

_READY: dict = {}


class PipUnavailable(RuntimeError):
    """pip is missing for this interpreter and could not be bootstrapped."""


def _has_pip(python: str) -> bool:
    try:
        r = subprocess.run([python, "-c", "import pip"], capture_output=True, text=True,
                           timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def pip_command(python: str = sys.executable) -> list:
    """argv prefix that runs pip for ``python``, installing pip first if missing."""
    if _READY.get(python):
        return [python, "-m", "pip"]
    if not _has_pip(python):
        try:
            r = subprocess.run([python, "-m", "ensurepip", "--default-pip"],
                               capture_output=True, text=True, timeout=300)
            detail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
        except (OSError, subprocess.SubprocessError) as e:
            detail = [f"{type(e).__name__}: {e}"]
        if not _has_pip(python):
            raise PipUnavailable(
                f"pip is not available in this environment ({python}) and "
                f"ensurepip could not install it ({detail[0]}). "
                f"Fix: {python} -m ensurepip --default-pip"
            )
        print(f"[m3] pip was missing from this environment; installed it with ensurepip.")
    _READY[python] = True
    return [python, "-m", "pip"]
