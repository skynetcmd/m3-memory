---
tool: bin/mem0_scan.py
sha1: 208514713eab
mtime_utc: 2026-07-15T18:30:53.088483+00:00
generated_utc: 2026-07-17T02:18:40.726480+00:00
private: false
---

# bin/mem0_scan.py

## Purpose

mem0_scan.py — Scan a codebase for mem0 usage and report the m3 swap.

The m3 LangChain surface (``m3_memory.langchain.Memory``) is a drop-in for
``mem0.Memory``: the import line changes, and mem0-identical calls
(``.add()``/``.search()``/``.get()``/``.delete()``/…) keep working byte-for-byte.
This tool finds every mem0 import + call site in a target tree, tells you which
calls are drop-in vs. which map to an m3-native extra vs. which have no
equivalent, and (with ``--fix``) rewrites the import line in place.

It is AST-based, so it only flags real mem0 usage — not the substring "mem0" in
a comment or unrelated string.

Usage:
    python bin/mem0_scan.py PATH [PATH ...]      # report only
    python bin/mem0_scan.py PATH --fix           # also rewrite import lines
    python bin/mem0_scan.py PATH --json          # machine-readable report

Exit code: 0 if no mem0 usage found, 1 if any found (report), 0 after --fix.

---

## Entry points

- `def main()` (line 296)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `paths` | files or directories to scan | — |  | Path |  |
| `--fix` | rewrite `from mem0 import ...` lines to the m3 import in place | `False` |  | store_true |  |
| `--json` | machine-readable output | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

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
