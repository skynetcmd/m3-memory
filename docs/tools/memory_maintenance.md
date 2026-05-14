---
tool: bin/memory_maintenance.py
sha1: ba3d613fb9d7
mtime_utc: 2026-05-14T05:25:36.033320+00:00
generated_utc: 2026-05-14T05:37:12.908575+00:00
private: false
---

# bin/memory_maintenance.py

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

- `_task_runtime (add_log_file_arg, setup_task_runtime)`
- `memory_core`
- `memory_core (ARCHIVE_DB_PATH, DEDUP_LIMIT, DEDUP_THRESHOLD, _content_hash, _cosine, _db, _embed, _get_embed_client, _pack, _unpack, ctx, get_best_llm, memory_link_impl)`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `ARCHIVE_DB_PATH`` (line 31)
- `sqlite3.connect()  → `active_path`` (line 211)


---

## Notable external imports

- `base64`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
