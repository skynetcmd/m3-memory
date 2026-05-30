"""Drift gate: the committed MCP_CATALOG.json must equal a fresh regeneration.

Complements tests/test_tool_count_drift.py. That test pins the *count* (the
manifest's `count` field and the prose "N tools" claims). This test pins the
*content*: the full committed manifest — every tool's domain, summary,
destructive flag, and args — must byte-equal what bin/gen_tool_manifest.py
produces right now from the live catalog.

Why both are needed
-------------------
The count gate stays GREEN when a change doesn't move the tool count — but a
tool's description or argument schema can change (or a new tool can replace an
old one) without changing the count, leaving the committed manifest silently
stale. That is the exact failure mode behind the 2026-05-30 inventory drift
(memory 2b7eb301): tools were added/edited and the generators were never
re-run. This test makes "forgot to regenerate" a red build, not a silent rot.

Determinism
-----------
gen_tool_manifest.build_manifest() is pure: it reads only mcp_tool_catalog.TOOLS
+ tool_domains, sorts tools by (domain, name), and the writer uses
sort_keys=True, indent=2 (see gen_tool_manifest module docstring). No
timestamps, no DB, no embedder/GPU path — so a fresh build is byte-reproducible
and this comparison has no flake surface. (The Markdown inventory generator
gen_mcp_inventory.py is deliberately NOT gated here: it imports memory_core and
touches the embedder/GPU path, which is not safely reproducible in a unit test.
MCP_CATALOG.json is the machine-readable source of truth; gating it is the
deterministic boundary.)

On failure: run `python bin/gen_tool_manifest.py` (and, for the human-readable
inventory, `python bin/gen_mcp_inventory.py`) and commit the regenerated files.
"""
from __future__ import annotations

import json
import os
import sys

# conftest.py already puts bin/ on sys.path; belt-and-suspenders for isolation.
_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import gen_tool_manifest

_MANIFEST = os.path.join(_ROOT, "docs", "tools", "MCP_CATALOG.json")


def _committed_manifest() -> dict:
    assert os.path.exists(_MANIFEST), (
        f"{_MANIFEST} missing — run `python bin/gen_tool_manifest.py`"
    )
    with open(_MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


def test_committed_manifest_equals_fresh_build():
    """The committed MCP_CATALOG.json must equal a fresh build_manifest().

    Catches description/arg/domain drift that the count gate cannot see. We
    compare the parsed objects (not raw bytes) so JSON-insignificant whitespace
    never causes a false failure — the writer already canonicalizes ordering, so
    a content change is the only thing that can make these differ.
    """
    committed = _committed_manifest()
    fresh = gen_tool_manifest.build_manifest()

    # The generated_note is static prose, not catalog-derived; compare it
    # explicitly first for a clear message, then drop it from the structural
    # diff so a wording tweak there doesn't masquerade as catalog drift.
    assert committed.get("generated_note") == fresh.get("generated_note"), (
        "generated_note text changed — update the committed manifest via "
        "`python bin/gen_tool_manifest.py`."
    )

    committed_body = {k: v for k, v in committed.items() if k != "generated_note"}
    fresh_body = {k: v for k, v in fresh.items() if k != "generated_note"}

    if committed_body != fresh_body:
        # Build a focused diff so the failure names the drifted tool(s) rather
        # than dumping the whole catalog.
        c_by_name = {t["name"]: t for t in committed_body.get("tools", [])}
        f_by_name = {t["name"]: t for t in fresh_body.get("tools", [])}
        added = sorted(set(f_by_name) - set(c_by_name))
        removed = sorted(set(c_by_name) - set(f_by_name))
        changed = sorted(
            n for n in (set(c_by_name) & set(f_by_name))
            if c_by_name[n] != f_by_name[n]
        )
        raise AssertionError(
            "MCP_CATALOG.json is stale vs the live catalog. Run "
            "`python bin/gen_tool_manifest.py` and commit. "
            f"count: committed={committed_body.get('count')} "
            f"fresh={fresh_body.get('count')}; "
            f"added={added} removed={removed} changed={changed}"
        )


def test_fresh_build_count_field_is_self_consistent():
    """build_manifest()'s `count` equals its own non-meta tool tally — guards the
    generator itself, independent of whatever is committed on disk."""
    fresh = gen_tool_manifest.build_manifest()
    non_meta = [t for t in fresh["tools"] if not t["name"].startswith("tools_")]
    assert fresh["count"] == len(non_meta), (
        f"build_manifest count={fresh['count']} but {len(non_meta)} non-meta "
        "tools in its own output — generator bug, not a stale-file issue."
    )
