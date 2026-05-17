"""Phase-3 eval harness — provenance, carry-forward, promotability, dedup, rename.

Runs the five P3 acceptance gates against a fresh temp files.db. Returns
non-zero exit if any subsystem regresses.

Run:
    python tests/eval_files_phase3.py
    python tests/eval_files_phase3.py --skip-llm   # skip extraction-dependent gates
    python tests/eval_files_phase3.py --extract-url http://localhost:11434 \
                                     --extract-model qwen2.5:1.5b-instruct
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

# Add bin/ to import path
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
sys.path.insert(0, str(_REPO_ROOT / "bin"))


def _print_gate(name: str, ok: bool, detail: str = ""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}  {detail}")


# ──────────────────────────────────────────────────────────────────────────────
# Gate: P3.0 — provenance (sidecar + CLI flag)
# ──────────────────────────────────────────────────────────────────────────────
def gate_provenance(tmp: Path) -> tuple[bool, str]:
    from files_memory.ingest import ingest_path
    from files_memory.index import files_index

    corpus = tmp / "p30_corpus"
    corpus.mkdir()
    plain = corpus / "plain.md"
    plain.write_text("# Plain\n\nNo conversion.\n", encoding="utf-8")
    side = corpus / "scan.txt"
    side.write_text("# Receipt\n\nOCR text.\n", encoding="utf-8")
    (corpus / "scan.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg")
    (corpus / "scan.txt.m3meta.json").write_text(
        json.dumps({"original_path": "scan.jpg", "conversion": {"tool": "tesseract"}}),
        encoding="utf-8",
    )
    db = tmp / "p30.db"
    ingest_path(str(corpus), db_path=str(db), exclude=["*.m3meta.json"], include=["*.md", "*.txt"])
    entries = files_index(db_path=str(db))
    prov = {e.filename: e.original_path for e in entries}
    if prov.get("plain.md") is not None:
        return False, f"plain.md should have None, got {prov.get('plain.md')!r}"
    side_orig = prov.get("scan.txt")
    if side_orig is None or not side_orig.endswith("scan.jpg"):
        return False, f"sidecar didn't apply: {side_orig!r}"
    return True, "sidecar + no-provenance paths correct"


# ──────────────────────────────────────────────────────────────────────────────
# Gate: P3.1 — carry-forward (requires LLM for facts; degrades gracefully)
# ──────────────────────────────────────────────────────────────────────────────
def gate_carry_forward(tmp: Path, use_llm: bool) -> tuple[bool, str]:
    from files_memory.ingest import ingest_path

    corpus = tmp / "p31_corpus"
    corpus.mkdir()
    path_b = corpus / "evolving.md"
    path_b.write_text(
        "# Document\n\n## Intro\n\n"
        "Three SkyWidget models exist: small, medium, large. Made in Portland.\n\n"
        "## Pricing\n\nSmall costs 50 dollars. Medium costs 100. "
        "Volume orders of 100+ get 12 percent off.\n",
        encoding="utf-8",
    )
    db = tmp / "p31.db"
    mode = "inline" if use_llm else "none"
    r1 = ingest_path(str(corpus), db_path=str(db), extract_mode=mode)
    if r1.files_created != 1:
        return False, f"expected 1 file, got {r1.files_created}"

    # Modify only the Pricing section.
    path_b.write_text(
        "# Document\n\n## Intro\n\n"
        "Three SkyWidget models exist: small, medium, large. Made in Portland.\n\n"
        "## Pricing\n\nSmall costs 65 dollars now. Medium costs 120. Large costs 200. "
        "Volume orders of 100+ get 15 percent off.\n",
        encoding="utf-8",
    )
    r2 = ingest_path(str(corpus), db_path=str(db), extract_mode=mode)
    if r2.files_superseded != 1:
        return False, f"expected 1 superseded, got {r2.files_superseded}"
    if r2.leaves_carried < 1:
        return False, f"expected leaves_carried >= 1, got {r2.leaves_carried}"
    if r2.embeds_avoided < 1:
        return False, f"expected embeds_avoided >= 1, got {r2.embeds_avoided}"

    # Verify evolved_from edges exist on the new leaves.
    conn = sqlite3.connect(str(db))
    n_evolved = conn.execute(
        "SELECT COUNT(*) FROM leaves l JOIN file_nodes fn ON fn.uuid=l.file_node "
        "WHERE fn.superseded_by IS NULL AND l.evolved_from IS NOT NULL"
    ).fetchone()[0]
    conn.close()
    if n_evolved < 1:
        return False, "no evolved_from edges in current leaves"

    detail = (f"carried={r2.leaves_carried} evolved={r2.leaves_evolved} "
              f"embeds_avoided={r2.embeds_avoided}")
    if use_llm:
        detail += f" facts_carried={r2.facts_carried}"
    return True, detail


# ──────────────────────────────────────────────────────────────────────────────
# Gate: P3.2 — promotability (requires LLM)
# ──────────────────────────────────────────────────────────────────────────────
def gate_promotability(tmp: Path) -> tuple[bool, str]:
    from files_memory.ingest import ingest_path
    from files_memory.search import files_search
    from files_memory.promotability import files_promotable

    db = tmp / "p32.db"
    eval_corpus = _HERE / "eval_corpus"
    ingest_path(str(eval_corpus), db_path=str(db), extract_mode="inline")

    # Hit several facts to bump hit_count.
    for q in ["how much do small widgets weigh", "widget weight kilograms",
              "small widgets red blue green", "express shipping business days"]:
        for _ in range(2):
            files_search(q, limit=5, db_path=str(db))

    candidates = files_promotable(limit=10, db_path=str(db))
    if not candidates:
        return False, "no promotable candidates after repeated hits"
    top = candidates[0]
    if top["hit_count"] < 1:
        return False, f"top candidate hit_count={top['hit_count']}"
    if top["promotability_score"] < 0.30:
        return False, f"top score below threshold: {top['promotability_score']}"
    return True, f"{len(candidates)} candidates, top score={top['promotability_score']:.2f}"


# ──────────────────────────────────────────────────────────────────────────────
# Gate: P3.3 — semantic dedup
# ──────────────────────────────────────────────────────────────────────────────
def gate_dedup(tmp: Path) -> tuple[bool, str]:
    from files_memory.ingest import ingest_path
    from files_memory.dedup import files_dedup, list_dedup_candidates, review_dedup_candidate

    corpus = tmp / "p33_corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text(
        "# Handbook\n\n## Vacation\n\n"
        "Full-time employees accrue 20 days of vacation per year. "
        "Unused vacation does not roll over past December 31st.\n",
        encoding="utf-8",
    )
    (corpus / "b.md").write_text(
        "# HR Policies\n\n## Annual Leave\n\n"
        "Each full-time worker earns twenty vacation days yearly. "
        "Leftover vacation expires at the end of December.\n",
        encoding="utf-8",
    )
    (corpus / "c.md").write_text(
        "# Widgets\n\n## Specs\n\nSmall widgets weigh 1.2 kg in red, blue, green.\n",
        encoding="utf-8",
    )
    db = tmp / "p33.db"
    ingest_path(str(corpus), db_path=str(db))
    scan = files_dedup(threshold=0.80, db_path=str(db))
    if scan["pairs_recorded"] < 1:
        return False, f"no near-dup pairs surfaced: {scan}"

    candidates = list_dedup_candidates(db_path=str(db), limit=5)
    top = candidates[0]
    files = {top["leaf_a"]["file"], top["leaf_b"]["file"]}
    if files != {"a.md", "b.md"}:
        return False, f"wrong pair: {files}"

    # Review round-trip.
    review_dedup_candidate(top["uuid"], "merged", db_path=str(db))
    after = list_dedup_candidates(db_path=str(db))
    if any(c["uuid"] == top["uuid"] for c in after):
        return False, "reviewed candidate still surfaces as unreviewed"

    return True, f"top pair cosine={top['cosine']:.3f}, review round-trip ok"


# ──────────────────────────────────────────────────────────────────────────────
# Gate: P3.4 — rename heuristic
# ──────────────────────────────────────────────────────────────────────────────
def gate_rename(tmp: Path) -> tuple[bool, str]:
    from files_memory.ingest import ingest_path
    from files_memory.staleness import files_staleness_review, link_rename

    corpus = tmp / "p34_corpus"
    corpus.mkdir()
    original = corpus / "old_name.md"
    original.write_text(
        "# Project Notes\n\nTracks Q2 milestones. Phase 1 ships next week.\n",
        encoding="utf-8",
    )
    db = tmp / "p34.db"
    ingest_path(str(corpus), db_path=str(db))

    renamed = corpus / "q2_milestones.md"
    os.rename(original, renamed)

    rpt = files_staleness_review(directory=str(corpus), db_path=str(db))
    if len(rpt.rename_candidates) != 1:
        return False, (f"expected 1 rename candidate, got {len(rpt.rename_candidates)} "
                       f"(missing={len(rpt.missing)} new={len(rpt.new)})")

    cand = rpt.rename_candidates[0]
    result = link_rename(
        cand.missing_file_node_uuid, cand.new_path,
        expect_sha256=cand.content_sha256, db_path=str(db),
    )
    if result.get("action") != "linked":
        return False, f"link_rename didn't link: {result}"

    rpt2 = files_staleness_review(directory=str(corpus), db_path=str(db))
    if rpt2.missing or rpt2.new or rpt2.rename_candidates:
        return False, (f"post-link review not clean: "
                       f"missing={len(rpt2.missing)} new={len(rpt2.new)} "
                       f"rename={len(rpt2.rename_candidates)}")

    # Content-change guard: rewrite renamed, link_rename must refuse.
    renamed.write_text("# Project Notes\n\nDifferent content now.\n", encoding="utf-8")
    try:
        link_rename(cand.missing_file_node_uuid, str(renamed), db_path=str(db))
        return False, "link_rename should have refused content change"
    except ValueError:
        pass

    return True, "detect, link, idempotent, content-guard all ok"


# ──────────────────────────────────────────────────────────────────────────────
# Driver
# ──────────────────────────────────────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--skip-llm", action="store_true",
                   help="skip gates that need an LLM (carry-forward facts, promotability)")
    p.add_argument("--extract-url", default="http://localhost:11434")
    p.add_argument("--extract-model", default="qwen2.5:1.5b-instruct")
    p.add_argument("--keep-tmp", action="store_true")
    args = p.parse_args()

    if not args.skip_llm:
        os.environ["M3_FILES_EXTRACT_URL"] = args.extract_url
        os.environ["M3_FILES_EXTRACT_MODEL"] = args.extract_model

    tmp = Path(tempfile.mkdtemp(prefix="files_p3_eval_"))
    failures: list[str] = []
    t0 = time.perf_counter()
    try:
        # Each gate gets its own subdir of `tmp`.
        for name, fn, needs_llm in [
            ("P3.0 provenance",     gate_provenance,    False),
            ("P3.1 carry-forward",  lambda t: gate_carry_forward(t, use_llm=not args.skip_llm), False),
            ("P3.3 dedup",          gate_dedup,         False),
            ("P3.4 rename",         gate_rename,        False),
            ("P3.2 promotability",  gate_promotability, True),
        ]:
            if needs_llm and args.skip_llm:
                print(f"  [SKIP] {name}  (--skip-llm)")
                continue
            sub = tmp / name.replace(" ", "_").replace(".", "_")
            sub.mkdir()
            try:
                ok, detail = fn(sub)
                _print_gate(name, ok, detail)
                if not ok:
                    failures.append(f"{name}: {detail}")
            except Exception as e:
                _print_gate(name, False, f"raised {type(e).__name__}: {e}")
                failures.append(f"{name}: raised {type(e).__name__}: {e}")

        duration = time.perf_counter() - t0
        print()
        if failures:
            print(f"FAIL  ({len(failures)} gate(s) failed, {duration:.1f}s)")
            for f in failures:
                print(f"  - {f}")
            return 1
        print(f"PASS  (all P3 gates cleared, {duration:.1f}s)")
        return 0
    finally:
        if not args.keep_tmp:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
