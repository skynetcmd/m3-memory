---
tool: bin/chatlog_decay.py
sha1: 43aeaa57d1f0
mtime_utc: 2026-05-23T12:31:13.374373+00:00
generated_utc: 2026-05-23T17:51:49.032521+00:00
private: false
---

# bin/chatlog_decay.py

## Purpose

chatlog_decay — deterministic ephemeral-content decay for chatlog turns.

Aggressively suppresses chatlog turns whose content has only short-term
value (transient IDs, status snapshots, short user commands) by lowering
their `importance` over time and setting `valid_to` past a hard cutoff.

The `m3:curate-chatlog` subagent calls this tool to do the heavy lifting
without spending tokens evaluating each row.

USAGE
=====

    # Dry run — print what would change, no writes.
    python bin/chatlog_decay.py [--db <path>] [--dry-run]

    # Apply the decay sweep.
    python bin/chatlog_decay.py [--db <path>] --apply

    # Override DB explicitly (also respects $CHATLOG_DB env var).
    python bin/chatlog_decay.py --db /path/to/agent_chatlog.db --apply

DB SELECTION
============

In priority order:
  1. --db <path> CLI argument
  2. $CHATLOG_DB env var
  3. $M3_DATABASE env var (unified mode)
  4. memory/agent_chatlog.db (default)

ALL queries scope to `type='chat_log'`, regardless of layout.

EPHEMERAL CONTENT CATEGORIES
============================

(1) GENERAL EPHEMERAL  — transient IDs, status snapshots, system noise:
    - PIDs / ports / uuids / batch_ids / temp-file paths
    - "completion: X%", "cost: $Y", "X/Y in_progress", live status numbers
    - JSON tool-result-only content like {"ok": true} or {"count": 42}

(2) SHORT-COMMAND  — short user-role turns:
    - "status", "start", "do it", "proceed", "yes", "ok", "go", "(a)"
    - any user-role content ≤4 words (per token-split heuristic)
    Halves the multiplier for stage (1).
    EXCLUSIONS: assistant-role short turns (could carry decisions),
    questions ("?" present), explicit refusals ("no", "stop", "kill").

DECAY SCHEDULE  (importance multiplier vs. age)
================================================

GENERAL EPHEMERAL:
  age <  1 day  -> 1.00x
  age <  3 days -> 0.50x
  age <  7 days -> 0.20x
  age >= 7 days -> 0.05x  AND  valid_to = now (immediate retire)

SHORT-COMMAND (additional halving):
  age <  1 day  -> 0.50x
  age <  3 days -> 0.10x
  age >= 3 days -> 0.02x  AND  valid_to = now - 1 day (immediate retire)

PROMOTION ESCAPE HATCH
======================

Rows with `type != 'chat_log'` (already promoted via `chatlog_promote`)
are excluded from this sweep. Promotion graduates a turn out of the
ephemeral regime entirely.

---

## Entry points

- `def main()` (line 331)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--db` | path to chatlog DB (overrides env) | None |  | str |  |
| `--apply` | actually write changes (default: dry-run) | `False` |  | store_true |  |
| `--dry-run` | explicit dry-run (default behavior) | `False` |  | store_true |  |

---

## Environment variables read

- `CHATLOG_DB`
- `CHATLOG_DB_PATH`
- `M3_DATABASE`

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `db_path`` (line 238)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `agent_chatlog.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
