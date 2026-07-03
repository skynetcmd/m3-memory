---
tool: bin/memory_maintenance.py
sha1: 30b96fc1cb4c
mtime_utc: 2026-07-02T21:51:11.654461+00:00
generated_utc: 2026-07-03T20:00:03.714637+00:00
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
- `audit_trail (write_audit_entry)`
- `m3_sdk (_LAST_USER_INTERACTION)`
- `memory_core`
- `memory_core (ARCHIVE_DB_PATH, DEDUP_LIMIT, DEDUP_THRESHOLD, EMBED_DIM, _content_hash, _cosine, _db, _embed, _get_embed_client, _pack, _unpack, ctx, get_best_llm, m3_core_rs, memory_link_impl)`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `ARCHIVE_DB_PATH`` (line 33)
- `sqlite3.connect()  → `active_path`` (line 579)


---

## Notable external imports

- `base64`
- `memory (confidence)`
- `memory (trust)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
