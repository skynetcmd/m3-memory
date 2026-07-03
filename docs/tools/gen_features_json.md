---
tool: bin/gen_features_json.py
sha1: e1cdcd87128a
mtime_utc: 2026-07-02T21:51:11.642462+00:00
generated_utc: 2026-07-03T20:00:03.365750+00:00
private: false
---

# bin/gen_features_json.py

## Purpose

gen_features_json.py — generate docs/features.json (machine-readable capabilities).

A static, structured feature/compliance schema for AI and search ingestion — lets
multi-model systems (Perplexity, Gemini, Claude, ChatGPT) pick up M3's binary
capabilities without parsing prose. The TOOL COUNT and DOMAIN LIST are derived from
docs/tools/MCP_CATALOG.json so they never drift; the compliance/feature booleans are
hand-curated below against verified source-of-truth docs (MYTHS_AND_FACTS.md,
FIPS_COMPLIANCE.md, the tool catalog). Re-run after any catalog change.

    python bin/gen_features_json.py

IMPORTANT: every value here must be VERIFIABLE in the codebase or the source-of-truth
docs. Do not add aspirational claims — this file is consumed as authoritative.

---

## Entry points

- `def main()` (line 25)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

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

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `MCP_CATALOG.json`
- `benchmarks/longmemeval/LME-S_Benchmarking_Report.md`
- `docs/CAPABILITY_MATRIX.md`
- `docs/tools/MCP_CATALOG.json`
- `features.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
