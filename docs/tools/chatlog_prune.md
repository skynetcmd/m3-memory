---
tool: bin/chatlog_prune.py
sha1: 24e2ef7e1c20
mtime_utc: 2026-06-30T21:32:48.327241+00:00
generated_utc: 2026-06-30T22:19:18.149720+00:00
private: false
---

# bin/chatlog_prune.py

## Purpose

chatlog_prune — aged noise pruning for chatlog turns.

Builds on bin/chatlog_decay.py's philosophy (deterministic, age-graded) but
adds (a) a PRUNE tier that soft-deletes aged noise, (b) a repeated-status
request/response detector, and (c) an aged generic-low-value classifier.

THREE AGE TIERS (all tunable):
    age < FRESH_DAYS            -> keep untouched (recent noise may still have value)
    FRESH_DAYS <= age < PRUNE   -> DECAY: lower importance + set valid_to (suppress)
    age >= PRUNE_DAYS           -> PRUNE: soft-delete (is_deleted=1) so it leaves
                                   retrieval and propagates fleet-wide as a tombstone

Soft-delete only: nothing is hard-deleted or VACUUMed here, so it stays
recoverable and rides the normal delta sync (is_deleted + updated_at bump).

NOISE = ephemeral (PIDs/UUIDs/status/tmp/JSON one-liners)
      | short user command (<=4 words, not a question/refusal)
      | repeated-status (normalized content recurs >= STATUS_MIN_CLUSTER times
        AND matches status request/response vocabulary)  [request AND response]
      | generic-low (importance <= GENERIC_IMP_MAX, unpromoted chat_log,
        not a question, no strong-signal markers)         [the bulk]

KEEP guards (never noise): questions ('?'), explicit refusals, importance above
a floor, assistant turns carrying decision/code markers.

USAGE
    python3 chatlog_prune.py --db <path> [--dry-run]            # default: dry-run
    python3 chatlog_prune.py --db <path> --apply
    options: --fresh-days 14 --prune-days 45 --status-min-cluster 5
             --generic-imp-max 0.3 --no-generic

---

## Entry points

- `def run()` (line 184)
- `def main()` (line 317)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--db` |  | — |  | str |  |
| `--fresh-days` |  | `14.0` |  | float |  |
| `--prune-days` |  | `45.0` |  | float |  |
| `--status-min-cluster` |  | `5` |  | int |  |
| `--generic-imp-max` |  | `0.3` |  | float |  |
| `--keep-imp-floor` |  | `0.4` |  | float |  |
| `--generic-protect-len` | generic turns >= this length (if structured) are suppress-only | `300` |  | int |  |
| `--generic-delete-maxlen` | generic turns >= this length are never tombstoned (suppress-only) | `300` |  | int |  |
| `--no-generic` |  | `False` |  | store_true |  |
| `--max-actions` | Max decay+prune writes per run (0 = no cap). Bounds a single pass so a large backlog drains across runs instead of one monster pass; oldest noise goes first. | `0` |  | int |  |
| `--apply` |  | `False` |  | store_true |  |
| `--dry-run` |  | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `sqlite_pragmas (apply_pragmas, checkpoint_truncate, profile_for_db)`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `db_path`` (line 197)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
