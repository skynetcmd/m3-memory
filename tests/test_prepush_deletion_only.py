"""A push made only of branch deletions publishes nothing, so the hook must pass it.

Hazard: with every ref a deletion there is no range to scan, and the manual-run
fallback would scan the checked-out branch -- blocking the deletion on content
it is not pushing.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_HOOK = _ROOT / ".githooks" / "pre-push"
_ZERO = "0" * 40


def _git_bash() -> "str | None":
    """The shell git itself runs hooks with.

    On Windows that is Git for Windows' bash, found next to `git --exec-path`;
    the `bash` on PATH may be WSL's, which sees /mnt/c paths and cannot follow a
    Windows worktree's gitdir.
    """
    if os.name != "nt":
        return shutil.which("bash")
    exec_path = subprocess.run(["git", "--exec-path"], capture_output=True, text=True).stdout.strip()
    if not exec_path:
        return None
    git_root = pathlib.Path(exec_path).parents[2]
    for cand in (git_root / "bin" / "bash.exe", git_root / "usr" / "bin" / "bash.exe"):
        if cand.is_file():
            return str(cand)
    return None


_BASH = _git_bash()
pytestmark = pytest.mark.skipif(_BASH is None, reason="git's bash not found")


def _run_hook(stdin: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        # Relative to cwd: a Windows path handed to bash loses its backslashes.
        [_BASH, ".githooks/pre-push", "private", "git@example.invalid:x/y.git"],
        input=stdin, capture_output=True, text=True, cwd=_ROOT, timeout=120,
    )


def test_deletion_only_push_passes_without_scanning():
    head = subprocess.run(["git", "-C", str(_ROOT), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    stdin = (f"(delete) {_ZERO} refs/heads/old-a {head}\n"
             f"(delete) {_ZERO} refs/heads/old-b {head}\n")
    r = _run_hook(stdin)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "deletion-only push" in r.stdout
    assert "1/3" not in r.stdout and "3/3" not in r.stdout, "no gate may run for a deletion"


def test_the_manual_fallback_is_still_reserved_for_an_empty_stdin():
    """The early exit must not swallow a mixed push: deletions plus real refs
    still produce ranges and reach the gates."""
    src = _HOOK.read_text(encoding="utf-8")
    assert 'if [ "$SAW_DELETE" -eq 1 ] && [ ${#RANGES[@]} -eq 0 ]; then' in src
