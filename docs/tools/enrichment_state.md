---
tool: bin/enrichment_state.py
sha1: 627a73d21e28
mtime_utc: 2026-07-19T03:04:59.563781+00:00
generated_utc: 2026-07-19T19:29:22.229286+00:00
private: false
---

# bin/enrichment_state.py

## Purpose

Durable per-group enrichment state for m3_enrich.

Backs migration 028's enrichment_groups + enrichment_runs tables. Pure helper
module — m3_enrich.py imports from here; no reverse dependency. Designed to
be unit-testable in isolation against a fresh sqlite file.

State machine:
    pending ──claim──▶ in_progress ──┬── success (obs_emitted > 0)
                                      ├── empty   (extractor OK, 0 obs)
                                      ├── failed  (transient — eligible for retry)
                                      └── dead_letter (deterministic OR attempts>=N)

    stale claim (claimed_at older than CLAIM_TIMEOUT_SEC) ──▶ pending  (auto on resume)
    source_content_hash changed                              ──▶ superseded (old row)

All callers should hold a per-DB sqlite3.Connection in WAL mode. The module
neither opens nor closes connections — that's the caller's responsibility,
matching the rest of bin/.

---

## Entry points

_(no conventional entry point detected)_

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

**subprocess**

- `subprocess.run()  → `['git', 'rev-parse', 'HEAD']`` (line 105)


---

## Notable external imports

- `memory.backends (dialect)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
