---
paths:
  - "pyproject.toml"
  - "server.json"
  - "mcp-server.json"
  - ".claude-plugin/plugin.json"
  - ".antigravity-plugin/plugin.json"
  - "docs/badges/pypi-version.svg"
  - "docs/SECURITY.md"
---

# Version has ONE source of truth

`pyproject.toml` `[project].version`. Do **not** hand-edit version strings in the
derived manifests.

To cut a release:

1. bump `pyproject.toml`
2. run `python bin/sync_manifest_versions.py` — it writes the new version into
   every derived manifest (`server.json`, `mcp-server.json`,
   `.claude-plugin/plugin.json`, `.antigravity-plugin/plugin.json`), plus the
   README's PyPI badge and `docs/SECURITY.md`'s "Supported Versions" row
3. commit all of it together

`tests/test_tool_count_drift.py::test_all_manifests_synced_to_pyproject_version`
runs `--check` and fails the build if step 2 is skipped, so a stale manifest
cannot ship. The plugin manifests are served straight from `main` to every
user's `/plugin install`.

A release is published by pushing the `vX.Y.Z` tag — that is the single publish
trigger.
