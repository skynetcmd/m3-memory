---
tool: bin/pg_setup.py
sha1: b200c2a3ebdb
mtime_utc: 2026-04-23T20:33:55.966657+00:00
generated_utc: 2026-05-24T12:09:08.352188+00:00
private: false
---

# bin/pg_setup.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `def main()` (line 99)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `PG_URL`

---

## Calls INTO this repo (intra-repo imports)

- `auth_utils (get_api_key)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `psycopg2`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
