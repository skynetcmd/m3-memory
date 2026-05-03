#!/usr/bin/env python3
"""
m3_enrich_assign.py — assign enrichment_groups.send_to for routed runs.

Updates the `send_to` column on rows matching a predicate, so that a
later `m3_enrich.py --send-to <name>` run claims only its assigned
rows. Use when running multiple providers (e.g. Grok + Gemini) against
the same source variant in parallel and you want disjoint pools by
explicit assignment rather than by accidental bucket isolation.

Common patterns:

    # Route all groups <= 7 KB to "grok"
    m3_enrich_assign --db memory/agent_test_bench.db \\
                     --source-variant LME-M-ingestion \\
                     --target-variant m3-observations-bench-LME-M-ingestion-20260428 \\
                     --max-size-k 7 --send-to grok

    # Route the rest (>= 16 KB) to "gemini", leaving the 8-15 KB middle
    # band unassigned (NULL) for later
    m3_enrich_assign --db memory/agent_test_bench.db \\
                     --source-variant LME-M-ingestion \\
                     --target-variant m3-observations-bench-LME-M-ingestion-20260428 \\
                     --min-size-k 16 --send-to gemini

    # Dry-run first
    m3_enrich_assign ... --send-to grok --dry-run

The script only touches `pending` and `failed` rows by default — it
won't reassign rows that already succeeded. Use --include-completed to
override.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, type=Path,
                    help="Path to the database with enrichment_groups.")
    ap.add_argument("--source-variant", required=True,
                    help="Match enrichment_groups.source_variant.")
    ap.add_argument("--target-variant", required=True,
                    help="Match enrichment_groups.target_variant.")
    ap.add_argument("--send-to", required=True,
                    help="Provider name to assign (e.g. 'grok', 'gemini'). "
                         "Pass the literal string 'NULL' to clear assignments.")
    ap.add_argument("--min-size-k", type=int,
                    help="Only assign rows whose content_size_k >= N.")
    ap.add_argument("--max-size-k", type=int,
                    help="Only assign rows whose content_size_k <= N.")
    ap.add_argument("--only-unassigned", action="store_true",
                    help="Only assign rows where send_to IS NULL. Use to "
                         "avoid clobbering an existing routing scheme.")
    ap.add_argument("--include-completed", action="store_true",
                    help="Also reassign rows in success/empty/dead_letter "
                         "status. Default: only pending and failed.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be updated; don't write.")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"ERROR: database not found: {args.db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(args.db), timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")

    # Verify the column exists (migration 031 applied).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(enrichment_groups)")}
    if "send_to" not in cols:
        print("ERROR: enrichment_groups.send_to does not exist on this DB. "
              "Apply migration 031 first: "
              f"python bin/migrate_memory.py --db {args.db} up", file=sys.stderr)
        return 2

    # Build the WHERE clause.
    where = ["source_variant = ?", "target_variant = ?"]
    params: list = [args.source_variant, args.target_variant]
    if args.min_size_k is not None:
        where.append("content_size_k >= ?")
        params.append(args.min_size_k)
    if args.max_size_k is not None:
        where.append("content_size_k <= ?")
        params.append(args.max_size_k)
    if args.only_unassigned:
        where.append("send_to IS NULL")
    if not args.include_completed:
        where.append("status IN ('pending', 'failed')")
    where_sql = " AND ".join(where)

    # Preview: count + size-band breakdown.
    n_match = conn.execute(
        f"SELECT COUNT(*) FROM enrichment_groups WHERE {where_sql}", params,
    ).fetchone()[0]

    if n_match == 0:
        print("0 rows match. Nothing to assign.")
        return 0

    print(f"Matched: {n_match} rows")
    print()
    print("Status breakdown:")
    for r in conn.execute(
        f"SELECT status, COUNT(*) FROM enrichment_groups WHERE {where_sql} "
        f"GROUP BY status ORDER BY 2 DESC", params,
    ):
        print(f"  {r[0]:<15} {r[1]}")
    print()
    print("Current send_to breakdown (pre-update):")
    for r in conn.execute(
        f"SELECT send_to, COUNT(*) FROM enrichment_groups WHERE {where_sql} "
        f"GROUP BY send_to ORDER BY 2 DESC", params,
    ):
        label = r[0] if r[0] is not None else "(NULL)"
        print(f"  {label:<15} {r[1]}")
    print()

    new_value: str | None = None if args.send_to.upper() == "NULL" else args.send_to
    print(f"New send_to value: {new_value!r}")

    if args.dry_run:
        print("(dry-run: no changes written)")
        return 0

    # Apply.
    n_updated = conn.execute(
        f"UPDATE enrichment_groups SET send_to = ? WHERE {where_sql}",
        [new_value, *params],
    ).rowcount
    conn.commit()
    print(f"Updated {n_updated} rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
