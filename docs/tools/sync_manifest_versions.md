---
tool: bin/sync_manifest_versions.py
sha1: 2d79c43271ff
mtime_utc: 2026-08-07T23:53:52.259120+00:00
generated_utc: 2026-08-08T14:40:50.109551+00:00
private: false
---

# bin/sync_manifest_versions.py

## Purpose

Sync every version-bearing manifest to the single source of truth:
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

---

## Entry points

- `def main()` (line 77)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--check` | Exit non-zero if any manifest is out of sync; write nothing. | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `tomllib`

---

## File dependencies (repo paths referenced)

- `mcp-server.json`
- `plugin.json`
- `pyproject.toml`
- `server.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
