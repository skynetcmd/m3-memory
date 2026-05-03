#!/usr/bin/env python3
"""
build_lme_m_db.py — build memory/lme_m.db from longmemeval_m_cleaned.json.

Reference DB for post-bench analysis. Three tables:

    lme_m_questions     — 500 rows, one per question. Question-level
                          metadata + gold_verified flag for manual audit.
    lme_m_conversations — ~241K rows, one per (question_id, haystack_idx).
                          Session-level metadata + concatenated raw text +
                          size + is_gold flag + chunk-count estimates.
    lme_m_turns         — ~2.45M rows, one per turn. Lets analysis SQL
                          ask "which user turn within the gold session
                          contains the evidence?" without reparsing JSON.

Usage:

    # Inspect what would happen, no write
    python bin/build_lme_m_db.py --dry-run

    # Build (idempotent — re-running with same source-of-truth is a no-op)
    python bin/build_lme_m_db.py

    # Force-rebuild (drops + recreates tables)
    python bin/build_lme_m_db.py --rebuild
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = REPO_ROOT / "data" / "longmemeval" / "longmemeval_m_cleaned.json"
DEFAULT_DB = REPO_ROOT / "memory" / "lme_m.db"


SCHEMA_QUESTIONS = """
CREATE TABLE IF NOT EXISTS lme_m_questions (
    question_id            TEXT PRIMARY KEY,
    question_type          TEXT NOT NULL,
    question_text          TEXT NOT NULL,
    question_date          TEXT,
    gold_answer            TEXT NOT NULL,
    -- Manual verification of the gold_answer correctness. Defaults to 0
    -- (unverified). Flip to 1 (verified), -1 (disputed) as you audit.
    gold_verified          INTEGER NOT NULL DEFAULT 0,
    gold_verified_at       TEXT,
    gold_verified_by       TEXT,
    gold_verification_note TEXT,
    -- Counts denormalized for fast filtering / sanity checks
    n_haystack_sessions    INTEGER NOT NULL,
    n_gold_sessions        INTEGER NOT NULL,
    -- Provenance
    imported_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    imported_from          TEXT NOT NULL
);
"""

SCHEMA_CONVERSATIONS = """
CREATE TABLE IF NOT EXISTS lme_m_conversations (
    question_id           TEXT NOT NULL,
    haystack_idx          INTEGER NOT NULL,
    -- The conversation_id format used in agent_test_bench.db is
    -- '<question_id>::<haystack_idx>' so the same string identifies
    -- a session across the analysis DB and the bench DB.
    conversation_id       TEXT NOT NULL,
    lme_session_id        TEXT NOT NULL,
    is_gold               INTEGER NOT NULL,
    session_date          TEXT,
    -- Full session as readable concatenated text:
    --   [user] turn 0 content...
    --   [assistant] turn 1 content...
    raw_conversation_text TEXT NOT NULL,
    raw_size_bytes        INTEGER NOT NULL,
    turn_count            INTEGER NOT NULL,
    -- How many chunks would call_observer split this session into at
    -- two common input_max_chars caps. Lets analysis ask: "If we bumped
    -- the profile to 32K, how many calls would we save in this bucket?"
    estimated_chunks_at_6k   INTEGER NOT NULL,
    estimated_chunks_at_32k  INTEGER NOT NULL,
    PRIMARY KEY (question_id, haystack_idx),
    FOREIGN KEY (question_id) REFERENCES lme_m_questions(question_id)
);
CREATE INDEX IF NOT EXISTS idx_lme_m_conv_qid ON lme_m_conversations(question_id);
CREATE INDEX IF NOT EXISTS idx_lme_m_conv_gold ON lme_m_conversations(question_id, is_gold);
CREATE INDEX IF NOT EXISTS idx_lme_m_conv_size ON lme_m_conversations(raw_size_bytes);
CREATE INDEX IF NOT EXISTS idx_lme_m_conv_session ON lme_m_conversations(lme_session_id);
CREATE INDEX IF NOT EXISTS idx_lme_m_conv_cid ON lme_m_conversations(conversation_id);
"""

SCHEMA_TURNS = """
CREATE TABLE IF NOT EXISTS lme_m_turns (
    question_id   TEXT NOT NULL,
    haystack_idx  INTEGER NOT NULL,
    turn_idx      INTEGER NOT NULL,
    role          TEXT NOT NULL,
    text          TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    PRIMARY KEY (question_id, haystack_idx, turn_idx),
    FOREIGN KEY (question_id, haystack_idx)
        REFERENCES lme_m_conversations(question_id, haystack_idx)
);
CREATE INDEX IF NOT EXISTS idx_lme_m_turns_role ON lme_m_turns(role);
"""


def _estimate_chunks(serialized_size: int, cap: int) -> int:
    """Match _chunk_turns' 0.85 safety margin in run_observer.py.

    The real chunker iterates turn-by-turn with a 200-byte session-wrapper
    overhead; this scalar estimate is good enough for analysis queries.
    """
    safe_budget = int(cap * 0.85) - 200
    if safe_budget <= 0:
        return 1
    return max(1, (serialized_size + safe_budget - 1) // safe_budget)


def _serialize_turns(turns: list[dict]) -> tuple[str, int]:
    """Concatenate turns into the readable form stored in
    raw_conversation_text. Returns (text, byte_size_in_utf8)."""
    parts = []
    for t in turns:
        role = t.get("role", "user")
        content = t.get("content", "")
        parts.append(f"[{role}] {content}")
    text = "\n\n".join(parts)
    return text, len(text.encode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                    help=f"Path to the LME-M JSON. Default: {DEFAULT_SOURCE}")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB,
                    help=f"Output SQLite path. Default: {DEFAULT_DB}")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be written; don't touch the DB.")
    ap.add_argument("--rebuild", action="store_true",
                    help="Drop the three tables before rebuilding. Without "
                         "this flag the script aborts if any of the tables "
                         "already contain rows (idempotent-by-default).")
    args = ap.parse_args()

    if not args.source.exists():
        print(f"ERROR: source not found: {args.source}", file=sys.stderr)
        return 2

    print(f"Reading {args.source} ...")
    t0 = time.time()
    with open(args.source, "r", encoding="utf-8") as f:
        data = json.load(f)
    n_questions = len(data)
    print(f"  {n_questions} questions in {time.time()-t0:.1f}s")

    # Pre-compute counts
    total_convs = 0
    total_turns = 0
    total_gold_rows = 0
    band_counts = {"0-4k": 0, "4-8k": 0, "8-16k": 0, "16-32k": 0, "32k+": 0}
    for q in data:
        haystack = q.get("haystack_sessions") or []
        gold = set(q.get("answer_session_ids") or [])
        sids = q.get("haystack_session_ids") or []
        total_convs += len(haystack)
        for idx, session in enumerate(haystack):
            total_turns += len(session) if isinstance(session, list) else 0
            sid = sids[idx] if idx < len(sids) else None
            if sid in gold:
                total_gold_rows += 1
            # quick band count for the dry-run summary
            text, size = _serialize_turns(session) if isinstance(session, list) else ("", 0)
            kb = size // 1024
            if kb < 4:
                band_counts["0-4k"] += 1
            elif kb < 8:
                band_counts["4-8k"] += 1
            elif kb < 16:
                band_counts["8-16k"] += 1
            elif kb < 32:
                band_counts["16-32k"] += 1
            else:
                band_counts["32k+"] += 1

    print()
    print("Plan:")
    print(f"  questions:     {n_questions}")
    print(f"  conversations: {total_convs}")
    print(f"  turns:         {total_turns}")
    print(f"  gold rows:     {total_gold_rows}")
    print(f"  size-bands:    {band_counts}")
    print(f"  output db:     {args.db}")

    if args.dry_run:
        print()
        print("(dry-run: no DB changes)")
        return 0

    # Open DB
    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(args.db), timeout=60.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")

    if args.rebuild:
        print("Dropping existing tables ...")
        conn.execute("DROP TABLE IF EXISTS lme_m_turns")
        conn.execute("DROP TABLE IF EXISTS lme_m_conversations")
        conn.execute("DROP TABLE IF EXISTS lme_m_questions")
        conn.commit()
    else:
        # Idempotent guard: non-empty tables abort
        for tbl in ("lme_m_questions", "lme_m_conversations", "lme_m_turns"):
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (tbl,),
            ).fetchone()
            if row:
                n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                if n > 0:
                    print(f"ERROR: {tbl} already has {n} rows. "
                          f"Re-run with --rebuild to drop and recreate.",
                          file=sys.stderr)
                    return 2

    print("Creating schema ...")
    conn.executescript(SCHEMA_QUESTIONS)
    conn.executescript(SCHEMA_CONVERSATIONS)
    conn.executescript(SCHEMA_TURNS)

    print("Importing ...")
    t0 = time.time()
    source_label = args.source.name
    n_q_done = 0
    n_c_done = 0
    n_t_done = 0
    last_print_at = t0

    # Batch inserts. Three lists per batch flush.
    batch_q: list[tuple] = []
    batch_c: list[tuple] = []
    batch_t: list[tuple] = []
    BATCH_SIZE = 5000

    def flush() -> None:
        if batch_q:
            conn.executemany(
                "INSERT INTO lme_m_questions ("
                "question_id, question_type, question_text, question_date, "
                "gold_answer, n_haystack_sessions, n_gold_sessions, imported_from"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                batch_q,
            )
            batch_q.clear()
        if batch_c:
            conn.executemany(
                "INSERT INTO lme_m_conversations ("
                "question_id, haystack_idx, conversation_id, lme_session_id, "
                "is_gold, session_date, raw_conversation_text, raw_size_bytes, "
                "turn_count, estimated_chunks_at_6k, estimated_chunks_at_32k"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                batch_c,
            )
            batch_c.clear()
        if batch_t:
            conn.executemany(
                "INSERT INTO lme_m_turns ("
                "question_id, haystack_idx, turn_idx, role, text, size_bytes"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                batch_t,
            )
            batch_t.clear()
        conn.commit()

    for q in data:
        qid = q["question_id"]
        qtype = q["question_type"]
        qtext = q["question"]
        qdate = q.get("question_date")
        gold_answer = q["answer"]
        haystack_sessions = q.get("haystack_sessions") or []
        haystack_sids = q.get("haystack_session_ids") or []
        haystack_dates = q.get("haystack_dates") or []
        answer_sids_set = set(q.get("answer_session_ids") or [])
        n_gold = sum(1 for s in haystack_sids if s in answer_sids_set)

        batch_q.append((
            qid, qtype, qtext, qdate, gold_answer,
            len(haystack_sessions), n_gold, source_label,
        ))
        n_q_done += 1

        for idx, session in enumerate(haystack_sessions):
            sid = haystack_sids[idx] if idx < len(haystack_sids) else f"unknown_{idx}"
            sdate = haystack_dates[idx] if idx < len(haystack_dates) else None
            is_gold = 1 if sid in answer_sids_set else 0
            if not isinstance(session, list):
                session = []
            text, size = _serialize_turns(session)
            chunks_6k = _estimate_chunks(size, 6000)
            chunks_32k = _estimate_chunks(size, 32000)
            cid = f"{qid}::{idx}"
            batch_c.append((
                qid, idx, cid, sid, is_gold, sdate,
                text, size, len(session), chunks_6k, chunks_32k,
            ))
            n_c_done += 1

            for tidx, turn in enumerate(session):
                role = turn.get("role", "user")
                content = turn.get("content", "")
                batch_t.append((
                    qid, idx, tidx, role, content,
                    len(content.encode("utf-8")),
                ))
                n_t_done += 1
                if len(batch_t) >= BATCH_SIZE:
                    flush()

        # Periodic progress
        now = time.time()
        if now - last_print_at >= 5.0:
            elapsed = now - t0
            rate = n_t_done / max(elapsed, 1e-3)
            print(f"  q={n_q_done}/{n_questions}  c={n_c_done}  t={n_t_done}  "
                  f"({rate:.0f} turns/s)")
            last_print_at = now

    flush()

    elapsed = time.time() - t0
    print(f"Done in {elapsed:.1f}s")
    print(f"  questions:     {n_q_done}")
    print(f"  conversations: {n_c_done}")
    print(f"  turns:         {n_t_done}")

    # Sanity counts from the DB
    print()
    print("DB-side sanity:")
    for sql, label in [
        ("SELECT COUNT(*) FROM lme_m_questions", "questions"),
        ("SELECT COUNT(*) FROM lme_m_conversations", "conversations"),
        ("SELECT COUNT(*) FROM lme_m_turns", "turns"),
        ("SELECT COUNT(*) FROM lme_m_conversations WHERE is_gold=1", "gold rows"),
        ("SELECT COUNT(DISTINCT question_id) FROM lme_m_conversations", "distinct qids in convs"),
    ]:
        n = conn.execute(sql).fetchone()[0]
        print(f"  {label:<28} {n}")

    conn.close()
    print(f"\nDB ready at {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
