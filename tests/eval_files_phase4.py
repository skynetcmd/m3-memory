"""Phase-4 eval harness — watch daemon + multi-corpus.

Runs four acceptance gates:

  P4.1 watch_once detects a stale file and emits one notification.
  P4.2 cooldown suppresses duplicate notifications within the window.
  P4.3 corpus_create / list / set / delete round-trip behaves.
  P4.4 cross-corpus search isolates by single corpus, fans out across
       a corpora list, and surfaces corpus_id on results.

LLM-free — uses fallback summaries and skips extraction.

Run:
    python tests/eval_files_phase4.py
    python tests/eval_files_phase4.py --keep-tmp
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
sys.path.insert(0, str(_REPO_ROOT / "bin"))


def _print_gate(name: str, ok: bool, detail: str = "") -> None:
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}  {detail}")


# ──────────────────────────────────────────────────────────────────────────────
# P4.1 + P4.2: watch_once detection + cooldown
# ──────────────────────────────────────────────────────────────────────────────
def gate_watch_detects_and_cooldown(tmp: Path) -> tuple[bool, str]:
    from files_memory.ingest import ingest_path
    from files_memory.watch import watch_once

    corpus_dir = tmp / "p41_corpus"
    corpus_dir.mkdir()
    f = corpus_dir / "evolve.md"
    f.write_text("# v1\n\nFirst version.\n", encoding="utf-8")
    db = tmp / "p41.db"
    ingest_path(str(corpus_dir), db_path=str(db))

    # Modify so the file becomes stale.
    time.sleep(1.0)
    f.write_text("# v2\n\nSecond version with substantially different content.\n",
                 encoding="utf-8")

    notifications: list[dict] = []

    def capture(agent_id: str, kind: str, payload: dict):
        notifications.append({"agent_id": agent_id, "kind": kind, "payload": payload})
        return f"capture-ok-{len(notifications)}"

    # First pass: should emit exactly one stale notification.
    r1 = watch_once(
        directory=str(corpus_dir), db_path=str(db),
        cooldown_seconds=3600.0, notify_callable=capture,
    )
    if r1.stale_count != 1:
        return False, f"expected stale_count=1, got {r1.stale_count}"
    if r1.notifications_emitted != 1:
        return False, f"expected 1 notify, got {r1.notifications_emitted}"
    if not any(n["kind"] == "files_staleness.stale" for n in notifications):
        return False, f"missing stale-kind notify: {[n['kind'] for n in notifications]}"

    # Second pass within cooldown: zero emissions, one suppression.
    r2 = watch_once(
        directory=str(corpus_dir), db_path=str(db),
        cooldown_seconds=3600.0, notify_callable=capture,
    )
    if r2.notifications_emitted != 0:
        return False, f"expected 0 emit on second pass, got {r2.notifications_emitted}"
    if r2.notifications_suppressed_by_cooldown < 1:
        return False, f"expected suppression on second pass, got {r2.notifications_suppressed_by_cooldown}"

    return True, (f"first: emit={r1.notifications_emitted}; second: "
                  f"emit={r2.notifications_emitted}, suppressed="
                  f"{r2.notifications_suppressed_by_cooldown}")


# ──────────────────────────────────────────────────────────────────────────────
# P4.3: corpus CRUD round-trip
# ──────────────────────────────────────────────────────────────────────────────
def gate_corpus_crud(tmp: Path) -> tuple[bool, str]:
    from files_memory.corpora import (
        corpus_create, corpus_list, corpus_get, corpus_set,
        corpus_delete, resolve_default_corpus,
    )
    db = tmp / "p43.db"

    # Create two corpora; only second marked default.
    info_a = corpus_create("alpha", description="alpha desc", db_path=str(db))
    if info_a.corpus_id != "alpha" or info_a.is_default:
        return False, f"unexpected alpha info: {info_a}"
    info_b = corpus_create("beta", default=True, db_path=str(db))
    if not info_b.is_default:
        return False, f"beta should be default: {info_b}"

    # Resolve default — should be beta from the row.
    default = resolve_default_corpus(db_path=str(db))
    if default != "beta":
        return False, f"resolve_default expected beta, got {default!r}"

    # Promote alpha to default via corpus_set — beta must lose its flag.
    info_a2 = corpus_set("alpha", default=True, db_path=str(db))
    if not info_a2.is_default:
        return False, f"alpha should be default after set: {info_a2}"
    info_b2 = corpus_get("beta", db_path=str(db))
    if info_b2 is None or info_b2.is_default:
        return False, f"beta should have lost default flag: {info_b2}"

    # corpus_list contains both, sorted.
    items = corpus_list(db_path=str(db))
    ids = [i.corpus_id for i in items]
    if "alpha" not in ids or "beta" not in ids:
        return False, f"corpus_list missing entries: {ids}"

    # Delete beta (no file_nodes) cleanly.
    deleted = corpus_delete("beta", db_path=str(db))
    if deleted["deleted"]["settings"] != 1:
        return False, f"expected 1 settings deleted, got {deleted}"
    after = corpus_get("beta", db_path=str(db))
    if after is not None:
        return False, f"beta should be gone, got {after}"

    return True, f"create/list/set/delete + default-flag transition correct"


# ──────────────────────────────────────────────────────────────────────────────
# P4.4: cross-corpus search isolates + fans out
# ──────────────────────────────────────────────────────────────────────────────
def gate_cross_corpus_search(tmp: Path) -> tuple[bool, str]:
    from files_memory.ingest import ingest_path
    from files_memory.search import files_search

    corpus_a_dir = tmp / "p44_alpha"
    corpus_b_dir = tmp / "p44_beta"
    corpus_a_dir.mkdir()
    corpus_b_dir.mkdir()

    (corpus_a_dir / "doc.md").write_text(
        "# Alpha doc\n\nWidget specifications: small widgets weigh 1.2 kilograms.\n",
        encoding="utf-8",
    )
    (corpus_b_dir / "doc.md").write_text(
        "# Beta doc\n\nWidget specifications: large widgets weigh 12.5 kilograms.\n",
        encoding="utf-8",
    )

    db = tmp / "p44.db"
    ingest_path(str(corpus_a_dir), corpus_id="alpha", db_path=str(db))
    ingest_path(str(corpus_b_dir), corpus_id="beta", db_path=str(db))

    # Single-corpus filter: alpha only.
    hits_a = files_search("widget kilograms", corpus_id="alpha", db_path=str(db))
    if not hits_a:
        return False, "alpha-only search returned 0 hits"
    if any(h.corpus_id != "alpha" for h in hits_a):
        return False, (f"alpha-only filter leaked other corpora: "
                       f"{[h.corpus_id for h in hits_a]}")

    # Single-corpus filter: beta only.
    hits_b = files_search("widget kilograms", corpus_id="beta", db_path=str(db))
    if not hits_b:
        return False, "beta-only search returned 0 hits"
    if any(h.corpus_id != "beta" for h in hits_b):
        return False, (f"beta-only filter leaked other corpora: "
                       f"{[h.corpus_id for h in hits_b]}")

    # corpora list: fan out across both. Should see hits from both.
    hits_both = files_search("widget kilograms", corpora=["alpha", "beta"], db_path=str(db))
    corpora_seen = {h.corpus_id for h in hits_both}
    if corpora_seen != {"alpha", "beta"}:
        return False, f"corpora fan-out missing one side: {corpora_seen}"

    # corpora overrides single corpus_id when both are passed.
    hits_override = files_search(
        "widget kilograms", corpus_id="alpha", corpora=["beta"],
        db_path=str(db),
    )
    if any(h.corpus_id != "beta" for h in hits_override):
        return False, (f"corpora should override corpus_id; "
                       f"got {[h.corpus_id for h in hits_override]}")

    return True, (f"single={len(hits_a)}/{len(hits_b)}, fan-out={len(hits_both)} "
                  f"(corpora={sorted(corpora_seen)}); override correct")


# ──────────────────────────────────────────────────────────────────────────────
# Driver
# ──────────────────────────────────────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--keep-tmp", action="store_true")
    args = p.parse_args()

    # No LLM — phase-4 gates don't need extraction.
    os.environ.pop("M3_FILES_EXTRACT_URL", None)
    os.environ.pop("M3_FILES_SUMMARY_URL", None)

    tmp = Path(tempfile.mkdtemp(prefix="files_p4_eval_"))
    failures: list[str] = []
    t0 = time.perf_counter()
    try:
        for name, fn in [
            ("P4.1+P4.2 watch_once + cooldown", gate_watch_detects_and_cooldown),
            ("P4.3 corpus CRUD",                gate_corpus_crud),
            ("P4.4 cross-corpus search",        gate_cross_corpus_search),
        ]:
            sub = tmp / name.replace(" ", "_").replace("+", "_").replace(".", "_")
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
        print(f"PASS  (all P4 gates cleared, {duration:.1f}s)")
        return 0
    finally:
        if not args.keep_tmp:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
