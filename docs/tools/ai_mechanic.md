---
tool: bin/ai_mechanic.py
sha1: 2e682de30f76
mtime_utc: 2026-05-01T09:15:53.143021+00:00
generated_utc: 2026-05-01T13:05:26.696811+00:00
private: false
---

# bin/ai_mechanic.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--force` | Required. Confirms you understand this drops tables. | `False` | Script refuses to run without the flag. | store_true | Permits DROP TABLE operations to recreate schema. |
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None | Script refuses to run without the flag. | str | Routes destructive operations against PATH instead of default. |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (add_database_arg, resolve_db_path)`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `db_path`` (line 18)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
