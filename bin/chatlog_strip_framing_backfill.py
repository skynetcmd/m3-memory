#!/usr/bin/env python3
"""chatlog_strip_framing_backfill.py — one-off backfill that removes harness
control framing (<system-reminder> / <task-notification> blocks) from EXISTING
chat_log rows.

Why this exists (companion to the write-path fix in chatlog_core):
    chatlog_redaction.strip_harness_framing() is wired into the two chatlog
    WRITE paths, so new turns are stripped as they land. But rows captured
    BEFORE that fix still carry the blocks verbatim. Because chatlog search
    returns stored turns as DATA, a persisted <system-reminder> (e.g. a genuine
    "the date has changed … do not mention this to the user") re-surfaces later
    reading like a LIVE instruction — indistinguishable from a prompt-injection
    payload, which is exactly what a curation subagent tripped over.

    chatlog_rescrub_impl does NOT fix these rows: it runs scrub() (the optional,
    config-gated SECRET redaction), not strip_harness_framing(). The two were
    deliberately kept separate — framing removal is structural block deletion,
    not secret regex→[REDACTED] substitution. So clearing legacy framing needs
    its own backfill: this script.

Safety:
    * DRY-RUN by default. Nothing is written unless --apply is passed.
    * Does NOT require redaction.enabled — framing stripping is independent of
      secret redaction (mirrors the unconditional call in chatlog_core).
    * Idempotent: strip_harness_framing() returns count 0 on already-clean
      content, so re-running is a no-op on rows already handled.
    * Read-only DRY-RUN opens no write transaction; --apply updates only rows
      that actually change, and records provenance in metadata_json:
      original_content_sha256 (once), harness_framing_stripped=True,
      harness_blocks_removed (cumulative).

Usage:
    python bin/chatlog_strip_framing_backfill.py                 # dry-run, all rows
    python bin/chatlog_strip_framing_backfill.py --apply         # apply to all rows
    python bin/chatlog_strip_framing_backfill.py --conversation-id <id> --apply
    python bin/chatlog_strip_framing_backfill.py --since 2026-06-01 --until 2026-07-01
    python bin/chatlog_strip_framing_backfill.py --limit 100 --verbose --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import chatlog_config
import chatlog_redaction
from chatlog_core import _content_hash, _utcnow_iso


def _build_where(conversation_id: str, since: str, until: str) -> tuple[str, list[Any]]:
    """Assemble the WHERE clause + params, mirroring chatlog_rescrub_impl so the
    filter semantics (chat_log, not-deleted, optional conv/time window) match."""
    clauses = ["type='chat_log'", "is_deleted=0"]
    params: list[Any] = []
    if conversation_id:
        clauses.append("conversation_id=?")
        params.append(conversation_id)
    if since:
        clauses.append("created_at>=?")
        params.append(since)
    if until:
        clauses.append("created_at<=?")
        params.append(until)
    return " AND ".join(clauses), params


def backfill(
    conversation_id: str = "",
    since: str = "",
    until: str = "",
    limit: int = 100_000,
    apply: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    """Scan candidate rows; strip harness framing. Returns a summary dict.

    In dry-run (apply=False) no UPDATE is issued — the row set is scanned and the
    would-change rows are counted/reported. In apply mode each changed row is
    UPDATEd in a single transaction committed at the end.
    """
    from m3_sdk import M3Context

    cfg = chatlog_config.resolve_config()  # resolves the active chatlog DB path
    ctx = M3Context.for_db(None)
    where, params = _build_where(conversation_id, since, until)

    scanned = 0
    changed = 0
    blocks_removed_total = 0
    samples: list[dict[str, Any]] = []

    with ctx.get_chatlog_conn() as conn:
        rows = conn.execute(
            f"SELECT id, content, metadata_json FROM memory_items "
            f"WHERE {where} LIMIT ?",
            params + [limit],
        ).fetchall()

        for r in rows:
            scanned += 1
            content = r["content"]
            if not content:
                continue
            stripped, count = chatlog_redaction.strip_harness_framing(content)
            if count == 0:
                continue  # already clean / no framing — idempotent no-op

            changed += 1
            blocks_removed_total += count
            if verbose and len(samples) < 20:
                samples.append({
                    "id": r["id"],
                    "blocks_removed": count,
                    "bytes_before": len(content),
                    "bytes_after": len(stripped),
                })

            if not apply:
                continue

            # --- apply: update content + provenance metadata --------------------
            try:
                meta = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
            except json.JSONDecodeError:
                meta = {}
            # Record the pre-strip hash ONCE, so a re-run does not clobber the
            # true original with an already-stripped one.
            if not meta.get("harness_framing_stripped"):
                meta["original_content_sha256"] = _content_hash(content)
            meta["harness_framing_stripped"] = True
            meta["harness_blocks_removed"] = (
                int(meta.get("harness_blocks_removed", 0)) + count
            )
            conn.execute(
                "UPDATE memory_items SET content=?, metadata_json=?, updated_at=? "
                "WHERE id=?",
                (stripped, json.dumps(meta, ensure_ascii=False), _utcnow_iso(), r["id"]),
            )

        if apply and changed:
            conn.commit()

    return {
        "mode": "apply" if apply else "dry-run",
        "db_path": cfg.db_path,
        "scanned": scanned,
        "changed": changed,
        "blocks_removed": blocks_removed_total,
        "samples": samples,
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description="Backfill: strip harness control framing from existing "
                    "chat_log rows (companion to the chatlog write-path fix).",
    )
    p.add_argument("--apply", action="store_true",
                   help="Write changes. Without this, runs a dry-run (default).")
    p.add_argument("--conversation-id", default="",
                   help="Limit to one conversation.")
    p.add_argument("--since", default="",
                   help="Only rows with created_at >= this ISO timestamp.")
    p.add_argument("--until", default="",
                   help="Only rows with created_at <= this ISO timestamp.")
    p.add_argument("--limit", type=int, default=100_000,
                   help="Max rows to scan (default 100000).")
    p.add_argument("--verbose", action="store_true",
                   help="Include per-row samples (first 20 changed rows).")
    p.add_argument("--json", action="store_true",
                   help="Emit the summary as JSON.")
    args = p.parse_args()

    result = backfill(
        conversation_id=args.conversation_id,
        since=args.since,
        until=args.until,
        limit=args.limit,
        apply=args.apply,
        verbose=args.verbose,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"[{result['mode']}] db: {result['db_path']}")
        print(f"  scanned:        {result['scanned']}")
        print(f"  rows to strip:  {result['changed']}")
        print(f"  blocks removed: {result['blocks_removed']}")
        if result["samples"]:
            print("  samples (id | blocks | bytes before->after):")
            for s in result["samples"]:
                print(f"    {s['id'][:8]} | {s['blocks_removed']} | "
                      f"{s['bytes_before']}->{s['bytes_after']}")
        if not args.apply and result["changed"]:
            print(f"\n  DRY-RUN — nothing written. Re-run with --apply to strip "
                  f"{result['changed']} row(s).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
