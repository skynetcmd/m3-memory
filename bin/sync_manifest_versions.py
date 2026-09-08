"""Sync every version-bearing manifest to the single source of truth:
``pyproject.toml`` ``[project].version``.

The problem this removes: the package version was hand-copied into FOUR static
manifests (server.json, mcp-server.json, and the Claude + Antigravity
plugin.json files). Every release someone had to remember to edit each one, and
when they forgot, the manifest silently drifted — the plugin.json files lagged
6 releases (2026.7.13.0 while pyproject was 2026.7.19.5), and the marketplace
serves those directly to every user's ``/plugin install``.

These are STATIC json read as-is by Claude Code / Antigravity / MCP registries,
so the version must physically live in each file — but it must be GENERATED from
pyproject, never hand-typed. Release flow is now:

    1. edit pyproject.toml  [project].version
    2. python bin/sync_manifest_versions.py     # writes it into every manifest
    3. commit

``--check`` exits non-zero if any manifest is out of sync (used by CI /
tests/test_tool_count_drift.py so a release bump that skips this step fails
loudly instead of shipping a stale manifest).

Every ``"version"`` key is rewritten: the top-level one, and any nested
``packages[].version`` (server.json) — matching what the drift test asserts.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# The derived manifests whose version must equal pyproject's. Keep in lockstep
# with tests/test_tool_count_drift.py::_VERSIONED_MANIFESTS.
_MANIFESTS = (
    _ROOT / "server.json",
    _ROOT / "mcp-server.json",
    _ROOT / ".claude-plugin" / "plugin.json",
    _ROOT / ".antigravity-plugin" / "plugin.json",
)


# NON-JSON artifacts that also carry the version. These drift the same way the
# manifests do, and for the same reason: nothing re-derives them, so they are
# only ever as fresh as the last person who remembered.
#
# The PyPI badge is the clearest case. It is a self-hosted SVG, not a live
# shields.io query, and on 2026-09-08 it still read v2026.7.18.1 while the
# shipped version was 2026.9.8.0 — seven weeks stale on the README's first
# screen. It is also the one badge that changes exactly when a release happens,
# which is what makes it belong here rather than in the weekly badge cron.
#
# SECURITY.md's "Supported Versions" table is the same failure with higher
# stakes: a security document telling readers that 2026.7.x is the supported
# line, two months after it stopped being.
_PYPI_BADGE = _ROOT / "docs" / "badges" / "pypi-version.svg"
_SECURITY_MD = _ROOT / "docs" / "SECURITY.md"

# Any m3 version string: YYYY.M.D.N, optionally v-prefixed.
_M3_VERSION_RE = re.compile(r"v?(?P<v>20\d\d\.\d{1,2}\.\d{1,2}\.\d+)")


def _pyproject_version() -> str:
    with open(_ROOT / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def _minor_series(version: str) -> str:
    """``2026.9.8.0`` -> ``2026.9.x`` — the form SECURITY.md's table uses."""
    parts = version.split(".")
    return ".".join(parts[:2]) + ".x"


def _sync_pypi_badge(target: str, *, check: bool) -> "str | None":
    """Rewrite every version string in the PyPI badge SVG.

    Returns a drift description when out of sync, else None. The SVG carries the
    version three times (title, shadow text, foreground text); all must match or
    the badge renders one value and announces another to screen readers.
    """
    if not _PYPI_BADGE.is_file():
        return f"{_PYPI_BADGE.relative_to(_ROOT)}: missing"
    text = _PYPI_BADGE.read_text(encoding="utf-8")
    stale = sorted({m.group("v") for m in _M3_VERSION_RE.finditer(text)} - {target})
    if not stale:
        return None
    rel = _PYPI_BADGE.relative_to(_ROOT)
    if check:
        return f"{rel}: {stale} != {target}"
    new_text = _M3_VERSION_RE.sub(lambda m: m.group(0).replace(m.group("v"), target), text)
    _PYPI_BADGE.write_text(new_text, encoding="utf-8")
    return None


