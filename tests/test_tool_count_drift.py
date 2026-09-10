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
import subprocess
import sys

import pytest

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
    # The package docstring is the description PyPI and every `help(m3_memory)`
    # renders. It was ungated and had rotted to "66 MCP tools" while the catalog
    # passed 100 — the same drift this file already guards everywhere else.
    os.path.join(_ROOT, "m3_memory", "__init__.py"),
    os.path.join(_ROOT, "docs", "COMPARISON.md"),
    os.path.join(_ROOT, "docs", "MYTHS_AND_FACTS.md"),
    os.path.join(_ROOT, "docs", "tools", "files_memory.md"),
    # Both carried a stale "87-tool" while the catalog was at 108. The list's own
    # instruction above ("a new public doc that quotes the count belongs here")
    # was never followed for either — which is why the completeness test below
    # now derives this set instead of trusting the list.
    os.path.join(_ROOT, "docs", "claude_ai_connector.md"),
    os.path.join(_ROOT, "docs", "claude_code_plugin.md"),
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
# How close a number must be to the catalog total before it reads as a stale copy
# of it rather than a legitimate subcount. The catalog grows by a few tools at a
# time, so a doc that lagged one or two bumps sits within ~25; a domain subcount
# (26 files-memory, 18 essentials) sits far below. Widen only with evidence.
_TOTAL_DRIFT_BAND = 25

_SUBCOUNT_EXCEPTIONS = (
    "26 MCP tools",        # files_memory.md title — the files-memory DOMAIN count
    "All 26 tools",        # files_memory.md — the files-memory domain itself
    "26-tool files-memory",  # README — the files-memory domain as a sub-layer
    "(5 tools)",           # files_memory.md — files_corpus_* module
    # Surfaced when _TOOLS_RE was widened to hyphen/interposed-qualifier forms.
    # All three are genuine SUBCOUNTS the older, narrower pattern could not see —
    # not newly-introduced drift. Each is far below the catalog total, which is
    # what makes them safe to whitelist rather than rephrase.
    "26-tool",             # README — files-memory domain, hyphenated form
    "10-tool",             # README — a per-layer subcount
    "18 essential tools",  # MYTHS_AND_FACTS — the lazy-mode essentials set
)

# Section headings like "## Phase 1 tools — walker" are not counts — the
# number is a phase ordinal. Strip these before scanning.
_NONCOUNT_RE = re.compile(r"\bPhase \d+ tools\b")

# Catches "96 tools", "96 MCP tools", "87-tool bridge" and "All 87 m3-memory
# tools".
#
# Widened twice, each time after a stale count walked through:
#   1. the optional "MCP " qualifier let a stale "96 MCP tools" slip past;
#   2. requiring a SPACE and requiring `tools` to be ADJACENT let BOTH
#      "87-tool bridge" (hyphen, singular) and "All 87 m3-memory tools"
#      (interposed qualifier) sit in docs/claude_ai_connector.md while the
#      catalog moved 87 -> 108.
#
# So: hyphen or space, an optional single interposed qualifier word, optional
# "MCP ", and singular or plural. Each widening was driven by a real miss rather
# than by imagination — but note the shape of the failure: three misses, three
# patches. If a fourth form appears, the answer is probably a different check,
# not a fourth alternation.
_TOOLS_RE = re.compile(r"\b(\d+)[- ](?:[A-Za-z0-9_.-]+[- ])?(?:MCP )?tools?\b")


# Files that legitimately carry an exact count this gate must not police.
_UNGATED_BY_DESIGN = {
    # A changelog is a HISTORICAL record: "96 tools" in a past release entry was
    # true when written, and rewriting history to match today's catalog would
    # make the changelog lie about the release it describes.
    os.path.normpath("CHANGELOG.md"),
}


def _looks_generated(text: str) -> bool:
    """True if the file declares itself generated (first ~40 lines).

    Generators own their numbers and often count a different denominator; their
    freshness is enforced by re-running them, not by this gate.
    """
    head = " ".join(text.splitlines()[:40]).lower()
    return any(
        marker in head
        for marker in (
            "**generated**",
            "_generated",       # docs/tools/ENV_VAR_RECONCILE_REPORT.md's italic form
            "generated by",     # docs/MCP_TOOLS.md
            "do not edit",
            "auto-generated",
            "autogenerated",
        )
    )


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


