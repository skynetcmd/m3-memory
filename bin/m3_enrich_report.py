#!/usr/bin/env python3
"""Summarize an m3_enrich run from enrichment_groups + enrichment_runs.

Produces a human-readable report with:
  - status breakdown (pending / success / empty / failed / dead_letter)
  - error_class distribution + sample messages
  - per-size-band success rate (if content_size_k is populated)
  - elapsed wallclock + throughput
  - a clear note when 429 / quota patterns dominate, so the operator
    knows to wait + resume rather than re-run from scratch

Modes:
  --run-id UUID          summarize a specific enrich_run_id
  --variant VARIANT      summarize all rows for a source_variant (any run)
  --target FILE          write markdown to FILE (default: stdout)
  --db PATH              SQLite DB path (default: memory/agent_memory.db
                         or M3_DATABASE env)

Designed to be called automatically at the end of m3_enrich runs so that
every run leaves an artifact behind, mirroring the docs/audits/ pattern
for security scans.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _build_filter(run_id: Optional[str], variant: Optional[str]) -> tuple[str, list]:
    if run_id:
        return ("enrich_run_id = ?", [run_id])
    if variant:
        return ("source_variant = ?", [variant])
    return ("1=1", [])


def _ascii(s: Optional[str], cap: int = 200) -> str:
    if not s:
        return ""
    return s.encode("ascii", "replace").decode()[:cap]


def _summarize(conn: sqlite3.Connection, where: str, params: list) -> dict:
    """Pull the numbers; format() turns them into markdown."""
    cur = conn.cursor()

    # Status breakdown
    cur.execute(f"""
        SELECT status, COUNT(*) AS n
        FROM enrichment_groups
        WHERE {where}
        GROUP BY status
    """, params)
    status: dict[str, int] = {r["status"]: r["n"] for r in cur.fetchall()}
    total = sum(status.values())

    # Error class distribution (failed rows only)
    cur.execute(f"""
        SELECT COALESCE(error_class, '(none)') AS error_class, COUNT(*) AS n
        FROM enrichment_groups
        WHERE {where} AND status='failed'
        GROUP BY error_class
        ORDER BY n DESC
    """, params)
    error_classes: list[tuple[str, int]] = [(r["error_class"], r["n"]) for r in cur.fetchall()]

    # Sample error messages (deduped by class)
    cur.execute(f"""
        SELECT error_class, last_error
        FROM enrichment_groups
        WHERE {where} AND status='failed' AND last_error IS NOT NULL
        ORDER BY id DESC LIMIT 500
    """, params)
    seen_class: set[str] = set()
    samples: list[tuple[str, str]] = []
    for r in cur.fetchall():
        cls = r["error_class"] or "(none)"
        if cls in seen_class:
            continue
        seen_class.add(cls)
        samples.append((cls, _ascii(r["last_error"])))
        if len(samples) >= 10:
            break

    # Size-band success rate
    cur.execute(f"""
        SELECT
          CASE
            WHEN content_size_k IS NULL THEN '(unknown)'
            WHEN content_size_k < 4 THEN '0-4k'
            WHEN content_size_k < 8 THEN '4-8k'
            WHEN content_size_k < 16 THEN '8-16k'
            WHEN content_size_k < 32 THEN '16-32k'
            ELSE '32k+'
          END AS band,
          status,
          COUNT(*) AS n
        FROM enrichment_groups
        WHERE {where}
        GROUP BY band, status
    """, params)
    band_status: dict[str, dict[str, int]] = {}
    for r in cur.fetchall():
        band_status.setdefault(r["band"], {})[r["status"]] = r["n"]

    # Throughput
    cur.execute(f"""
        SELECT MIN(first_attempt_at) AS started,
               MAX(last_attempt_at)  AS ended,
               COUNT(*) AS n_attempted,
               SUM(obs_emitted)      AS total_obs,
               AVG(enrichment_ms)    AS mean_ms
        FROM enrichment_groups
        WHERE {where} AND first_attempt_at IS NOT NULL
    """, params)
    perf = dict(cur.fetchone() or {})

    # Run rows (if any) for profile/model info
    runs: list[dict] = []
    try:
        cur.execute(f"""
            SELECT DISTINCT enrich_run_id, profile, model
            FROM enrichment_groups
            WHERE {where} AND enrich_run_id IS NOT NULL
            ORDER BY enrich_run_id
            LIMIT 20
        """, params)
        runs = [dict(r) for r in cur.fetchall()]
    except sqlite3.OperationalError:
        pass

    return {
        "total": total,
        "status": status,
        "error_classes": error_classes,
        "samples": samples,
        "band_status": band_status,
        "perf": perf,
        "runs": runs,
    }


def _format(data: dict, where_label: str) -> str:
    """Render the summary as markdown."""
    lines: list[str] = []
    lines.append("# m3_enrich run report")
    lines.append("")
    lines.append(f"> Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} · scope: `{where_label}`")
    lines.append("")

    # Headline status
    s = data["status"]
    total = data["total"]
    if total == 0:
        lines.append("No enrichment_groups rows match this scope.")
        return "\n".join(lines)

    succ = s.get("success", 0)
    empty = s.get("empty", 0)
    failed = s.get("failed", 0)
    pending = s.get("pending", 0)
    in_prog = s.get("in_progress", 0)
    dead = s.get("dead_letter", 0)
    succ_pct = 100 * succ / total if total else 0
    fail_pct = 100 * failed / total if total else 0

    lines.append("## Headline")
    lines.append("")
    lines.append(f"- Total groups in scope: **{total:,}**")
    lines.append(f"- Success: **{succ:,}** ({succ_pct:.1f}%)")
    lines.append(f"- Empty (legitimately no facts): {empty:,} ({100 * empty / total:.1f}%)")
    lines.append(f"- Failed: **{failed:,}** ({fail_pct:.1f}%)")
    if dead:
        lines.append(f"- Dead-letter: {dead:,}")
    if pending:
        lines.append(f"- Still pending: {pending:,}")
    if in_prog:
        lines.append(f"- Still in-progress: {in_prog:,} (run was interrupted)")

    obs = data["perf"].get("total_obs") or 0
    if succ:
        lines.append(f"- Observations written: **{obs:,}** "
                     f"(mean {obs / succ:.1f} per successful group)")

    # Cascade detection
    if failed > 0:
        top_err_class, top_err_n = data["error_classes"][0] if data["error_classes"] else ("(none)", 0)
        if top_err_class == "http_status" and top_err_n / max(failed, 1) > 0.5:
            lines.append("")
            lines.append("> ⚠️ **Quota / rate-limit cascade detected.** "
                         f"{top_err_n:,} of {failed:,} failures are `http_status` "
                         "errors — this is the signature of a 429 / quota wall, "
                         "not real per-group bugs. Wait for the upstream quota "
                         "reset, then re-run with `--resume` to retry these.")

    # Throughput
    lines.append("")
    lines.append("## Throughput")
    lines.append("")
    p = data["perf"]
    started = p.get("started") or "(none)"
    ended = p.get("ended") or "(none)"
    n_attempted = p.get("n_attempted") or 0
    mean_ms = p.get("mean_ms") or 0
    lines.append(f"- Window: `{started}` → `{ended}`")
    lines.append(f"- Groups attempted: {n_attempted:,}")
    if mean_ms:
        lines.append(f"- Mean per-group enrichment time: {mean_ms / 1000:.2f}s")
    if started and ended and started != "(none)":
        try:
            t0 = datetime.fromisoformat(started.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(ended.replace("Z", "+00:00"))
            wall = (t1 - t0).total_seconds()
            if wall > 0 and n_attempted:
                rate = n_attempted / wall
                lines.append(f"- Wallclock: {wall / 60:.1f} min "
                             f"(effective rate {rate:.2f} g/s)")
        except (ValueError, TypeError):
            pass

    # Error classes
    if data["error_classes"]:
        lines.append("")
        lines.append("## Failure breakdown")
        lines.append("")
        lines.append("| error_class | count | % of failed |")
        lines.append("|---|---:|---:|")
        for cls, n in data["error_classes"]:
            pct = 100 * n / failed if failed else 0
            lines.append(f"| `{cls}` | {n:,} | {pct:.1f}% |")
        lines.append("")
        lines.append("### Sample messages")
        lines.append("")
        for cls, msg in data["samples"]:
            lines.append(f"- `{cls}`: `{_ascii(msg, 200)}`")

    # Size band table
    if data["band_status"]:
        lines.append("")
        lines.append("## Per-size-band outcome")
        lines.append("")
        lines.append("| band | total | success | empty | failed | success rate |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        bands = ["0-4k", "4-8k", "8-16k", "16-32k", "32k+", "(unknown)"]
        for b in bands:
            if b not in data["band_status"]:
                continue
            row = data["band_status"][b]
            t = sum(row.values())
            ss = row.get("success", 0)
            ee = row.get("empty", 0)
            ff = row.get("failed", 0)
            sr = 100 * ss / t if t else 0
            lines.append(f"| {b} | {t:,} | {ss:,} | {ee:,} | {ff:,} | {sr:.1f}% |")

    # Run metadata
    if data["runs"]:
        lines.append("")
        lines.append("## Runs in scope")
        lines.append("")
        for r in data["runs"]:
            rid = (r.get("enrich_run_id") or "")[:8]
            lines.append(f"- `{rid}` profile=`{r.get('profile')}` model=`{r.get('model')}`")

    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=os.environ.get("M3_DATABASE", "memory/agent_memory.db"),
                    help="SQLite DB path. Default: memory/agent_memory.db or $M3_DATABASE.")
    ap.add_argument("--run-id", help="enrich_run_id to summarize (UUID).")
    ap.add_argument("--variant", help="source_variant to summarize across all runs.")
    ap.add_argument("--target", help="Output file (default: stdout).")
    args = ap.parse_args()

    if not (args.run_id or args.variant):
        ap.error("must pass --run-id or --variant")

    if not Path(args.db).exists():
        print(f"ERROR: db not found: {args.db}", file=sys.stderr)
        return 1

    conn = _connect(args.db)
    where, params = _build_filter(args.run_id, args.variant)
    data = _summarize(conn, where, params)
    label = f"run_id={args.run_id}" if args.run_id else f"variant={args.variant}"
    md = _format(data, label)

    if args.target:
        Path(args.target).parent.mkdir(parents=True, exist_ok=True)
        Path(args.target).write_text(md, encoding="utf-8")
        print(f"wrote {args.target}", file=sys.stderr)
    else:
        sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
