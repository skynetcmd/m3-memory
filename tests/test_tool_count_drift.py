"""Regression test: the hardcoded "N tools" claims in the public docs must
match the live catalog count.

The catalog count is name-prefix-derived and easy to drift out of sync with
the prose: adding a ToolSpec bumps the real number, but the README / comparison
tables / myths page quote a hardcoded "N tools" that nobody remembers to
update. This test pins both ends:

  1. The `count` field in the generated manifest (docs/tools/MCP_CATALOG.json)
     must equal the independently-computed catalog count
     (`len([t for t in TOOLS if not t.name.startswith("tools_")])`).
  2. Every catalog-total "N tools" claim in the documented files must equal
     that same count, so any drift at any site fails the build.

If this test fails after a deliberate catalog change, regenerate the manifest
(`python bin/gen_tool_manifest.py`) and update the doc numbers below.

Mirrors the import + sys.path setup of tests/test_lazy_tool_loading.py.
"""
from __future__ import annotations

import json
import os
import re
import sys

# conftest.py already puts bin/ on sys.path. Belt-and-suspenders so this
# file is also importable in isolation:
_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import mcp_tool_catalog

_MANIFEST = os.path.join(_ROOT, "docs", "tools", "MCP_CATALOG.json")

# Doc files that quote a catalog-total "N tools" number. Grep these before
# editing the list; a new public doc that quotes the count belongs here.
_DOC_FILES = [
    os.path.join(_ROOT, "README.md"),
    os.path.join(_ROOT, "docs", "COMPARISON.md"),
    os.path.join(_ROOT, "docs", "MYTHS_AND_FACTS.md"),
    os.path.join(_ROOT, "docs", "tools", "files_memory.md"),
]

# Domain-/phase-subcount claims that are intentionally NOT the catalog total
# (e.g. "All 21 tools" = the files-memory domain, "(5 tools)" = one module).
# Matched verbatim and skipped so they don't masquerade as drift. Keep this
# list tight: anything that isn't a recognized subcount must equal the total.
_SUBCOUNT_EXCEPTIONS = (
    "26 MCP tools",        # files_memory.md title — the files-memory DOMAIN count
    "All 26 tools",        # files_memory.md — the files-memory domain itself
    "26-tool files-memory",  # README — the files-memory domain as a sub-layer
    "(5 tools)",           # files_memory.md — files_corpus_* module
)

# Section headings like "## Phase 1 tools — walker" are not counts — the
# number is a phase ordinal. Strip these before scanning.
_NONCOUNT_RE = re.compile(r"\bPhase \d+ tools\b")

# Catches "96 tools" AND "96 MCP tools" — the optional "MCP " qualifier was a
# blind spot that let a stale "96 MCP tools" in README slip past a catalog bump.
_TOOLS_RE = re.compile(r"\b(\d+) (?:MCP )?tools\b")


def _computed_count() -> int:
    """The number the docs quote: non-meta catalog tools."""
    return len([t for t in mcp_tool_catalog.TOOLS if not t.name.startswith("tools_")])


def test_manifest_count_matches_catalog():
    """The manifest's `count` field must equal the live catalog count.

    Guards against a stale committed manifest — if someone changes the
    catalog but forgets to re-run bin/gen_tool_manifest.py, this fails.
    """
    assert os.path.exists(_MANIFEST), (
        f"{_MANIFEST} missing — run `python bin/gen_tool_manifest.py`"
    )
    manifest = json.load(open(_MANIFEST, encoding="utf-8"))
    computed = _computed_count()
    assert manifest["count"] == computed, (
        f"MCP_CATALOG.json count={manifest['count']} but the live catalog has "
        f"{computed} non-meta tools. Re-run `python bin/gen_tool_manifest.py`."
    )


def test_manifest_tool_records_are_well_formed():
    """Every manifest tool record carries the documented fields and the
    universal `database` arg is never present in the per-tool args."""
    manifest = json.load(open(_MANIFEST, encoding="utf-8"))
    for t in manifest["tools"]:
        assert {"name", "domain", "summary", "destructive", "args"} <= set(t), t
        assert isinstance(t["destructive"], bool), t
        for a in t["args"]:
            assert {"name", "type", "required"} <= set(a), a
            assert a["name"] != "database", (
                f"{t['name']}: universal 'database' arg leaked into manifest"
            )
        assert len(t["summary"]) <= 100, (t["name"], t["summary"])


def _exact_tool_count_claims(text: str) -> list[int]:
    """Every exact '`N` tools' number left in `text` after stripping the
    legitimate domain/module subcounts.

    Under the "100+ tools" policy this should be empty for every gated doc:
    the catalog total is never spelled out in prose, and the only exact
    "N tools" phrases allowed are the subcount exceptions (which are stripped
    here). A non-empty result means someone reintroduced a hardcodable total.
    """
    cleaned = _NONCOUNT_RE.sub("", text)
    for exc in _SUBCOUNT_EXCEPTIONS:
        cleaned = cleaned.replace(exc, "")
    return [int(m.group(1)) for m in _TOOLS_RE.finditer(cleaned)]


def test_prose_does_not_hardcode_catalog_total():
    """No gated doc may spell out an exact catalog-total 'N tools' number.

    The catalog total has exactly one exact home (the generated manifest);
    public prose says "100+ tools". Domain/module subcounts
    (_SUBCOUNT_EXCEPTIONS) are the only exact "N tools" phrases permitted.
    Anything else is a drift hazard and must be rephrased "100+ tools" (or
    whitelisted as a subcount). The manifest↔catalog exactness is still
    pinned by test_manifest_count_matches_catalog above.
    """
    offenders: list[str] = []
    for path in _DOC_FILES:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for n in _exact_tool_count_claims(text):
            offenders.append(f"{os.path.relpath(path, _ROOT)}: '{n} tools'")

    assert not offenders, (
        "Hardcoded exact tool-count(s) found in prose — rephrase as "
        f"'100+ tools' or whitelist as a subcount: {offenders}"
    )