def _sync_security_supported(target: str, *, check: bool) -> "str | None":
    """Keep SECURITY.md's "Supported Versions" row on the current series.

    Only the row marked ``(latest)`` is touched: older rows are deliberate
    history (what is still receiving fixes), and rewriting those would change a
    security claim rather than refresh one.
    """
    if not _SECURITY_MD.is_file():
        return f"{_SECURITY_MD.relative_to(_ROOT)}: missing"
    text = _SECURITY_MD.read_text(encoding="utf-8")
    want = _minor_series(target)
    row = re.compile(r"^\|\s*(20\d\d\.\d{1,2}\.x)\s*\(latest\)", re.M)
    m = row.search(text)
    rel = _SECURITY_MD.relative_to(_ROOT)
    if not m:
        return f"{rel}: no '(latest)' row found in Supported Versions"
    if m.group(1) == want:
        return None
    if check:
        return f"{rel}: {m.group(1)} (latest) != {want}"
    _SECURITY_MD.write_text(
        text[: m.start(1)] + want + text[m.end(1):], encoding="utf-8"
    )
    return None


# Match a JSON ``"version": "X.Y.Z…"`` pair. Surgical TEXT edit — we rewrite ONLY
# the version value and leave every other byte (em-dashes, array layout, spacing)
# untouched. Re-serializing the whole file via json.dump would reflow arrays and
# escape non-ASCII (ensure_ascii), producing spurious churn on every release.
_VERSION_RE = re.compile(r'("version"\s*:\s*")([^"]*)(")')


def _current_versions(text: str) -> list[str]:
    """Every ``"version"`` value found in the raw manifest text (for --check)."""
    return [m.group(2) for m in _VERSION_RE.finditer(text)]


def _rewrite_versions(text: str, target: str) -> tuple[str, int]:
    """Rewrite every ``"version"`` value to ``target``; return (new_text, changed)."""
    changed = 0

    def _sub(m: "re.Match[str]") -> str:
        nonlocal changed
        if m.group(2) != target:
            changed += 1
        return f"{m.group(1)}{target}{m.group(3)}"

    return _VERSION_RE.sub(_sub, text), changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="Exit non-zero if any manifest is out of sync; write nothing.")
    args = ap.parse_args()

    target = _pyproject_version()
    drifted: list[str] = []
    wrote: list[str] = []

    for path in _MANIFESTS:
        if not path.is_file():
            print(f"[!] missing manifest: {path}", file=sys.stderr)
            drifted.append(str(path))
            continue
        text = path.read_text(encoding="utf-8")
        # Sanity: the file must actually parse as JSON (catch a corrupt manifest
        # before we regex-edit it), but we WRITE the surgically-edited text, not a
        # re-serialization, to preserve formatting.
        json.loads(text)
        stale = [v for v in _current_versions(text) if v != target]
        rel = path.relative_to(_ROOT)
        if not stale:
            continue
        if args.check:
            drifted.append(f"{rel}: {stale} != {target}")
            continue
        new_text, n = _rewrite_versions(text, target)
        json.loads(new_text)  # the surgical edit must not have broken JSON
        path.write_text(new_text, encoding="utf-8")
        wrote.append(f"{rel} ({n} version field(s) -> {target})")

    # Non-JSON artifacts: the README's PyPI badge and SECURITY.md's supported
    # series. Same contract as the manifests -- report under --check, rewrite
    # otherwise -- so a release can never ship a stale badge or a security
    # document naming the wrong supported line.
    for label, fn in (("pypi badge", _sync_pypi_badge),
                      ("SECURITY.md supported versions", _sync_security_supported)):
        # Ask in check-mode FIRST so we know whether a write is actually needed.
        # Reporting "wrote X" for a file that was already current is a small lie,
        # and a sync tool that overstates its work is one you stop reading.
        problem = fn(target, check=True)
        if not problem:
            continue                      # already in sync — say nothing
        if args.check:
            drifted.append(problem)
            continue
        if problem.endswith(": missing") or "no '(latest)' row" in problem:
            drifted.append(problem)       # cannot fix by rewriting
            continue
        fn(target, check=False)
        wrote.append(f"{label} -> {target}")

    if args.check:
        if drifted:
            print("version drift (run `python bin/sync_manifest_versions.py`):")
            for d in drifted:
                print(f"  {d}")
            return 1
        print(f"all manifests in sync at {target}")
        return 0

    if wrote:
        print(f"synced to {target}:")
        for w in wrote:
            print(f"  {w}")
    else:
        print(f"already in sync at {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
