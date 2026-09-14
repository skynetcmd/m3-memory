"""The documented generator chain must match the real one.

Defect E's second half. `check_tool_catalog_drift.py` runs three generators;
two more (`gen_capability_matrix.py`, `gen_features_json.py`) exist and are
caught only by the full suite. A tool-count change tripped both, because the
count claim lives in more places than the drift checker knows about.

The doc listing them is prose, so it can go stale the moment a sixth generator
lands. This test makes the list load-bearing.
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DOC = _ROOT / "docs" / "AGENT_INSTRUCTIONS.md"

# Every generator that must be run by hand after a catalog / bin/ change.
EXPECTED = [
    "gen_tool_manifest.py",
    "gen_mcp_inventory.py",
    "gen_tool_inventory.py",
    "gen_capability_matrix.py",
    "gen_features_json.py",
]


def test_every_generator_exists():
    for name in EXPECTED:
        assert (_ROOT / "bin" / name).is_file(), f"bin/{name} is missing"


def test_the_doc_names_every_generator():
    text = _DOC.read_text(encoding="utf-8")
    missing = [n for n in EXPECTED if n not in text]
    assert not missing, (
        f"AGENT_INSTRUCTIONS.md does not name {missing}. An undocumented "
        f"generator is one nobody runs until the full suite fails."
    )


def test_no_undocumented_generator_has_appeared():
    """A sixth generator must be documented, not discovered by a red suite."""
    found = {p.name for p in (_ROOT / "bin").glob("gen_*.py")}
    doc = _DOC.read_text(encoding="utf-8")
    # Only generators that feed the tool catalog / count claims are in scope;
    # the others (badges, star history) do not gate a push.
    in_scope = {n for n in found if n in set(EXPECTED)} | {
        n for n in found if "tool" in n or "capability" in n or "features" in n
    }
    undocumented = sorted(n for n in in_scope if n not in doc)
    assert not undocumented, (
        f"new generator(s) {undocumented} are not in the pre-push chain doc"
    )
