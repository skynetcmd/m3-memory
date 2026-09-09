---
tool: bin/watch_pr_checks.py
sha1: e2908414e166
mtime_utc: 2026-09-08T23:37:05.252666+00:00
generated_utc: 2026-09-08T23:37:13.978705+00:00
private: false
---

# bin/watch_pr_checks.py

## Purpose

Watch CI checks on one or more PRs until every check is terminal, then print a
PASS/FAIL summary and exit 0 (all green) / 1 (any failed) / 2 (timeout) / 3 (bad
setup).

WHY THIS EXISTS — the silent-success trap (incident 2026-07-24)
---------------------------------------------------------------
An ad-hoc monitor loop reported "ALL-GREEN" TWICE while the test job was still
IN_PROGRESS. Two compounding causes, both about a tool being *silently absent*:

  1. `gh pr checks` returned EMPTY in the monitor's shell (auth/PATH differed),
     and the loop treated "no failure signal" as "done + passed".
  2. A first rewrite used `jq` — which is NOT installed on the Windows dev box at
     all — so it would exit before ever running. `gh` + Python (always present)
     is the portable combination here.

The invariant this script enforces, and the reason it is Python not shell:

    DECLARE A TERMINAL/GREEN STATE ONLY ON A POSITIVE SIGNAL.

An empty, failed, or unparseable query is UNKNOWN — never done, never green. The
loop keeps waiting and SAYS "unknown", so a query problem can never masquerade as
success. "Green" requires a real 'pass' bucket for every check from gh; "failed"
requires a real 'fail' bucket. Uses only the stdlib (subprocess/json) so it runs
wherever Python + gh do.

Usage:
    python bin/watch_pr_checks.py <pr> [<pr> ...] [--repo owner/name]
                                  [--interval SECS] [--timeout SECS] [--once]

---

## Entry points

- `def main()` (line 87)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `prs` | PR numbers to watch | — |  | str |  |
| `--repo` | owner/name (else current repo) | None |  | str |  |
| `--interval` |  | `45.0` |  | float |  |
| `--timeout` |  | `1800.0` |  | float |  |
| `--once` | Poll once and exit (for tests / a quick check). | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `cmd`` (line 47)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
