---
tool: bin/secret_rotator.py
sha1: 547b73bd9fb7
mtime_utc: 2026-05-01T09:15:53.151020+00:00
generated_utc: 2026-05-01T13:05:27.055540+00:00
private: false
---

# bin/secret_rotator.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--dry-run` | Show planned rotations without writing | `False` | Rotates secrets: backs up old value, generates new token, encrypts to vault, logs event. | store_true | Logs planned rotations (new token length) but skips backup, encryption, vault write, and event logging. |
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None | Falls back to M3_DATABASE env then memory/agent_memory.db. | str | Routes all DB reads/writes against PATH for this run. |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `auth_utils (set_api_key)`
- `m3_sdk (M3Context)`
- `m3_sdk (add_database_arg)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `secrets`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
