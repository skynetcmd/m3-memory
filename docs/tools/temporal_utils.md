---
tool: bin/temporal_utils.py
sha1: ed3f27f0aebb
mtime_utc: 2026-05-01T09:15:39.852428+00:00
generated_utc: 2026-05-01T13:05:27.076173+00:00
private: false
---

# bin/temporal_utils.py

## Purpose

Enhanced temporal resolution utility for m3-memory.
Resolves relative date expressions (yesterday, last Friday, the Sunday before June 1st)
into absolute ISO-8601 dates based on an anchor timestamp.

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

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
