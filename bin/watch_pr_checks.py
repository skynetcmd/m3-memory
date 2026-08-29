#!/usr/bin/env python3
"""Watch CI checks on one or more PRs until every check is terminal, then print a
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
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time


def _query_pr(pr: str, repo: str | None) -> "list | None":
    """Return gh's check list for a PR, or None on ANY problem (missing gh, auth
    failure, empty output, non-JSON, wrong shape). None means UNKNOWN — the
    caller must never read it as green. This is the guard the incident needed."""
    cmd = ["gh", "pr", "checks", pr, "--json", "name,state,bucket"]
    if repo:
        cmd += ["--repo", repo]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None  # gh not found / hung → UNKNOWN, not green
    if out.returncode != 0 or not out.stdout.strip():
        return None  # gh errored or produced nothing → UNKNOWN
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or not data:
        return None  # empty array (no checks yet) is UNKNOWN, not "green"
    return data


def _summarize(checks: "list | None") -> str:
    """One-token state for a PR: 'UNKNOWN' | 'green' | 'failed' | 'running'."""
    if checks is None:
        return "UNKNOWN"
    buckets = [c.get("bucket") for c in checks]
    if any(b == "fail" for b in buckets):
        return "failed"
    if any(b == "pending" for b in buckets):
        return "running"
    if all(b == "pass" for b in buckets):
        return "green"
    # Any bucket we don't recognize → treat as not-yet-terminal, never green.
    return "running"


def _counts(checks: "list | None") -> str:
    if checks is None:
        return "unknown"
    n = {"pass": 0, "fail": 0, "pending": 0}
    for c in checks:
        b = c.get("bucket")
        if b in n:
            n[b] += 1
    return f"pass={n['pass']} fail={n['fail']} pending={n['pending']}"


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("prs", nargs="+", help="PR numbers to watch")
    ap.add_argument("--repo", default=None, help="owner/name (else current repo)")
    ap.add_argument("--interval", type=float, default=45.0)
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--once", action="store_true",
                    help="Poll once and exit (for tests / a quick check).")
    args = ap.parse_args(argv)

    start = time.monotonic()
    while True:
        stamp = time.strftime("%H:%M:%SZ", time.gmtime())
        states = {}
        for pr in args.prs:
            checks = _query_pr(pr, args.repo)
            states[pr] = (_summarize(checks), _counts(checks))
        line = stamp + "  " + "  ".join(
            f"PR#{pr}[{st} {cn}]" for pr, (st, cn) in states.items())
        print(line, flush=True)

        vals = [st for st, _ in states.values()]
        # Terminal ONLY when every PR is a real terminal signal (green|failed).
        # A single UNKNOWN or running keeps us waiting — the core guarantee.
        if all(v in ("green", "failed") for v in vals):
            if any(v == "failed" for v in vals):
                print("RESULT: FAILED", flush=True)
                return 1
            print("RESULT: ALL-GREEN", flush=True)
            return 0
        if args.once:
            print("RESULT: NOT-TERMINAL (--once)", flush=True)
            return 2
        if time.monotonic() - start >= args.timeout:
            print(f"RESULT: TIMEOUT after {args.timeout:.0f}s "
                  "(never reached a terminal state)", flush=True)
            return 2
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
