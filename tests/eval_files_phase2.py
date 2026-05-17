"""Phase-2 eval harness for files_memory extraction + ascension.

Ingests tests/eval_corpus/ with --mode inline (or queue + drain),
then verifies:
  - facts table is populated (≥1 fact per file with extractable content)
  - facts can be retrieved by content (text recall)
  - promotion writes a memory.db row idempotently
  - staleness review classifies correctly

Pass criterion: facts populated, ≥ 70% of P1 questions answerable from
facts alone (not leaves), promotion + idempotency pass, staleness
classification pass.

Run:
    python tests/eval_files_phase2.py
    python tests/eval_files_phase2.py --queue   # use queue + drain instead of inline
    python tests/eval_files_phase2.py --no-extract-llm  # extraction disabled
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# Add bin/ to import path
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
sys.path.insert(0, str(_REPO_ROOT / "bin"))

import yaml  # type: ignore


def _load_questions() -> list[dict]:
    qpath = _HERE / "eval_corpus" / "eval_questions.yaml"
    with open(qpath, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _ingest(corpus_dir: Path, db_path: Path, mode: str) -> dict:
    """Ingest the corpus with the requested extract mode."""
    os.environ["M3_FILES_DB_PATH"] = str(db_path)
    from files_memory.ingest import ingest_path
    r = ingest_path(str(corpus_dir), extract_mode=mode)
    return {
        "files_created": r.files_created,
        "leaves": r.leaves_written,
        "facts": r.facts_extracted,
        "duration_ms": r.duration_ms,
        "failures": r.failures,
    }


def _drain(db_path: Path) -> dict:
    os.environ["M3_FILES_DB_PATH"] = str(db_path)
    from files_memory.extract import extract_for_pending_leaves
    return extract_for_pending_leaves(limit=200)


def _count_facts(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    n = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    conn.close()
    return n


def _fact_recall(db_path: Path, topn: int = 5) -> dict:
    """Run the P1 Q-A questions, but only count a hit if the answer text
    appears in a FACT (not just any leaf). Tighter recall metric than P1."""
    os.environ["M3_FILES_DB_PATH"] = str(db_path)
    from files_memory.db import _db

    questions = _load_questions()
    hits = 0
    with _db() as conn:
        for q in questions:
            needle = q["expect_text"].lower()
            # Top-N facts by simple LIKE (we don't need ranking here —
            # the question is "does the fact text contain the answer at all").
            rows = conn.execute(
                "SELECT statement FROM facts WHERE LOWER(statement) LIKE ? LIMIT ?",
                (f"%{needle}%", topn),
            ).fetchall()
            if rows:
                hits += 1
    return {"hits": hits, "total": len(questions), "pct": hits / len(questions) * 100}


def _promotion_smoke(db_path: Path, memory_db_path: Path) -> dict:
    """Promote one fact and verify idempotency + memory.db landing."""
    os.environ["M3_FILES_DB_PATH"] = str(db_path)
    os.environ["M3_DATABASE"] = str(memory_db_path)

    from files_memory.db import _db
    from files_memory.promote import files_promote

    with _db() as conn:
        row = conn.execute("SELECT uuid FROM facts LIMIT 1").fetchone()
        if not row:
            return {"ok": False, "reason": "no facts to promote"}
        fact_uuid = row[0]

    r1 = files_promote(fact_uuid, reason="phase2 eval first")
    r2 = files_promote(fact_uuid, reason="phase2 eval second")

    if r1["already_promoted"]:
        return {"ok": False, "reason": "first call returned already_promoted"}
    if not r2["already_promoted"]:
        return {"ok": False, "reason": "second call should have been idempotent"}
    if r1["promoted_to"] != r2["promoted_to"]:
        return {"ok": False, "reason": "promoted_to mismatch between calls"}

    # Confirm memory.db has the row
    conn = sqlite3.connect(str(memory_db_path))
    row = conn.execute(
        "SELECT id, content FROM memory_items WHERE id = ?", (r1["promoted_to"],),
    ).fetchone()
    conn.close()
    if not row:
        return {"ok": False, "reason": "memory.db has no row for promoted_to"}

    return {"ok": True, "promoted_to": r1["promoted_to"], "content": row[1]}


def _staleness_smoke(corpus_dir: Path, db_path: Path) -> dict:
    """Modify a file and verify the staleness helper classifies it as 'stale'."""
    import time as _time
    os.environ["M3_FILES_DB_PATH"] = str(db_path)
    from files_memory.staleness import files_staleness_review

    target = corpus_dir / "widgets.md"
    original = target.read_text(encoding="utf-8")
    try:
        _time.sleep(1.0)  # ensure mtime delta
        target.write_text(original + "\n## NEW SECTION\n\nAdded by eval.\n", encoding="utf-8")
        rpt = files_staleness_review(directory=str(corpus_dir))
        stale_paths = [s.path for s in rpt.stale]
        ok = str(target) in stale_paths or str(target.resolve()) in stale_paths
        return {"ok": ok, "stale_count": len(rpt.stale), "stale_paths": stale_paths}
    finally:
        target.write_text(original, encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--queue", action="store_true",
                   help="use queue mode + drain instead of inline")
    p.add_argument("--no-extract-llm", action="store_true",
                   help="don't set M3_FILES_EXTRACT_URL; extraction will fail")
    p.add_argument("--extract-url", default="http://localhost:11434")
    p.add_argument("--extract-model", default="qwen2.5:1.5b-instruct")
    p.add_argument("--keep-db", action="store_true")
    args = p.parse_args()

    if not args.no_extract_llm:
        os.environ["M3_FILES_EXTRACT_URL"] = args.extract_url
        os.environ["M3_FILES_EXTRACT_MODEL"] = args.extract_model

    tmp = Path(tempfile.mkdtemp(prefix="files_p2_eval_"))
    db_path = tmp / "files.db"
    memory_db_path = tmp / "memory.db"
    corpus_dir = _HERE / "eval_corpus"

    failures: list[str] = []
    try:
        mode = "queue" if args.queue else "inline"
        print(f"--- Ingest (mode={mode}) ---")
        ing = _ingest(corpus_dir, db_path, mode)
        print(
            f"  files={ing['files_created']} leaves={ing['leaves']} "
            f"facts_inline={ing['facts']} duration={ing['duration_ms']}ms"
        )
        if ing["failures"]:
            print(f"  failures: {ing['failures']}")
            failures.append("ingest failures present")

        if args.queue:
            print("--- Queue drain ---")
            d = _drain(db_path)
            print(f"  {d}")

        facts = _count_facts(db_path)
        print(f"--- Facts: {facts} ---")
        if facts < 10:
            failures.append(f"too few facts produced ({facts} < 10)")

        recall = _fact_recall(db_path)
        print(f"--- Fact recall on P1 Q&A: {recall['hits']}/{recall['total']} ({recall['pct']:.0f}%) ---")
        if recall["pct"] < 70:
            failures.append(f"fact recall {recall['pct']:.0f}% < 70%")

        print("--- Promotion smoke ---")
        promo = _promotion_smoke(db_path, memory_db_path)
        print(f"  {promo}")
        if not promo["ok"]:
            failures.append(f"promotion: {promo.get('reason')}")

        print("--- Staleness smoke ---")
        stale = _staleness_smoke(corpus_dir, db_path)
        print(f"  {stale}")
        if not stale["ok"]:
            failures.append(f"staleness: stale_paths={stale.get('stale_paths')}")

        print()
        if failures:
            print("FAIL:")
            for f in failures:
                print(f"  - {f}")
            return 1
        print("PASS: phase-2 eval gate cleared.")
        return 0
    finally:
        if not args.keep_db:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
