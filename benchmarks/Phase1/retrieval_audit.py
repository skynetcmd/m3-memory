"""Phase 1: LOCOMO retrieval audit.

Runs the production retrieve_for_question on the first N questions of the LOCOMO
dataset and compares the retrieved hits against the per-question gold dia_id
evidence. No answerer, no judge. Output is a JSONL trace that Phase 2 consumes.

This script imports from bin/ read-only — it does not modify any main-branch
retrieval, ingest, or generation logic.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR / "bin"))

# Force embedding endpoint before memory_core import, same as bench_locomo.py.
os.environ.setdefault("LLM_ENDPOINTS_CSV", "http://localhost:1234/v1")

import bench_locomo  # noqa: E402
from bench_locomo import (  # noqa: E402
    CATEGORIES,
    classify_question,
    ingest_sample_with_graph,
    retrieve_for_question,
)
import memory_core  # noqa: E402


DEFAULT_DATASET = BASE_DIR / "data" / "locomo" / "locomo10.json"
RECALL_KS = [1, 3, 5, 10, 20, 40]


def load_dataset(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def first_n_qa_entries(dataset: list[dict], n: int) -> list[tuple[dict, dict, int]]:
    """Yield (sample, qa, global_idx) for the first N questions across samples."""
    out = []
    idx = 0
    for sample in dataset:
        for qa in sample.get("qa", []):
            if idx >= n:
                return out
            out.append((sample, qa, idx))
            idx += 1
    return out


def samples_touched(entries) -> list[str]:
    """Ordered unique list of sample_ids appearing in the question slice."""
    seen, out = set(), []
    for s, _, _ in entries:
        if s["sample_id"] not in seen:
            seen.add(s["sample_id"])
            out.append(s["sample_id"])
    return out


async def ensure_ingested(dataset: list[dict], wanted: list[str], *, force: bool, log) -> None:
    """Ingest any wanted sample_ids not already present in the DB."""
    by_id = {s["sample_id"]: s for s in dataset}
    to_ingest = []
    for sid in wanted:
        if force:
            to_ingest.append(sid); continue
        # Probe the DB: is there any message for this sample_id?
        with memory_core._db() as db:
            row = db.execute(
                "SELECT id FROM memory_items WHERE user_id = ? AND is_deleted = 0 LIMIT 1",
                (sid,),
            ).fetchone()
        if row is None:
            to_ingest.append(sid)
        else:
            log(f"  {sid}: already ingested — skipping")
    for sid in to_ingest:
        log(f"  ingesting {sid}...")
        n, elapsed = await ingest_sample_with_graph(by_id[sid])
        log(f"  {sid}: ingested {n} items in {elapsed:.1f}s")


def hits_to_records(hits: list[dict]) -> list[dict]:
    """Compact a hit list for audit output — drop bulky content, keep ids + metadata."""
    out = []
    for rank, h in enumerate(hits, start=1):
        meta = h.get("metadata") or {}
        content = h.get("content") or ""
        out.append({
            "rank": rank,
            "id": h.get("id"),
            "score": float(h.get("score", 0.0)),
            "type": h.get("type"),
            "title": h.get("title"),
            "dia_id": meta.get("dia_id"),
            "session_index": meta.get("session_index"),
            "turn_index": meta.get("turn_index"),
            "role": meta.get("role"),
            "session_date": meta.get("session_date"),
            "content_snippet": content[:200],
            "content_len": len(content),
        })
    return out


def recall_at_k(hit_records: list[dict], gold: set[str], ks: list[int]) -> dict:
    """Per-K recall over gold dia_ids using the retrieval order (pre-expansion)."""
    if not gold:
        return {f"r@{k}": None for k in ks} | {"first_gold_rank": None, "any_gold_hit": None}
    first_rank = None
    matched_by_rank = []
    seen_dia = set()
    for rec in hit_records:
        d = rec.get("dia_id")
        if d and d in gold and d not in seen_dia:
            seen_dia.add(d)
            matched_by_rank.append(rec["rank"])
            if first_rank is None:
                first_rank = rec["rank"]
    out = {"first_gold_rank": first_rank, "any_gold_hit": bool(matched_by_rank)}
    for k in ks:
        hits_in_k = sum(1 for r in matched_by_rank if r <= k)
        out[f"r@{k}"] = hits_in_k / len(gold)
    return out


def dia_ids_in_set(hit_records: list[dict], gold: set[str]) -> list[str]:
    return sorted({r["dia_id"] for r in hit_records if r.get("dia_id") in gold})


async def run(args: argparse.Namespace) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(__file__).parent / "runs" / f"audit_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    trace_path = out_dir / "retrieval_trace.jsonl"
    summary_path = out_dir / "summary.json"
    log_path = out_dir / "run.log"
    log_f = open(log_path, "a", encoding="utf-8", buffering=1)

    def log(msg: str):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        try: log_f.write(line + "\n")
        except Exception: pass

    log(f"dataset: {args.dataset}")
    log(f"limit:   first {args.limit} questions")
    log(f"k:       {args.k}    cluster_size: {args.cluster_size}    graph_depth: {args.graph_depth}")
    log(f"out:     {out_dir}")

    dataset = load_dataset(args.dataset)
    entries = first_n_qa_entries(dataset, args.limit)
    log(f"selected {len(entries)} questions across samples: {samples_touched(entries)}")

    await ensure_ingested(dataset, samples_touched(entries), force=args.force_ingest, log=log)

    per_cat_stats = defaultdict(lambda: {"n": 0, "any_hit": 0, "first_rank_sum": 0, "first_rank_n": 0,
                                          **{f"r@{k}_sum": 0.0 for k in RECALL_KS}})
    zero_hit = []
    signal_counter = Counter()

    with open(trace_path, "w", encoding="utf-8") as trace_f:
        for i, (sample, qa, idx) in enumerate(entries, start=1):
            sid = sample["sample_id"]
            q = qa["question"]
            ref = qa.get("answer")
            gold = set(qa.get("evidence") or [])
            cat_idx = qa.get("category", 0)
            cat_label = CATEGORIES.get(cat_idx, "unknown")

            # The runtime classifier (q_signal) is what the real bench uses, not the
            # dataset category. We log both — signal drives retrieve_for_question.
            q_signal = classify_question(q)
            signal_counter[q_signal] += 1

            hits = await retrieve_for_question(
                sid, q, args.k, args.cluster_size, args.graph_depth, q_signal=q_signal,
                enable_smart_retrieval=args.enable_smart_retrieval,
                variant=args.variant,
            )
            records = hits_to_records(hits)
            recall = recall_at_k(records, gold, RECALL_KS)
            gold_hits = dia_ids_in_set(records, gold)

            trace = {
                "idx": idx,
                "sample_id": sid,
                "question": q,
                "reference": ref,
                "category_idx": cat_idx,
                "category": cat_label,
                "q_signal": q_signal,
                "gold_dia_ids": sorted(gold),
                "n_hits": len(records),
                "hits": records,
                "gold_hits_in_retrieved": gold_hits,
                **recall,
            }
            trace_f.write(json.dumps(trace, ensure_ascii=False) + "\n")
            trace_f.flush()

            s = per_cat_stats[cat_label]
            s["n"] += 1
            if recall["any_gold_hit"]:
                s["any_hit"] += 1
                s["first_rank_sum"] += recall["first_gold_rank"]
                s["first_rank_n"] += 1
            else:
                zero_hit.append({"idx": idx, "sample_id": sid, "category": cat_label,
                                 "question": q, "gold": sorted(gold)})
            for k in RECALL_KS:
                v = recall.get(f"r@{k}")
                if v is not None:
                    s[f"r@{k}_sum"] += v

            if i % 20 == 0 or i == len(entries):
                hit_rate = sum(c["any_hit"] for c in per_cat_stats.values()) / i
                log(f"  {i}/{len(entries)}  any_gold_hit_rate={hit_rate:.3f}  zero_hit={len(zero_hit)}")

    # Build summary.
    summary = {
        "run_ts": ts,
        "dataset": str(args.dataset),
        "n_questions": len(entries),
        "variant": args.variant or None,
        "params": {"k": args.k, "cluster_size": args.cluster_size, "graph_depth": args.graph_depth,
                   "enable_smart_retrieval": args.enable_smart_retrieval,
                   "variant": args.variant or None},
        "samples_touched": samples_touched(entries),
        "q_signal_distribution": dict(signal_counter),
        "overall": {},
        "per_category": {},
        "zero_hit_count": len(zero_hit),
    }

    overall_any_hit = 0
    overall_n = 0
    overall_rank_sum = 0
    overall_rank_n = 0
    overall_recall = {f"r@{k}": 0.0 for k in RECALL_KS}
    for cat, s in per_cat_stats.items():
        n = s["n"]
        summary["per_category"][cat] = {
            "n": n,
            "any_gold_hit_rate": s["any_hit"] / n if n else None,
            "mean_first_gold_rank": (s["first_rank_sum"] / s["first_rank_n"]) if s["first_rank_n"] else None,
            **{f"mean_{k_lbl}": (s[f"{k_lbl}_sum"] / n if n else None)
               for k_lbl in [f"r@{k}" for k in RECALL_KS]},
        }
        overall_any_hit += s["any_hit"]
        overall_n += n
        overall_rank_sum += s["first_rank_sum"]
        overall_rank_n += s["first_rank_n"]
        for k in RECALL_KS:
            overall_recall[f"r@{k}"] += s[f"r@{k}_sum"]

    if overall_n:
        summary["overall"] = {
            "any_gold_hit_rate": overall_any_hit / overall_n,
            "mean_first_gold_rank": (overall_rank_sum / overall_rank_n) if overall_rank_n else None,
            **{f"mean_{k}": v / overall_n for k, v in overall_recall.items()},
        }

    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    with open(out_dir / "zero_hit_questions.json", "w", encoding="utf-8") as f:
        json.dump(zero_hit, f, indent=2)

    log(f"done. any_gold_hit_rate={summary['overall'].get('any_gold_hit_rate', 0):.3f}")
    log(f"mean_first_gold_rank={summary['overall'].get('mean_first_gold_rank')}")
    log(f"trace  -> {trace_path}")
    log(f"summary -> {summary_path}")
    try: log_f.close()
    except Exception: pass


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    p.add_argument("--limit", type=int, default=200,
                   help="Process first N questions across samples (default 200).")
    p.add_argument("--k", type=int, default=40, help="Top-K retrieval baseline — matches bench_locomo default.")
    p.add_argument("--cluster-size", type=int, default=5)
    p.add_argument("--graph-depth", type=int, default=1)
    p.add_argument("--force-ingest", action="store_true",
                   help="Re-ingest touched samples even if already present.")
    p.add_argument(
        "--enable-smart-retrieval",
        action="store_true",
        default=os.environ.get("M3_ENABLE_SMART_RETRIEVAL", "").lower() in ("1", "true", "yes"),
        help="Opt into smart_time_boost + neighbor-session expansion. Off by default on LOCOMO "
             "(relative-date dialog). Env var: M3_ENABLE_SMART_RETRIEVAL=1.",
    )
    p.add_argument(
        "--variant", type=str, default="",
        help="Filter retrieval to rows with this variant tag. Use '__none__' for untagged rows. "
             "Empty (default) returns all rows regardless of variant.",
    )
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
