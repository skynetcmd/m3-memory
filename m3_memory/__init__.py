"""
M3 Memory — Local-first agentic memory layer for MCP agents.

100+ MCP tools · Hybrid search (FTS5 + vector + MMR) · Contradiction detection
Bitemporal history · GDPR Article 17/20 · Cross-device sync · 100% local
"""

import os as _os
import sys as _sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version


def _resolve_version() -> str:
    """The version of the m3-memory package THIS module belongs to.

    ``importlib.metadata`` searches ``sys.path``, whose first entry is the
    working directory. Anything run from a source checkout therefore reads that
    tree's ``*.egg-info`` / ``*.dist-info`` before the installed distribution —
    so the same interpreter reports different versions depending on where it was
    launched. That is not hypothetical: a stale ``m3_memory.egg-info`` in a
    checkout made the doctor's entrypoint probe invent three "stale launcher"
    failures and advise uninstalling a working entrypoint (fixed 2026-08-11,
    9d10760c — but the shadowing itself lives here).

    Editable installs add a second, quieter divergence: pip stamps the version
    into ``dist-info`` at INSTALL time and never rewrites it on a source edit,
    so a long-lived ``pip install -e .`` reports whatever the version was when
    it was created — 22 releases stale on this box.

    Resolution order, most authoritative first:

      1. ``pyproject.toml`` beside this package. Present only in a checkout, and
         there it IS the source of truth (the repo's own convention — see
         bin/sync_manifest_versions.py, which derives every manifest from it).
         Reading it makes a checkout self-consistent no matter how old the
         editable install's metadata is.
      2. Installed distribution metadata — the correct answer for a real wheel
         install, where no pyproject.toml sits beside the package.
      3. ``0.0.0+local`` when neither exists.
    """
    here = _os.path.dirname(_os.path.abspath(__file__))
    pyproject = _os.path.join(_os.path.dirname(here), "pyproject.toml")
    if _os.path.isfile(pyproject):
        try:
            if _sys.version_info >= (3, 11):
                import tomllib
                with open(pyproject, "rb") as fh:
                    v = tomllib.load(fh).get("project", {}).get("version")
            else:  # pragma: no cover — 3.10 and older
                import re
                with open(pyproject, encoding="utf-8") as fh:
                    m = re.search(r'^version\s*=\s*"([^"]+)"', fh.read(), re.M)
                v = m.group(1) if m else None
            if v:
                return str(v)
        except Exception:  # noqa: BLE001 — a bad pyproject falls through, never raises
            pass

    try:
        return _pkg_version("m3-memory")
    except PackageNotFoundError:
        return "0.0.0+local"


__version__ = _resolve_version()

__author__ = "skynetcmd, Gemini CLI"
__license__ = "Apache-2.0"
