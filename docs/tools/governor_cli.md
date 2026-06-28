---
tool: bin/governor_cli.py
sha1: 60ce6c996f0e
mtime_utc: 2026-06-27T17:54:55.461944+00:00
generated_utc: 2026-06-27T23:22:27.216277+00:00
private: false
---

# bin/governor_cli.py

## Purpose

`m3 governor <status|migrate>` — inspect and migrate legacy scheduled tasks
to the Adaptive Background Workload Governor.

  status  — report which governor-eligible cron/schtasks entries are still
            installed (the same nag `m3 doctor` prints), plus the not-migratable
            tasks and why.
  migrate — remove the governor-eligible entries with current privileges; print
            the privileged OS-specific commands for any that need elevation.

Thin CLI over bin/governor_migration.py so the detection/removal logic stays in
one tested module. Always exits 0 on `status`; `migrate` exits 0 unless every
removal failed (so scripts can detect a no-op-due-to-permission).

---

## Entry points

- `def main()` (line 92)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--yes`, `-y` | Skip the confirmation prompt (headless use). | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `governor_migration`

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
