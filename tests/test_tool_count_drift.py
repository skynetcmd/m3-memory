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
    # MCP registry manifests — these carry a public description string that must
    # follow the same "100+ tools" policy. They previously drifted to stale
    # exact counts (66 and 25) because nothing tested them.
    os.path.join(_ROOT, "server.json"),
    os.path.join(_ROOT, "mcp-server.json"),
    # User-facing plugin surfaces. These ship to every `/plugin install` and were
    # NOT gated, which is exactly why both help files sat at a stale "51 MCP
    # tools" for many releases while the real catalog passed 100. Anything a user
    # reads must follow the same "100+ tools" policy as the rest of the docs.
    os.path.join(_ROOT, "commands", "help.md"),
    os.path.join(_ROOT, "skills", "m3-guide", "SKILL.md"),
    os.path.join(_ROOT, ".antigravity-plugin", "skills", "m3-help", "SKILL.md"),
    os.path.join(_ROOT, ".antigravity-plugin", "skills", "m3-guide", "SKILL.md"),
]

# Files whose version string must equal the pyproject [project] version, so a
# release bump can't leave a manifest advertising a stale version (server.json
# said 2026.4.24.5 and mcp-server.json said 2026.04 while pyproject was newer;
# the plugin.json manifests drifted 6 releases behind — 2026.7.13.0 vs 2026.7.19.5
# — because release bumps updated pyproject but forgot these, and NOTHING caught
# it. The marketplace serves these directly from main, so a stale version.json
# ships to every user's `/plugin install`).
_VERSIONED_MANIFESTS = [
    os.path.join(_ROOT, "server.json"),
    os.path.join(_ROOT, "mcp-server.json"),
    os.path.join(_ROOT, ".claude-plugin", "plugin.json"),
    os.path.join(_ROOT, ".antigravity-plugin", "plugin.json"),
]

