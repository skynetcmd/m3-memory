#!/usr/bin/env python3
"""split_chatlog_from_core — move chat_log rows out of the CORE memory DB into
the dedicated CHATLOG DB, making the two stores functionally separate.

Use this when a prior "integrated" configuration (CHATLOG_DB_PATH pointed at the
main memory DB) has accumulated `type='chat_log'` rows inside agent_memory.db and
you now want them in agent_chatlog.db, with the core store holding memory only.

The copy is done by EXPLICIT SHARED-COLUMN INTERSECTION, not `SELECT *`: the core
schema typically carries enrichment/belief/KG columns (source_group_id, pinned,
belief_alpha, vector_kind, ...) that the leaner chatlog schema neither has nor
needs. Copying positionally would corrupt the insert; copying the shared columns
drops the enrichment-only fields, which is correct for a chatlog store.

Order of operations (only under --commit):
  copy memory_items  (shared cols, INSERT OR IGNORE — idempotent)
  copy memory_embeddings for those rows (shared cols)
  rebuild target FTS
  VERIFY target row count >= source count   ← delete is skipped on mismatch
  delete chat_log rows (+ their embeddings) from source
VACUUM of the source is intentionally left to the operator (slow, locks the DB).

USAGE
=====

    # Dry run — print the plan and counts, write nothing (default).
    python bin/split_chatlog_from_core.py

    # Execute the move (backs nothing up for you — take backups first).
    python bin/split_chatlog_from_core.py --commit

    # Explicit paths (override all env/default resolution).
    python bin/split_chatlog_from_core.py \
        --source /path/to/agent_memory.db \
        --target /path/to/agent_chatlog.db --commit

DB SELECTION
============

Source (CORE, where chat_log rows currently live), in priority order:
  1. --source <path>
  2. $M3_DATABASE env var
  3. <engine_root>/agent_memory.db   (via m3_core.paths.resolve_engine_file)

Target (CHATLOG, where they should go), in priority order:
  1. --target <path>
  2. $M3_CHATLOG_DB_PATH / $CHATLOG_DB_PATH / legacy $CHATLOG_DB env var
  3. <engine_root>/agent_chatlog.db  (via m3_core.paths.resolve_engine_file)

The target must already carry the chatlog schema (memory_items,
memory_embeddings, memory_items_fts). A fresh engine root created by the
installer/homecoming already does; if yours does not, bootstrap it with the
chatlog migrations before running this.

SAFETY
======

Refuses to run if --source and --target resolve to the same file (that would be
a no-op "integrated" layout, not a split). The delete step is gated on a
post-copy count check and is skipped — leaving source intact — on any shortfall.
Take a filesystem backup of both DBs before --commit; this script does not.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

from m3_sdk import getenv_compat

try:
    from m3_core.paths import resolve_engine_file
except ImportError:  # pragma: no cover - direct-run fallback
    from m3_sdk import resolve_engine_file  # type: ignore


# ── DB resolution ──────────────────────────────────────────────────────────
def resolve_source(cli_arg: str | None) -> str:
    if cli_arg:
        return os.path.abspath(cli_arg)
    env = os.environ.get("M3_DATABASE")
    if env:
        return os.path.abspath(env)
    return os.path.abspath(resolve_engine_file("agent_memory.db"))


def resolve_target(cli_arg: str | None) -> str:
    if cli_arg:
        return os.path.abspath(cli_arg)
    env = (os.environ.get("CHATLOG_DB")
           or getenv_compat("M3_CHATLOG_DB_PATH", "CHATLOG_DB_PATH"))
    if env:
        return os.path.abspath(env)
    return os.path.abspath(resolve_engine_file("agent_chatlog.db"))


def shared_cols(src: str, tgt: str, table: str) -> list[str]:
    """Columns present in BOTH tables, in the source's declared order."""
    def cols(p: str) -> list[str]:
        c = sqlite3.connect(p)
        try:
            return [row[1] for row in c.execute(f"PRAGMA table_info({table})")]
        finally:
            c.close()
    tgt_set = set(cols(tgt))
    return [c for c in cols(src) if c in tgt_set]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source", help="CORE memory DB (default: $M3_DATABASE or engine agent_memory.db)")
    ap.add_argument("--target", help="CHATLOG DB (default: $CHATLOG_DB_PATH or engine agent_chatlog.db)")
    ap.add_argument("--commit", action="store_true",
                    help="actually copy + delete (default: dry-run)")
    args = ap.parse_args()

    source = resolve_source(args.source)
    target = resolve_target(args.target)

    print(f"source (CORE):    {source}")
    print(f"target (CHATLOG): {target}")

    if not os.path.exists(source):
        print("ERROR: source DB does not exist.", file=sys.stderr)
        return 2
    if not os.path.exists(target):
        print("ERROR: target DB does not exist (bootstrap its chatlog schema first).", file=sys.stderr)
        return 2
    if os.path.realpath(source) == os.path.realpath(target):
        print("ERROR: source and target are the same file — that is an integrated "
              "layout, not a split. Nothing to do.", file=sys.stderr)
        return 2

    item_cols = shared_cols(source, target, "memory_items")
    emb_cols = shared_cols(source, target, "memory_embeddings")
    print(f"shared memory_items cols: {len(item_cols)}")
    print(f"shared memory_embeddings cols: {len(emb_cols)}")

    core = sqlite3.connect(source, timeout=30)
    try:
        n_items = core.execute(
            "SELECT COUNT(*) FROM memory_items WHERE type='chat_log'").fetchone()[0]
        n_emb = core.execute(
            "SELECT COUNT(*) FROM memory_embeddings WHERE memory_id IN "
            "(SELECT id FROM memory_items WHERE type='chat_log')").fetchone()[0]
        print(f"chat_log rows to move: {n_items}")
        print(f"their embeddings to move: {n_emb}")

        if n_items == 0:
            print("Nothing to move — source has no chat_log rows.")
            return 0

        if not args.commit:
            print("\nDRY-RUN. Re-run with --commit to execute. "
                  "Take backups of both DBs first.")
            return 0

        core.execute("ATTACH DATABASE ? AS tgt", (target,))
        ic, ec = ",".join(item_cols), ",".join(emb_cols)
        core.execute(
            f"INSERT OR IGNORE INTO tgt.memory_items ({ic}) "
            f"SELECT {ic} FROM main.memory_items WHERE type='chat_log'")
        core.execute(
            f"INSERT OR IGNORE INTO tgt.memory_embeddings ({ec}) "
            f"SELECT {ec} FROM main.memory_embeddings WHERE memory_id IN "
            f"(SELECT id FROM main.memory_items WHERE type='chat_log')")
        core.commit()

        moved = core.execute(
            "SELECT COUNT(*) FROM tgt.memory_items WHERE type='chat_log'").fetchone()[0]
        print(f"\nAfter copy — target chat_log rows: {moved}")

        try:
            core.execute("INSERT INTO tgt.memory_items_fts(memory_items_fts) VALUES('rebuild')")
            core.commit()
            print("target FTS rebuilt")
        except sqlite3.Error as e:
            print(f"FTS rebuild note (non-fatal): {e}")

        if moved < n_items:
            print(f"\n*** ABORT: target has {moved}/{n_items} rows. "
                  f"Source left INTACT. ***", file=sys.stderr)
            return 1

        core.execute(
            "DELETE FROM memory_embeddings WHERE memory_id IN "
            "(SELECT id FROM memory_items WHERE type='chat_log')")
        core.execute("DELETE FROM memory_items WHERE type='chat_log'")
        core.commit()
        remain = core.execute(
            "SELECT COUNT(*) FROM memory_items WHERE type='chat_log'").fetchone()[0]
        total = core.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]
        print(f"\nSource after delete — chat_log remaining: {remain}, total: {total}")
        print("DONE. Run VACUUM on the source separately to reclaim space.")
        return 0
    finally:
        core.close()


if __name__ == "__main__":
    sys.exit(main())
