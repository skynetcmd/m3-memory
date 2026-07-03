---
tool: bin/m3_lifecycle_summary.py
sha1: 317885c7094d
mtime_utc: 2026-07-02T21:51:11.647462+00:00
generated_utc: 2026-07-03T20:00:03.608552+00:00
private: false
---

# bin/m3_lifecycle_summary.py

## Purpose

CLI wrapper for the memory lifecycle/contradiction observability summary.

Thin operator-facing surface over ``memory_maintenance.memory_lifecycle_summary_impl``
— the SAME function the ``memory_lifecycle_summary`` MCP tool calls, so the agent
and the operator see identical numbers. Read-only.

    python bin/m3_lifecycle_summary.py                 # last 7 days, human table
    python bin/m3_lifecycle_summary.py --window-days 30 --json
    python bin/m3_lifecycle_summary.py --top-n 10

---

## Entry points

- `def main()` (line 50)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--window-days` | Look-back window in days (default 7). | `7` |  | int |  |
| `--top-n` | Rows in the most-revised/contradicted lists (0 = omit). | `5` |  | int |  |
| `--json` | Emit raw JSON instead of a human table. | `False` |  | store_true |  |
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None |  | str |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (add_database_arg)`
- `memory_maintenance`

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
