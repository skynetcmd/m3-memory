---
tool: bin/re_embed_all.py
sha1: 9a29d86275e7
mtime_utc: 2026-09-08T10:56:30.059924+00:00
generated_utc: 2026-09-08T10:59:36.329848+00:00
private: false
---

# bin/re_embed_all.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `def main()` (line 64)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None | Falls back to M3_DATABASE env then memory/agent_memory.db. | str | Routes this run against PATH for all DB reads/writes. |

---

## Environment variables read

- `M3_DATABASE`

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (add_database_arg, resolve_db_path)`
- `memory_core (_embed, _pack)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `m3_core.paths (seam_backend)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
