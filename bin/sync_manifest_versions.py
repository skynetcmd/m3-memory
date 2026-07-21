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


def _pyproject_version() -> str:
    with open(_ROOT / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)["project"]["version"]


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
