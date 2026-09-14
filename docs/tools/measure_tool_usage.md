---
tool: bin/measure_tool_usage.py
sha1: 79f2e8bbbd32
mtime_utc: 2026-09-14T03:08:55.981570+00:00
generated_utc: 2026-09-14T03:08:59.523934+00:00
private: false
---

# bin/measure_tool_usage.py

## Purpose

Freeze the m3 tool-usage baseline from agent transcripts.

P0 of the slim-tools work. One script, run once, prints the whole baseline --
no manual collation, no second pass, no hand-edited numbers. Re-running it on
the same transcripts reproduces the same output.

REPRODUCIBLE, NOT FROZEN: the counts grow while a session is live, because the
running agent appends to the transcript this script reads -- measuring it
changes it. Two runs minutes apart legitimately differ (observed: delegate
26 -> 27 across one edit). That is the observer effect, not nondeterminism.
Pin a comparison with --since, or diff against a saved --json artifact.

WHY A SCRIPT AND NOT A COUNT IN A DOC: the first two attempts at this number
were both wrong, in ways a frozen figure would have hidden.

  * A getsource()-based sweep silently skipped every tool in the catalog
    (LazyImpl wrappers have no source) and reported a confident zero.
  * A transcript count of 391 calls missed every tool invoked THROUGH m3_call
    and every tool invoked from the CLI, undercounting the delegated surface.

THREE SURFACES, COUNTED SEPARATELY. They are different questions and a single
total hides the one that matters:

  direct   -- mcp__m3_memory__<tool> as its own tool_use block. The startup
              surface: these are the tools whose schemas are loaded eagerly.
  delegate -- a tool named in an m3_call payload ({"tool": "x"} or a batch).
              Reached WITHOUT a loaded schema, so usage here is evidence the
              proxy works, not evidence the tool needs promoting.
  cli      -- `m3 <domain> <tool>` in a Bash command string. Invisible to any
              MCP-level count; a tool used only here looks unused.

A tool with zero DIRECT calls but heavy DELEGATE use is not unused -- it is
being reached the cheap way, which is the outcome the slim plan wants. Demoting
it on a merged total would be backwards. That distinction is the point of this
script.

Usage:
    python bin/measure_tool_usage.py                 # human-readable
    python bin/measure_tool_usage.py --json          # machine-readable
    python bin/measure_tool_usage.py --since 2026-08-01
    python bin/measure_tool_usage.py --transcripts DIR

---

## Entry points

- `def main()` (line 253)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--transcripts` | f'transcript root (default: {default_dir})' | `default_dir` |  | Path |  |
| `--since` | ignore calls before this YYYY-MM-DD | — |  | str |  |
| `--json` | emit JSON | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `mcp_tool_catalog`

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
