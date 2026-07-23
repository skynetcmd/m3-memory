#!/usr/bin/env python3
"""Detect stray control characters in text files — the PowerShell backtick trap.

In PowerShell the BACKTICK is the escape character, not the backslash. So
markdown inline-code written through a double-quoted PowerShell string is
silently mangled: the opening backtick escapes the word's first letter into a
control byte, and the closing backtick is consumed too.

    `bytemuck`  ->  \\x08ytemuck    (`b = backspace)
    `ring`      ->  \\r + "ing"     (`r = carriage return)
    `ndarray`   ->  \\n + "darray"  (`n = newline)
    `r2d2`      ->  \\r + "2d2"

Only words starting with b, n, r, t, a, f, v, or 0 are affected — exactly
PowerShell's single-letter escapes — which is why `sqlx`, `sha2`, `proptest`
and `maturin` all survive untouched in the same document. That selectivity
makes the damage easy to miss in review: most of the file looks perfect.

Real incident (2026-07-15, repaired 2026-07-23): a design plan was authored
through such a string. Six crate names lost their leading letter and the file
was committed twice with the damage before anyone noticed, because a stray
\\x08 renders as nothing in most viewers and \\r/\\n just look like line breaks.

Fix when writing files from PowerShell: use a SINGLE-quoted here-string
(``@'...'@``), which does no escape processing — never ``@"..."@``. Better
still, write the file with Python or an editor rather than shell string
interpolation.

Usage:
    python bin/check_control_chars.py [paths...]   # explicit paths
    python bin/check_control_chars.py --staged     # git-staged text files

Exit 0 = clean, 1 = corruption found (prints file:line:col and a repair hint).
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess

# Control bytes that must never appear in a text file. TAB (\x09), LF (\x0a)
# and CR (\x0d) are legitimate; everything else in the C0 range is not. \x0b
# (vertical tab) and \x0c (form feed) are included because PowerShell's `v and
# `f produce them and neither has a legitimate use in our docs.
_BAD = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# PowerShell escape -> the letter it consumed, for the repair hint.
_PS_ESCAPES = {
    0x08: ("`b", "b"),
    0x0b: ("`v", "v"),
    0x0c: ("`f", "f"),
    0x07: ("`a", "a"),
    0x00: ("`0", "0"),
}

_TEXT_EXT = {".md", ".txt", ".rst", ".py", ".json", ".yaml", ".yml", ".toml",
             ".cfg", ".ini", ".sh", ".ps1", ".sql"}


def _is_text(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in _TEXT_EXT


def _staged_files() -> "list[str]":
    """Text files staged for commit (added/copied/modified/renamed)."""
    try:
        out = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [p for p in out.splitlines() if p and _is_text(p) and os.path.exists(p)]


def scan(path: str) -> "list[tuple[int, int, int]]":
    """Return [(line, col, byte)] for every stray control char in *path*."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return []
    hits = []
    for m in _BAD.finditer(data):
        off = m.start()
        line = data.count(b"\n", 0, off) + 1
        col = off - (data.rfind(b"\n", 0, off) + 1) + 1
        hits.append((line, col, data[off]))
    return hits


def _hint(byte: int, path: str, line: int) -> str:
    if byte in _PS_ESCAPES:
        seq, letter = _PS_ESCAPES[byte]
        return (f"looks like PowerShell consumed {seq!r} — a word starting with "
                f"{letter!r} lost its first letter (e.g. `{letter}ytemuck` -> "
                f"\\x{byte:02x}ytemuck). Restore the letter and the backticks.")
    return f"unexpected control byte \\x{byte:02x}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="check_control_chars.py",
        description="Fail on stray control characters in text files.",
    )
    ap.add_argument("paths", nargs="*", help="Files to scan.")
    ap.add_argument("--staged", action="store_true",
                    help="Scan the git-staged text files instead of PATHS.")
    args = ap.parse_args(argv)

    targets = _staged_files() if args.staged else [p for p in args.paths if _is_text(p)]
    if not targets:
        return 0

    bad = 0
    for path in targets:
        for line, col, byte in scan(path):
            bad += 1
            print(f"{path}:{line}:{col}: stray control char \\x{byte:02x} — "
                  f"{_hint(byte, path, line)}")

    if bad:
        print()
        print(f"[control-chars] {bad} stray control character(s) found.")
        print("  Cause: markdown backticks passed through a DOUBLE-quoted PowerShell")
        print("         string. PowerShell's escape char is ` (backtick), not \\.")
        print("  Fix:   restore the mangled words, and write files with a SINGLE-quoted")
        print("         here-string @'...'@ (no escape processing) or with Python.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
