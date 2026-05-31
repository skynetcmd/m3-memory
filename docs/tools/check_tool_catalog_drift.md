---
tool: bin/check_tool_catalog_drift.py
sha1: 6f5fa0c718d2
mtime_utc: 2026-05-31T16:08:17.340434+00:00
generated_utc: 2026-05-31T18:42:52.653159+00:00
private: false
---

# bin/check_tool_catalog_drift.py

## Purpose

Single source of truth for the tool-catalog pre-push drift gate.

`bin/mcp_tool_catalog.py` is the canonical MCP tool registry. Several
artifacts are *generated* from it and are NOT auto-refreshed on write:

  - docs/tools/MCP_CATALOG.json   (via bin/gen_tool_manifest.py)
  - docs/MCP_TOOLS.md             (via bin/gen_mcp_inventory.py)
  - hardcoded "N tools" counts in README.md / docs/COMPARISON.md /
    docs/MYTHS_AND_FACTS.md / docs/tools/files_memory.md

If a tool is added/removed/renamed and these aren't regenerated, the docs
silently lie. This check regenerates the artifacts and fails if anything
changed (drift) or if the drift tests fail.

It is invoked by BOTH:
  - .githooks/pre-push  (local gate, every agent + human, before push)
  - .github/workflows/tool-catalog-drift.yml  (required CI check, on PR/push)

so the rule holds regardless of which agent (Claude / Gemini / Antigravity /
human) authored the change or which AGENTS file they read. This is the
mechanical enforcement behind the prose in docs/AGENT_INSTRUCTIONS.md.

Exit codes:
  0  no drift, tests pass
  1  drift detected or a drift test failed (the artifacts have been
     regenerated in the working tree — stage and commit them, then re-run)
  2  the check itself could not run (missing generator, etc.)

Usage:
    python bin/check_tool_catalog_drift.py            # check
    python bin/check_tool_catalog_drift.py --fix      # regen + leave staged-ready

---

## Entry points

- `def main()` (line 89)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--fix` | regenerate and leave changes in the working tree (don't fail on drift — for interactive fixing) | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `['git', 'diff', '--name-only', '--', *_GENERATED_PATHS]`` (line 82)
- `subprocess.run()  → `[_PY, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', *_DRIFT_TESTS]`` (line 117)
- `subprocess.run()  → `cmd`` (line 66)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `docs/MCP_TOOLS.md`
- `docs/tools/MCP_CATALOG.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
