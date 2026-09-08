---
tool: bin/embed_agent_instructions.py
sha1: d5b4139c5c0a
mtime_utc: 2026-09-08T23:41:01.507484+00:00
generated_utc: 2026-09-08T23:41:23.658884+00:00
private: false
---

# bin/embed_agent_instructions.py

## Purpose

One-shot script: embed AGENT_INSTRUCTIONS.md sections as searchable memory items.

Splits the file into 9 semantic sections, writes each as type=document
with embed=True. Idempotent: soft-deletes any prior architecture items
(agent_id="system", source="architecture") before writing fresh ones.

---

## Entry points

- `async def main()` (line 238)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `NO_COLOR`

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (resolve_db_path)`
- `memory_bridge (memory_delete, memory_write)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `m3_core.paths (seam_backend, seam_dialect)`

---

## File dependencies (repo paths referenced)

- `AGENT_INSTRUCTIONS.md`
- `agent_memory.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