# The plugin marketplace manifests (published to every user). Their descriptions
# must NOT quote an exact "N MCP tools" — policy is "100+ MCP tools" (an exact
# count drifts on every catalog change and there is no generator syncing them).
_PLUGIN_MANIFESTS = [
    os.path.join(_ROOT, ".claude-plugin", "plugin.json"),
    os.path.join(_ROOT, ".claude-plugin", "marketplace.json"),
    os.path.join(_ROOT, ".antigravity-plugin", "plugin.json"),
    os.path.join(_ROOT, ".antigravity-plugin", "marketplace.json"),
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
    with open(_MANIFEST, encoding="utf-8") as _f:
        manifest = json.load(_f)
    computed = _computed_count()
    assert manifest["count"] == computed, (
        f"MCP_CATALOG.json count={manifest['count']} but the live catalog has "
        f"{computed} non-meta tools. Re-run `python bin/gen_tool_manifest.py`."
    )


def test_manifest_tool_records_are_well_formed():
    """Every manifest tool record carries the documented fields and the
    universal `database` arg is never present in the per-tool args."""
    with open(_MANIFEST, encoding="utf-8") as _f:
        manifest = json.load(_f)
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


def _pyproject_version() -> str:
    import tomllib
    with open(os.path.join(_ROOT, "pyproject.toml"), "rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def test_registry_manifests_match_pyproject_version():
    """server.json / mcp-server.json version strings must equal the pyproject
    version — a release bump must not leave a manifest advertising a stale one.

    server.json also carries a packages[].version; check every "version" value.
    """
    expected = _pyproject_version()
    mismatches: list[str] = []
    for path in _VERSIONED_MANIFESTS:
        with open(path, encoding="utf-8") as _f:
            data = json.load(_f)
        rel = os.path.relpath(path, _ROOT)
        if str(data.get("version", expected)) != expected:
            mismatches.append(f"{rel}: version={data.get('version')!r} != {expected!r}")
        for pkg in data.get("packages", []):
            if str(pkg.get("version", expected)) != expected:
                mismatches.append(
                    f"{rel}: packages[].version={pkg.get('version')!r} != {expected!r}"
                )
    assert not mismatches, (
        "MCP registry manifest version drift (bump these on release): "
        + "; ".join(mismatches)
    )


def test_all_manifests_synced_to_pyproject_version():
    """The authoritative single-source check: bin/sync_manifest_versions.py --check
    must pass, i.e. EVERY version-bearing manifest equals pyproject's version. A
    release bump that edits pyproject but forgets to run the sync fails HERE,
    loudly, instead of shipping a stale manifest to the marketplace.

    This subsumes test_registry_manifests_match_pyproject_version (kept for its
    targeted message) — both must stay green."""
    import subprocess
    script = os.path.join(_ROOT, "bin", "sync_manifest_versions.py")
    r = subprocess.run([sys.executable, script, "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, (
        "manifest version drift — run `python bin/sync_manifest_versions.py`:\n"
        + r.stdout + r.stderr
    )


def test_plugin_manifests_use_no_exact_tool_count():
    """Plugin marketplace manifests (published to every user) must say
    "100+ MCP tools", never an exact "N MCP tools" — an exact count silently
    drifts on every catalog change (there is no generator syncing them) and is
    what shipped a stale "101 MCP tools" while the catalog had 108."""
    offenders: list[str] = []
    exact_re = re.compile(r"\b\d+ MCP tools\b")
    for path in _PLUGIN_MANIFESTS:
        with open(path, encoding="utf-8") as _f:
            text = _f.read()
        for m in exact_re.finditer(text):
            offenders.append(f"{os.path.relpath(path, _ROOT)}: {m.group(0)!r}")
    assert not offenders, (
        "plugin manifests must say '100+ MCP tools', not an exact count: "
        + "; ".join(offenders)
    )


# The m3-guide skill is INLINED into both plugin bundles rather than referenced,
# because plugin users have no repo checkout to follow a link into. That buys
# offline/standalone correctness at the cost of a second copy — and this repo has
# already shipped two bugs from exactly that shape (an un-fixed duplicate of the
# embed-server health probe, and a stale "51 MCP tools" that survived because the
# help files were not gated). Pin the copies byte-for-byte so they cannot drift.
_GUIDE_SKILL_COPIES = [
    os.path.join(_ROOT, "skills", "m3-guide", "SKILL.md"),
    os.path.join(_ROOT, ".antigravity-plugin", "skills", "m3-guide", "SKILL.md"),
]


def test_m3_guide_skill_copies_are_identical():
    """Both plugin bundles must ship byte-identical m3-guide skills."""
    contents = {}
    for path in _GUIDE_SKILL_COPIES:
        assert os.path.isfile(path), f"missing m3-guide skill copy: {path}"
        with open(path, encoding="utf-8") as fh:
            contents[path] = fh.read()

    first, *rest = list(contents)
    for other in rest:
        assert contents[first] == contents[other], (
            "m3-guide SKILL.md copies have drifted — they are inlined in both "
            "plugin bundles and must stay byte-identical. Sync them: "
            f"{os.path.relpath(first, _ROOT)} vs {os.path.relpath(other, _ROOT)}"
        )


def test_claude_skills_live_at_plugin_root():
    """Claude Code loads plugin skills from `<plugin-root>/skills/`, NOT from
    inside `.claude-plugin/` (which holds manifests only).

    Shipped 2026-07-22 at `.claude-plugin/skills/m3-guide/` — the file reached
    every user's plugin cache and silently never loaded, because the client does
    not look there. Verified against the layout of plugins that DO work
    (claude-plugins-official/discord, frontend-design): both put skills at
    `skills/` beside `.claude-plugin/plugin.json`.
    """
    root_skills = os.path.join(_ROOT, "skills")
    assert os.path.isdir(root_skills), (
        "Claude Code plugin skills must live at <root>/skills/ — directory missing"
    )

    stray = os.path.join(_ROOT, ".claude-plugin", "skills")
    assert not os.path.exists(stray), (
        "skills found under .claude-plugin/skills — Claude Code will NOT load "
        "them. Move to <root>/skills/ (see this test's docstring)."
    )

    # Every skill dir must carry a SKILL.md, or the client silently skips it.
    for name in os.listdir(root_skills):
        d = os.path.join(root_skills, name)
        if os.path.isdir(d):
            assert os.path.isfile(os.path.join(d, "SKILL.md")), (
                f"skills/{name}/ has no SKILL.md — it will not load"
            )
