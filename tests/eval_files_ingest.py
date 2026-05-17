"""Phase-1 eval harness for files_memory retrieval.

Ingests `tests/eval_corpus/` into a fresh temp files.db, runs each
question in `eval_questions.yaml` against `files_search`, and reports:

  - per-question pass/fail
  - file-localization rate (top hit's filename matches expect_file)
  - text-recall rate (any top-N hit's text contains expect_text)
  - overall pass rate

Pass criterion: ≥ 80% text-recall on the default top-5 cutoff. This is
the phase-1 acceptance gate (FILE_INGESTION_PLAN.md §11 P1).

Run:
    python tests/eval_files_ingest.py
    python tests/eval_files_ingest.py --topn 3
    python tests/eval_files_ingest.py --no-llm   # disables summarizer
"""
from __future__ import annotations

import argparse
import os
import shutil
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


def _ingest(corpus_dir: Path, db_path: Path) -> None:
    """Fresh ingest of the eval corpus into the temp DB."""
    os.environ["M3_FILES_DB_PATH"] = str(db_path)
    from files_memory.ingest import ingest_path
    r = ingest_path(str(corpus_dir))
    print(
        f"[ingest] {r.files_created} files, {r.leaves_written} leaves "
        f"in {r.duration_ms} ms"
    )
    if r.files_failed:
        print(f"[ingest] FAIL: {r.failures}")
        sys.exit(2)


def _run_eval(topn: int) -> dict:
    from files_memory.search import files_search

    questions = _load_questions()
    results = []
    text_hits = 0
    file_hits = 0
    for q in questions:
        hits = files_search(q["q"], limit=topn)
        text_match = False
        file_match_top = False
        first_match_rank = None
        for i, h in enumerate(hits, start=1):
            haystack = h.text.lower()
            needle = q["expect_text"].lower()
            if needle in haystack:
                text_match = True
                if first_match_rank is None:
                    first_match_rank = i
                if "expect_file" in q and h.filename == q["expect_file"]:
                    if i == 1:
                        file_match_top = True

        results.append({
            "q": q["q"],
            "expect_text": q["expect_text"],
            "expect_file": q.get("expect_file"),
            "text_match": text_match,
            "file_match_top1": file_match_top,
            "first_match_rank": first_match_rank,
            "top_filenames": [h.filename for h in hits[:topn]],
        })
        if text_match:
            text_hits += 1
        if file_match_top:
            file_hits += 1

    return {
        "topn": topn,
        "total": len(questions),
        "text_recall": text_hits,
        "file_localization": file_hits,
        "text_recall_pct": (text_hits / len(questions)) * 100,
        "results": results,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--topn", type=int, default=5, help="top-N for recall check")
    p.add_argument("--no-llm", action="store_true",
                   help="disable summarizer LLM (fallback summaries)")
    p.add_argument("--pass-threshold", type=float, default=0.80,
                   help="pass rate floor (0.0-1.0); default 0.80")
    p.add_argument("--keep-db", action="store_true",
                   help="don't delete the temp DB after running")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    if args.no_llm:
        os.environ.pop("M3_FILES_SUMMARY_URL", None)
        os.environ.pop("M3_LMSTUDIO_URL", None)

    tmp = Path(tempfile.mkdtemp(prefix="files_eval_"))
    db_path = tmp / "eval.db"
    corpus_dir = _HERE / "eval_corpus"

    try:
        _ingest(corpus_dir, db_path)
        summary = _run_eval(args.topn)

        print()
        print(f"=== Eval results (top-{args.topn}) ===")
        print(f"Total questions:    {summary['total']}")
        print(f"Text recall:        {summary['text_recall']} / {summary['total']} "
              f"({summary['text_recall_pct']:.1f}%)")
        print(f"File localization:  {summary['file_localization']} / {summary['total']} "
              f"({summary['file_localization'] / summary['total'] * 100:.1f}%)")

        if args.verbose:
            print()
            for r in summary["results"]:
                tag = "PASS" if r["text_match"] else "FAIL"
                rank = f"@{r['first_match_rank']}" if r["first_match_rank"] else "  -"
                print(f"  [{tag} {rank}] {r['q'][:50]:50}  exp={r['expect_text'][:30]}")
                if not r["text_match"]:
                    print(f"           top files: {r['top_filenames']}")

        passed = summary["text_recall"] / summary["total"] >= args.pass_threshold
        print()
        print(f"{'PASS' if passed else 'FAIL'} "
              f"(threshold = {args.pass_threshold * 100:.0f}%)")
        return 0 if passed else 1
    finally:
        if not args.keep_db:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
