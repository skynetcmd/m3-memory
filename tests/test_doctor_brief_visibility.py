"""Every doctor probe must report ONE LINE in brief mode -- never nothing.

`brief` is the DEFAULT (`--verbose` opts into detail), so a probe that prints
only when `not brief` is invisible to essentially every user. Two probes added
2026-07-25 shipped exactly that way: they worked, passed their unit tests, and
produced no output in `m3 doctor` -- the command a user actually runs.

A check nobody sees is worth nothing, and worse, it reads as coverage.
"""

import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBES = os.path.join(REPO, "bin", "doctor")

# Probes whose healthy state legitimately prints nothing in brief mode because
# another line already covers them. Keep this EMPTY unless there is a real
# reason -- an entry here is a check the user cannot see.
EXEMPT: set[str] = set()


def _probe_files():
    for name in sorted(os.listdir(PROBES)):
        if name.endswith("_probe.py") and name[:-3] not in EXEMPT:
            yield name, os.path.join(PROBES, name)


def test_no_probe_gates_its_healthy_line_behind_not_brief():
    """The specific bug: `if not brief: print(...OK...)` in the success path."""
    offenders = []
    for name, path in _probe_files():
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        # A `if not brief:` whose body's only print looks like a status line.
        for m in re.finditer(r"if not brief:\s*\n((?:\s+.*\n)+?)(?=\s*(?:return|if |for |\Z))",
                             src):
            block = m.group(1)
            if "print(" in block and re.search(r"OK|healthy|verified", block):
                offenders.append(f"{name}: {block.strip().splitlines()[0][:70]}")
    assert not offenders, (
        "probe hides its healthy status behind --verbose; brief means ONE "
        "LINE, not silence:\n  " + "\n  ".join(offenders)
    )


def test_every_probe_run_prints_something_on_the_success_path():
    """Structural: each probe's run() must contain at least one unconditional
    print reachable in brief mode."""
    missing = []
    for name, path in _probe_files():
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        if "def run(" not in src:
            continue
        run_src = src[src.index("def run("):]
        # Strip `if not brief:` blocks, then look for a remaining print.
        stripped = re.sub(r"if not brief:\s*\n(?:\s+.*\n)+?(?=\s*(?:return|if |\Z))",
                          "", run_src)
        if "print(" not in stripped:
            missing.append(name)
    assert not missing, (
        "probe(s) print nothing outside a `not brief` guard, so they are "
        f"invisible in default `m3 doctor`: {missing}"
    )
