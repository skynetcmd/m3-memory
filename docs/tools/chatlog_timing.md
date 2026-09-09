---
tool: bin/chatlog_timing.py
sha1: a74abc46ceb6
mtime_utc: 2026-09-09T03:36:57.706937+00:00
generated_utc: 2026-09-09T11:22:39.930880+00:00
private: false
---

# bin/chatlog_timing.py

## Purpose

Per-turn wall-clock timing from the chatlog — who spent the time, objectively.

WHY THIS EXISTS
---------------
"Why did that memory write take 30 seconds?" was answered wrong four times in a
row on 2026-09-09 (blamed m3's write path, then payload size, then line count,
then character count) because every answer was an INFERENCE. The chatlog already
stores what settles it: every turn with an ISO-8601 millisecond ``created_at``,
its role, and its length. The gap between consecutive turns IS the wall clock
for the turn that follows. Measured, not guessed.

The measurement that mattered: m3's own write is 0.09-0.51s and a full MCP stdio
round trip including cold start is 0.99s, so a 30s "save" is ~29s of AGENT
generation. This tool makes that decomposition a one-liner instead of a 15-line
ad-hoc script written under pressure.

CONCURRENCY — why this scopes by conversation, not by time
----------------------------------------------------------
Another agent (or another session on this machine) may be writing to m3 at the
same time. A pure time-window query would interleave their turns with yours and
silently corrupt every gap. So the default scope is a single
``conversation_id``, which is stable for a whole session (measured: 1,697 turns
under one id), and ``--agent`` / ``--model`` narrow further. ``idx_mi_conversation_composite``
already covers this shape, so it stays cheap as the store grows.

Reads go through ``M3Context.get_chatlog_conn()`` — the seam decides WHICH store
holds the turns (integrated / separate / hybrid) and WHICH backend serves it, so
this works on SQLite and PostgreSQL alike and on all three OSes. SELECT-only: a
concurrent writer is never blocked, and a row arriving mid-read is simply not
seen, which is correct — a partial tail beats a lock.

    python bin/chatlog_timing.py --last 30
    python bin/chatlog_timing.py --since 2026-09-09T02:40 --slowest 10
    python bin/chatlog_timing.py --conversation <uuid> --json

---

## Entry points

- `def main()` (line 156)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--conversation` | conversation_id to scope to (default: the most recent one — see the concurrency note) | `` |  | str |  |
| `--since` | ISO timestamp lower bound | `` |  | str |  |
| `--last` | max turns (default 40) | `40` |  | int |  |
| `--agent` | filter by agent_id | `` |  | str |  |
| `--model` | filter by model_id | `` |  | str |  |
| `--slowest` | also print the N slowest turns | `0` |  | int |  |
| `--json` | machine-readable output | `False` |  | store_true |  |

---

## Environment variables read

- `M3_PATH_BIN`

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `m3_core.context (M3Context)`
- `memory.backends (active_backend)`
- `memory.backends (active_backend, chatlog_table)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
