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
    # Best-effort chmod on POSIX (no-op semantics on Windows).
    try:
        (hooks_dir / "pre-push").chmod(0o755)
    except OSError:
        pass
    print("Enabled shared git hooks (core.hooksPath -> .githooks). "
          "Pre-push drift + leakage gate is now active for this clone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
