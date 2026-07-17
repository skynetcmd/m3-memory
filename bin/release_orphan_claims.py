#!/usr/bin/env python3
"""release_orphan_claims — safely release stuck in_progress enrichment_groups rows.

Use after a worker process crashes mid-batch and leaves rows claimed but
unfinalized. Provides three filtering modes to avoid sniping live workers'
claims:

  --run-id <id>       Release all in_progress rows belonging to this run.
                      Safe ONLY when you've confirmed the run's process is
                      dead (tasklist | grep python).

  --older-than <min>  Release rows whose claimed_at is older than N minutes.
                      Heuristic for "definitely abandoned." Default cutoff
                      should exceed the longest legitimate in_progress
                      window — typically batch-poll cadence × max-poll-count.
                      For our Anthropic batches: ~60-120 min is the sweet spot.

  --dry-run           Show what would be released without committing.

  --skip-qps-done     Defensive: do NOT release a row if its
                      question_pipeline_state.result is already in done_text /
                      done_empty / failed. This prevents the "release-back-to-
                      pending" reverse-drift bug where a previously-terminal
                      qps row gets re-flagged because of a worker crash.

Default mode requires explicit user confirmation.

Usage:
    python bin/release_orphan_claims.py --db memory/your-corpus.db \\
        --older-than 120 --skip-qps-done

    python bin/release_orphan_claims.py --db memory/your-corpus.db \\
        --run-id <enrichment_runs.id>
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="Path to the SQLite DB.")
    ap.add_argument("--run-id", default=None,
                    help="Release rows where enrich_run_id = this value.")
    ap.add_argument("--older-than", type=int, default=None,
                    help="Release rows where claimed_at older than N minutes.")
    ap.add_argument("--all", action="store_true",
                    help="Release ALL in_progress rows (DANGEROUS — only use "
                         "when no live workers exist).")
    ap.add_argument("--skip-qps-done", action="store_true",
                    help="Skip rows whose question_pipeline_state already says "
                         "done_text/done_empty/failed (prevents reverse-drift).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Preview without committing.")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="Skip the interactive confirm prompt.")
    args = ap.parse_args()

    if not (args.run_id or args.older_than or args.all):
        sys.exit("ERROR: must specify one of --run-id, --older-than, or --all")

    if sum(map(bool, [args.run_id, args.older_than, args.all])) > 1:
        sys.exit("ERROR: --run-id, --older-than, and --all are mutually exclusive")

    db = Path(args.db).resolve()
    if not db.exists():
        sys.exit(f"ERROR: db not found: {db}")

    # Route through the storage seam so this reads/writes the LIVE core store on
    # both backends (on PG-primary a raw sqlite3.connect would hit a stale file).
    import memory_core as mc
    from m3_sdk import active_database
    from memory.backends import dialect

    with active_database(str(db)):
        _d = dialect()
        _p = _d.param()

        # Build the WHERE clause
        where = "status='in_progress'"
        params: list = []
        desc_lines: list[str] = []

        if args.run_id:
            where += f" AND enrich_run_id = {_p}"
            params.append(args.run_id)
            desc_lines.append(f"  enrich_run_id = {args.run_id}")
        elif args.older_than:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=args.older_than)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            where += f" AND claimed_at < {_p}"
            params.append(cutoff)
            desc_lines.append(f"  claimed_at < {cutoff}  (older than {args.older_than} min)")
        elif args.all:
            desc_lines.append("  ALL in_progress rows")

        if args.skip_qps_done:
            # Defensive: only release if qps says pending or failed (not already terminal)
            where += """ AND NOT EXISTS (
                SELECT 1 FROM question_pipeline_state q
                WHERE q.convo_id = enrichment_groups.group_key
                  AND q.result IN ('done_text','done_empty')
            )"""
            desc_lines.append("  AND qps.result NOT IN (done_text,done_empty)")

        with mc._db() as conn:
            # Preview
            cnt = conn.execute(
                f"SELECT COUNT(*) FROM enrichment_groups WHERE {where}",
                params,
            ).fetchone()[0]
            print("=== release_orphan_claims preview ===")
            print(f"DB: {db}")
            print("Filters:")
            for line in desc_lines:
                print(line)
            print(f"Rows that would be released: {cnt}")

            if cnt == 0:
                print("Nothing to do.")
                return 0

            if args.dry_run:
                print("--dry-run: not committing.")
                return 0

            if not args.yes:
                try:
                    ans = input(f"Release {cnt} rows? [y/N]: ").strip().lower()
                except EOFError:
                    ans = "n"
                if ans != "y":
                    print("Aborted.")
                    return 1

            cur = conn.execute(
                f"""UPDATE enrichment_groups
                    SET status='pending', claim_token=NULL, claimed_at=NULL,
                        enrich_run_id=NULL
                    WHERE {where}""",
                params,
            )
            conn.commit()
            print(f"Released {cur.rowcount} rows.")

            # If --run-id was given, also try to mark the run row as aborted (cosmetic
            # — keeps enrichment_runs audit clean).
            if args.run_id:
                n = conn.execute(
                    f"""UPDATE enrichment_runs
                       SET finished_at={_p}, status='aborted',
                           abort_reason=COALESCE(abort_reason,'orphan_release')
                       WHERE id={_p} AND finished_at IS NULL""",
                    (_utcnow_iso(), args.run_id),
                ).rowcount
                conn.commit()
                if n:
                    print(f"Also marked run {args.run_id} as aborted in enrichment_runs.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
