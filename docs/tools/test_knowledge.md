---
tool: bin/test_knowledge.py
sha1: 049b2cf5254c
mtime_utc: 2026-07-02T01:21:24.721357+00:00
generated_utc: 2026-07-03T20:00:03.993714+00:00
private: false
---

# bin/test_knowledge.py

## Purpose

_(no module docstring — update the source file.)_

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

- `memory.knowledge_helpers (add_knowledge, delete_knowledge, list_knowledge, search_knowledge)`
- `unittest`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
