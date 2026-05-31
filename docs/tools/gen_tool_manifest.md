---
tool: bin/gen_tool_manifest.py
sha1: 0d2b35be4f4f
mtime_utc: 2026-05-29T19:38:17.576494+00:00
generated_utc: 2026-05-31T18:42:52.726921+00:00
private: false
---

# bin/gen_tool_manifest.py

## Purpose

Generate a machine-readable tool-catalog manifest at docs/tools/MCP_CATALOG.json.

Imports `mcp_tool_catalog.TOOLS` (the single source of truth for every MCP
tool the bridge can register) and emits one compact record per tool:

  - name        — the catalog tool name
  - domain      — lazy-loading domain bucket (via tool_domains.domain_of_tool)
  - summary     — first sentence of the description, truncated to <=100 chars
  - destructive — True when the tool mutates/deletes (i.e. not default_allowed)
  - args        — [{name, type, required}] from parameters.properties, with the
                  universal "database" arg dropped (it's injected everywhere and
                  carries no per-tool signal)

Plus two top-level fields:

  - count          — number of non-meta tools (names not starting with "tools_").
                     This is the number the public docs quote ("N tools"); the
                     meta-tools tools_list_domains / tools_load_domain are the
                     lazy-loading escape hatch and are excluded by convention.
  - generated_note — reminder that the file is auto-generated.

Output is deterministic: tools are sorted by (domain, name), keys are sorted,
indent=2. Re-running with no catalog change produces a byte-identical file, so
a diff in MCP_CATALOG.json always reflects a real catalog change. The
`tests/test_tool_count_drift.py` regression test reads the `count` field and
asserts the hardcoded "N tools" claims in the docs still agree with it.

This manifest is derived only from the public TOOLS catalog — no benchmark,
database, or runtime data is read or written.

---

## Entry points

- `def main()` (line 126)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `mcp_tool_catalog`
- `tool_domains`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `MCP_CATALOG.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
