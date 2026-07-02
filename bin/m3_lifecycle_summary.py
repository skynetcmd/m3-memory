#!/usr/bin/env python3
"""CLI wrapper for the memory lifecycle/contradiction observability summary.

Thin operator-facing surface over ``memory_maintenance.memory_lifecycle_summary_impl``
— the SAME function the ``memory_lifecycle_summary`` MCP tool calls, so the agent
and the operator see identical numbers. Read-only.

    python bin/m3_lifecycle_summary.py                 # last 7 days, human table
    python bin/m3_lifecycle_summary.py --window-days 30 --json
    python bin/m3_lifecycle_summary.py --top-n 10
"""
import argparse
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "bin"))


def _format_human(summary: dict) -> str:
    ev = summary["events"]
    corr = summary["corroboration"]
    lines = [
        f"Lifecycle summary — last {summary['window_days']} day(s)",
        "",
        "Events:",
        f"  create     {ev['create']:>6}",
        f"  update     {ev['update']:>6}",
        f"  delete     {ev['delete']:>6}",
        f"  supersede  {ev['supersede']:>6}",
        "",
        "Corroboration:",
        f"  corroborated  {corr['corroborated']:>6}",
        f"  contradicted  {corr['contradicted']:>6}",
    ]
    if summary["most_revised"]:
        lines += ["", "Most revised:"]
        for r in summary["most_revised"]:
            title = (r["title"] or "").strip() or "(untitled)"
            lines.append(f"  {r['revisions']:>3}x  {r['memory_id'][:8]}  {title[:60]}")
    if summary["top_contradicted"]:
        lines += ["", "Most contradicted:"]
        for r in summary["top_contradicted"]:
            title = (r["title"] or "").strip() or "(untitled)"
            lines.append(f"  {r['contradiction_count']:>3}x  {r['memory_id'][:8]}  {title[:60]}")
    return "\n".join(lines)


def main() -> int:
    from m3_sdk import add_database_arg
    import memory_maintenance

    parser = argparse.ArgumentParser(
        description="m3-memory lifecycle & contradiction summary (read-only).",
    )
    add_database_arg(parser)
    parser.add_argument("--window-days", type=int, default=7, help="Look-back window in days (default 7).")
    parser.add_argument("--top-n", type=int, default=5, help="Rows in the most-revised/contradicted lists (0 = omit).")
    parser.add_argument("--json", action="store_true", help="Emit raw JSON instead of a human table.")
    args = parser.parse_args()

    summary = memory_maintenance.memory_lifecycle_summary_impl(
        window_days=args.window_days, top_n=args.top_n
    )
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(_format_human(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
