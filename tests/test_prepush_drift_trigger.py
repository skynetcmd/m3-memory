"""The pre-push drift gate must TRIGGER on every source the inventory covers.

`bin/gen_tool_inventory.py` emits a `docs/tools/<name>.md` page for every script
in `bin/` and `scripts/`, each stamped with a hash of its source, and
`tests/test_generated_docs_fresh.py` fails when a source moves without its page.
The pre-push gate is the cheap place to catch that -- but it only runs
`check_tool_catalog_drift.py` when a CHANGED path matches its trigger regex.

That regex used to name five specific files (the catalog + the three
generators). Every other script in `bin/` -- the large majority -- therefore
skipped the gate entirely, and the staleness surfaced only at the end of the
~5.5-minute full suite. It cost five failed runs in one session (2026-09-12).

These tests pin the trigger's SHAPE, not a file list: any `.py`/`.sh` under
`bin/` or `scripts/` must arm the gate, and paths the generators do not read
must still not. The second half matters as much as the first -- a trigger
widened to everything would run a multi-second regeneration on every push and
train people to reach for --no-verify (DESIGN_PHILOSOPHIES section 3).
"""
from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_HOOK = _ROOT / ".githooks" / "pre-push"


def _trigger_regex() -> str:
    """The drift gate's path regex, read from the hook itself."""
    src = _HOOK.read_text(encoding="utf-8")
    marker = "1/3 tool-catalog drift gate (conditional)"
    idx = src.index(marker)
    lines = src[idx:].splitlines()
    for n, ln in enumerate(lines):
        if "grep -qE" in ln:
            m = re.match(r"\s*'(.+)'\s*$", lines[n + 1])
            assert m, f"unparseable trigger line: {lines[n + 1]!r}"
            return m.group(1)
    raise AssertionError("drift-gate trigger regex not found")


def _fires(path: str) -> bool:
    """Mirror the hook: does this changed path arm the drift gate?

    Delegated to `grep -E` rather than `re`, because the hook is POSIX ERE and
    Python's dialect is not the same language -- asserting against `re` would
    pin a regex the hook never runs.
    """
    proc = subprocess.run(
        ["grep", "-qE", _trigger_regex()],
        input=path + "\n", text=True, capture_output=True,
    )
    return proc.returncode == 0


@pytest.mark.parametrize(
    "path",
    [
        "bin/mcp_tool_catalog.py",        # the original five...
        "bin/gen_tool_inventory.py",
        "bin/m3_notification_waiter.py",  # ...and the ones that used to slip
        "bin/install_schedules.py",
        "bin/setup_memory.py",
        "bin/m3_upgrade.py",
        "bin/ai-audit.sh",                # the generator reads *.sh too
        "scripts/inventory_graph.py",
        "docs/tools/agent_protocol.md",
        "README.md",
    ],
)
def test_inventoried_sources_arm_the_gate(path):
    assert _fires(path), (
        f"{path} has a generated docs/tools/ page but does not arm the "
        f"pre-push drift gate -- staleness would surface only at the end of "
        f"the full suite"
    )


@pytest.mark.parametrize(
    "path",
    [
        "m3_memory/cli.py",          # package code: no docs/tools/ page
        "tests/test_notification_waiter.py",
        "bin/notes.txt",             # not a script
        "docs/DESIGN_PHILOSOPHIES.md",
        ".github/workflows/ci.yml",
    ],
)
def test_unrelated_paths_do_not_arm_the_gate(path):
    assert not _fires(path), (
        f"{path} arms the drift gate but no generator reads it -- a gate that "
        f"fires on unrelated work is the false alarm that trains people to "
        f"bypass it"
    )


def test_trigger_covers_bin_wholesale_not_a_named_list():
    """A hand-maintained list of filenames silently stops covering each new
    script someone adds. The trigger must match the DIRECTORY."""
    rx = _trigger_regex()
    assert r"^bin/.*" in rx, (
        "the trigger must match bin/ wholesale; a named file list has to be "
        "extended by hand and fails silently when it is not"
    )


def test_every_bin_script_actually_arms_the_gate():
    """The real invariant, checked against the tree rather than a sample: no
    script under bin/ or scripts/ may be invisible to the gate."""
    missed = []
    for d in ("bin", "scripts"):
        base = _ROOT / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")) + sorted(base.rglob("*.sh")):
            rel = p.relative_to(_ROOT).as_posix()
            if not _fires(rel):
                missed.append(rel)
    assert not missed, f"scripts invisible to the drift gate: {missed}"
