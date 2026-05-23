---
tool: bin/curator_apply.py
sha1: a1381d3906e0
mtime_utc: 2026-05-23T12:31:13.376194+00:00
generated_utc: 2026-05-23T17:51:49.068960+00:00
private: false
---

# bin/curator_apply.py

## Purpose

Deterministic apply of a curator plan — one entry point, no LLM in the loop.

The curate-{memory,chatlog} subagents emit a structured plan in their PLAN
spawn. Historically the APPLY spawn was another LLM agent that interpreted
that plan and called MCP tools one operation at a time. That path has
failed twice (2026-05-17) with two distinct failure modes:

  1. Agent looped single-id `memory_delete` for ~486 IDs (~16 min budget).
  2. After the prompt was rewritten to mandate `memory_delete_bulk`, the
     replacement agent instead invented a Bash-file-writes-the-ids strategy
     and ran past its budget reasoning about Windows path mapping.

This module is the structural fix per memory `4090f663` (the diagnose-the-
tool-shape rule, generalized): make the wrong path *impossible* by replacing
the agent-driven apply procedure with one deterministic function. The
agent's job becomes "emit a plan, call apply, read the report" — one MCP
round-trip instead of N.

Plan schema (both stores use the same shape; sections without a key are
treated as a no-op):

    {
        # memory.db plan
        "delete":   ["<uuid>", ...]                          # soft delete
        "delete_hard": ["<uuid>", ...]                       # cascade delete
        "link":     [{"from_id": ..., "to_id": ...,
                      "relationship_type": "related"}, ...]
        "update":   [{"id": ..., "importance": 0.9, ...}, ...]

        # chatlog.db plan
        "decay":    True | {"batch_size": 1000} | False      # run chatlog_decay
        "dedup":    [{"keep_id": ..., "drop_ids": [...]}, ...]
        "promote":  [{"ids": [...], "target_type": "conversation"}, ...]
        "prune":    [{"conversation_id": ..., "reason": "..."}, ...]
    }

Returns a structured dict per section + a summary. No exceptions cross
the boundary; per-section errors surface in the result.

---

## Entry points

- `def main()` (line 319)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `store` | Which store the plan targets. | — |  | str |  |
| `--plan` | Path to a JSON file containing the plan, or '-' to read stdin. | — |  | str |  |
| `--db` | Override DB path (chatlog only; memory uses M3_DATABASE). | None |  | str |  |
| `--pretty` | Pretty-print the result JSON (default: compact one-line). | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `chatlog_config`
- `chatlog_core`
- `chatlog_decay`
- `m3_sdk (active_database)`
- `memory_core`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `db_path`` (line 239)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
