---
tool: bin/m3_notification_waiter.py
sha1: 8c7fad07fafc
mtime_utc: 2026-09-12T12:10:40.632753+00:00
generated_utc: 2026-09-12T12:26:01.434727+00:00
private: false
---

# bin/m3_notification_waiter.py

## Purpose

Single-shot m3 notification waiter — blocks until YOUR inbox has something new.

Exits 0 the moment a new notification appears for --agent-id. Exits 2 on timeout.
Run it as a background/async task: your runtime's process-completion signal is
what pushes the wake-up turn to you, so this costs ZERO conversation turns while
it waits.

WHY IT WATCHES THE WAL FILE, NOT THE DATABASE
SQLite has no cross-process blocking change notification. Update hooks are
in-process only and fire solely for the connection's OWN writes, so a waiter
CANNOT be woken by another process inserting a row (verified, sqlite 3.50.4).
Watching agent_memory.db-wal is the working substitute: a notify write changes
both its mtime and its size (measured: 263712 -> 280192 bytes).

The cheap 1s polling happens HERE, in a subprocess that costs no context. That
is the whole point: on-change delivery to the agent, without a turn per tick.

---

## Entry points

- `def main()` (line 57)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--agent-id` | Inbox to watch. Repeatable: pass once per agent to serve several inboxes from ONE process. The WAL trigger is shared -- a single file change covers every inbox -- so N agents cost N cheap confirm-polls after a change, not N watchers. | — |  | append |  |
| `--engine-root` |  | `os.environ.get('M3_ENGINE_ROOT') or str(pathlib.Path.home() / '.m3' / 'engine')` |  | str |  |
| `--interval` |  | `1.0` |  | float |  |
| `--timeout` | Give up after N seconds so a forgotten waiter cannot leak. | `3600.0` |  | float |  |
| `--supervise` | Never exit: after each detection, re-arm and keep waiting. For an ONSTART scheduled task, which needs a long-lived process. Without it the waiter is single-shot, which is what a runtime wants when the process EXIT is the wake signal. | `False` |  | store_true |  |
| `--ack` | Ack on detection. OFF BY DEFAULT, deliberately: the notifications table has only `read_at` -- no separate 'received' column -- so acking here destroys the only record that a message was unread. Measured on this machine: 29 of 30 recent notifications carried NO task_id, so 'the task state machine tracks it' is false for ~97% of real traffic. Enable this only where every watched kind is backed by a task whose own state survives the ack. | `False` |  | store_true |  |

---

## Environment variables read

- `M3_ENGINE_ROOT`

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `['m3', 'admin', 'notifications_ack_all', '--agent_id', a, '--yes']`` (line 145)
- `subprocess.run()  → `['m3', 'admin', 'notifications_poll', '--agent_id', agent_id, '--limit', '50']`` (line 45)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
