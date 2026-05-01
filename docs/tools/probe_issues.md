---
tool: benchmarks/locomo/probe_issues.py
sha1: fc40ff64e6b4
mtime_utc: 2026-04-22T01:01:09.578756+00:00
generated_utc: 2026-05-01T13:05:27.174577+00:00
private: true
---

# benchmarks/locomo/probe_issues.py

## Purpose

Probe specific issues identified in handoff analysis:

1) role distribution Melanie:7465 / Gina:24 — is Caroline missing? Check the
   raw dataset and the ingest rows.
2) dia_id format "D8:6; D9:17" in multi-hop gold — is that a single string with
   semicolon-joined evidence? That would break gold matching.
3) Zero-hit "gold: []" cases — empty gold list means the Q has no evidence to
   match against; these should be filtered out of the denominator or flagged.
4) For one zero-hit case with real gold (idx=45 'D11:4'), did we actually
   ingest that dia_id? Pull the row.

---

## Entry points

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

**sqlite**

- `sqlite3.connect()  → `str(db_path)`` (line 78)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `agent_memory.db`
- `locomo10.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
