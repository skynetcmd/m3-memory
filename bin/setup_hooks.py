#!/usr/bin/env python3
"""Enable the repo's shared git hooks for this clone.

Points git at the tracked .githooks/ directory so the pre-push drift +
leakage gate runs for every agent and human, regardless of which AGENTS
instruction file they read. Run once per clone:

    python bin/setup_hooks.py

Idempotent. Cross-platform (the pre-push hook is bash; on Windows it runs
under Git-for-Windows' bundled bash, which `git push` invokes automatically).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    hooks_dir = _ROOT / ".githooks"
    if not (hooks_dir / "pre-push").exists():
        print("error: .githooks/pre-push not found", file=sys.stderr)
        return 1
    rc = subprocess.run(["git", "config", "core.hooksPath", ".githooks"],
                        cwd=_ROOT).returncode
    if rc != 0:
        return rc
    # Best-effort chmod on POSIX (no-op semantics on Windows). pre-commit is
    # optional-by-presence so an older checkout without it still sets up fine.
    for name in ("pre-push", "pre-commit"):
        try:
            (hooks_dir / name).chmod(0o755)
        except OSError:
            pass
    enabled = ["pre-push drift + leakage gate"]
    if (hooks_dir / "pre-commit").exists():
        enabled.append("pre-commit control-char scan")
    print("Enabled shared git hooks (core.hooksPath -> .githooks). "
          "Active for this clone: " + "; ".join(enabled) + ".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
