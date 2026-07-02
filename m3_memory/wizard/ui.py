"""Pure output helpers for the setup wizard (color, say/ok/warn/err, progress).

Extracted verbatim from setup_wizard.py. None of these are ever monkeypatched
by tests, and none of them call anything that is — safe to live in a
submodule and be imported back into setup_wizard.py at module load time.
"""
from __future__ import annotations

import os
import sys


def _color(code: str, msg: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return msg
    return f"\033[{code}m{msg}\033[0m"

def _say(msg: str) -> None:
    print(f"{_color('36', '==>')} {msg}", flush=True)

def _ok(msg: str) -> None:
    print(_color("32", f"[OK] {msg}"), flush=True)

def _warn(msg: str) -> None:
    print(_color("33", f"[!] {msg}"), flush=True)

def _err(msg: str) -> None:
    print(_color("31", f"[X] {msg}"), file=sys.stderr, flush=True)

# Transient single-line progress for long, line-by-line sequences (per-package
# installs, per-section embeds). On a TTY each call REWRITES the same line
# (carriage-return + clear-to-end-of-line) so a 20-line "installing X / installed
# X" wall collapses to one self-updating status line. When stdout is NOT a TTY
# (piped, redirected, non-interactive SSH, CI) it degrades to a normal newline
# print so logs stay complete and grep-able. Call once more with done=True (or
# follow with a plain _ok) to commit a final newline so the next output starts
# on its own line.
_PROGRESS_ACTIVE = False

def _progress(msg: str, *, done: bool = False) -> None:
    global _PROGRESS_ACTIVE
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        # Non-interactive: every step on its own line (full, parseable log).
        print(f"    {msg}", flush=True)
        return
    # Interactive: rewrite the current line. \r returns to col 0; \033[K clears
    # to end of line so a shorter message doesn't leave stale trailing chars.
    end = "\n" if done else ""
    sys.stdout.write(f"\r\033[K    {msg}{end}")
    sys.stdout.flush()
    _PROGRESS_ACTIVE = not done

def _progress_done() -> None:
    """Commit a newline if a transient progress line is still open (TTY only)."""
    global _PROGRESS_ACTIVE
    if _PROGRESS_ACTIVE and sys.stdout.isatty():
        sys.stdout.write("\n")
        sys.stdout.flush()
    _PROGRESS_ACTIVE = False