def test_every_doc_quoting_a_tool_count_is_gated():
    """Completeness gate: scan ALL public docs, not just the curated list.

    `_DOC_FILES` is hand-maintained, so it can only catch drift in files somebody
    remembered to add. Two docs (claude_ai_connector.md, claude_code_plugin.md)
    quoted "87-tool" while the catalog sat at 108, and neither was listed --
    invisible to a check that iterates the list, despite the list's own comment
    telling contributors to add such files.

    So this walks every tracked .md and fails on an exact count in a file that is
    not gated. It is the same fix as test_every_sqlite_core_table_exists_on_pg
    and test_every_cli_tool_has_a_page: enumerate the source of truth, don't
    re-check a curated subset.

    Reuses `_exact_tool_count_claims` rather than restating the pattern -- a
    second copy of the predicate would drift from the first, which is the defect
    independent of whether either copy is correct.
    """
    import subprocess

    try:
        tracked = subprocess.run(
            ["git", "ls-files", "*.md"], cwd=_ROOT,
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout.split()
    except Exception as exc:  # noqa: BLE001 — not a git checkout / no git
        import pytest
        pytest.skip(f"cannot enumerate tracked docs: {exc}")

    gated = {os.path.normcase(os.path.abspath(p)) for p in _DOC_FILES}
    ungated: list[str] = []
    for rel in tracked:
        path = os.path.join(_ROOT, rel)
        if os.path.normcase(os.path.abspath(path)) in gated:
            continue
        # Bench/private trees are excluded from public-count policy.
        if rel.startswith(("benchmarks/", "build/", ".scratch/")):
            continue
        if os.path.normpath(rel) in _UNGATED_BY_DESIGN:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        # GENERATED docs carry counts their own generator computes, often on a
        # DIFFERENT denominator (MCP_TOOLS.md documents 121 = 110 in-catalog + 11
        # proxy tools; docs/tools/README.md's 107 counts bin/*.py CLI tools, not
        # MCP tools). Flagging those would be a false alarm, and a gate that
        # fires on healthy files trains people to ignore it. Their freshness is
        # already enforced by re-running the generator (test_generated_docs_fresh).
        if _looks_generated(text):
            continue
        # Only CATALOG-TOTAL-shaped claims matter here. Most exact counts in the
        # docs are legitimate subcounts -- a domain's size, a module's tool list,
        # a phase plan -- and flagging all 57 of them would make this gate a
        # false-alarm generator, which trains people to ignore it and is strictly
        # worse than no gate at all. A number is total-shaped when it is within
        # _TOTAL_DRIFT_BAND of the real catalog total: close enough that it is
        # almost certainly trying to BE the total, and stale if it is not equal.
        total = _computed_count()
        near = [
            n for n in _exact_tool_count_claims(text)
            if n != total and abs(n - total) <= _TOTAL_DRIFT_BAND
        ]
        if near:
            ungated.append(f"{rel}: {sorted(set(near))} (catalog total is {total})")

    assert not ungated, (
        "these docs quote a number close enough to the catalog total to be a "
        "stale copy of it, and are NOT gated by _DOC_FILES — rephrase as "
        "'100+ tools', or add the file to _DOC_FILES so it is checked "
        f"exactly:\n" + "\n".join(ungated)
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


def test_no_hardcoded_version_in_console_script_version_flags():
    """`--version` must read package metadata, never a literal.

    pyproject.toml is the single source of truth and sync_manifest_versions.py
    propagates it to the derived manifests -- but a literal baked into an
    argparse `version=` string is invisible to that sync and drifts silently
    every release. `m3-team --version` reported 2026.4.8 for three months while
    the package was 2026.7.25.0, which makes a CURRENT install look stale to
    anyone (or any probe) that trusts it.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    # A version= keyword whose value is a STRING LITERAL containing a version
    # number, e.g.  version="m3-team 2026.4.8". An f-string interpolating
    # __version__ is the correct form and must not match.
    literal = re.compile(r'\bversion\s*=\s*["\'][^"\']*\d+\.\d+[.\d]*[^"\']*["\']')
    offenders = []
    for py in (root / "m3_memory").rglob("*.py"):
        if "__pycache__" in str(py):
            continue
        text = py.read_text(encoding="utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            m = literal.search(line)
            if m and "__version__" not in line:
                offenders.append(f"{py.relative_to(root)}:{n}: {line.strip()}")

    assert not offenders, (
        "hardcoded version literal(s) -- read m3_memory.__version__ instead:\n  "
        + "\n  ".join(offenders)
    )


# ── test-count claims ──────────────────────────────────────────────────
#
# Same drift shape as the tool count, previously ungated: the README advertises
# a rounded floor ("N+ tests") that only moves when someone remembers. It had
# rotted to "2,400+" while the suite had grown well past it.
#
# The floor counts `def test_` FUNCTIONS, not collected cases. Collected counts
# vary by environment -- optional extras, plugins, and the working directory all
# change what pytest gathers (a --collect-only here reported 3,654 while the
# pre-push hook's run reported 3,176). Pinning a published claim to a number
# that moves with the machine makes the gate itself flaky, and a flaky gate gets
# disabled. The static function count is identical everywhere, and is a
# CONSERVATIVE floor: parametrisation only ever expands it at run time, so a
# README claim at or below it is true on every machine.

_TEST_FLOOR_RE = re.compile(r"([0-9][0-9,]{2,})\s*\*{0,2}\+?\s*\*{0,2}\s*tests", re.I)


def _readme_test_floors() -> list[int]:
    with open(os.path.join(_ROOT, "README.md"), encoding="utf-8") as fh:
        text = fh.read()
    return [int(m.group(1).replace(",", "")) for m in _TEST_FLOOR_RE.finditer(text)]


def _count_test_functions() -> int:
    """`def test_` functions across tests/ -- environment-independent."""
    total = 0
    for root, _dirs, files in os.walk(os.path.join(_ROOT, "tests")):
        for fn in files:
            if fn.startswith("test_") and fn.endswith(".py"):
                with open(os.path.join(root, fn), encoding="utf-8", errors="replace") as fh:
                    total += sum(1 for ln in fh if ln.lstrip().startswith("def test_"))
    return total


def test_readme_test_count_floor_is_honest():
    """The advertised test floor must be a true floor, and not badly stale.

    Guards both directions: it may never OVERSTATE the suite (a false claim),
    and may not fall more than 500 behind (stale enough to undersell it).
    """
    floors = _readme_test_floors()
    assert floors, "README no longer states a test count -- expected a '<N> tests' claim"
    actual = _count_test_functions()
    for claimed in floors:
        assert claimed <= actual, (
            f"README claims {claimed:,} tests but only {actual:,} `def test_` "
            "functions exist -- the floor OVERSTATES the suite. Lower it."
        )
        assert actual - claimed < 500, (
            f"README claims {claimed:,} tests while {actual:,} `def test_` functions "
            "exist -- the floor is stale. Round the real number down and update it "
            "(the At-a-Glance table and the Why Trust This section)."
        )
