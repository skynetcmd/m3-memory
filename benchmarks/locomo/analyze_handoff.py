"""Phase 1 analysis: what does retrieval hand off to the answerer?

Reads retrieval_trace.jsonl and characterizes the context the answerer would
see. No LLM calls — pure structural analysis.

Questions answered:
  - Where does the FIRST gold hit land in the ranking? (distribution)
  - How much of top-K is noise vs gold? (precision@K)
  - Which categories fail where? (rank histogram per category)
  - Zero-hit questions: why — gold not ingested, or ingested but unretrieved?
  - For temporal Qs: is the session_date visible in the snippets?
  - Top-K content: how much is role=user vs assistant, within-session clustering
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path


def load_trace(path: Path) -> list[dict]:
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def rank_bucket(rank: int | None) -> str:
    if rank is None:
        return "never"
    if rank <= 1: return "1"
    if rank <= 3: return "2-3"
    if rank <= 5: return "4-5"
    if rank <= 10: return "6-10"
    if rank <= 20: return "11-20"
    if rank <= 40: return "21-40"
    if rank <= 100: return "41-100"
    return "100+"


RANK_ORDER = ["1", "2-3", "4-5", "6-10", "11-20", "21-40", "41-100", "100+", "never"]


def analyze(trace: list[dict]) -> dict:
    out = {
        "n": len(trace),
        "per_category": {},
        "rank_distribution_overall": Counter(),
        "noise_floor": {},
        "zero_hit_diagnosis": [],
        "temporal_session_date_visibility": {},
        "top_k_composition": {},
    }

    by_cat = defaultdict(list)
    for t in trace:
        by_cat[t["category"]].append(t)

    all_top10_gold_fractions = []
    all_top40_gold_fractions = []

    for cat, rows in by_cat.items():
        first_ranks = []
        bucket = Counter()
        n_zero = 0
        n_gold_in_top10 = 0
        n_gold_in_top40 = 0
        gold_frac_top10 = []
        gold_frac_top40 = []
        session_date_present_in_topk = 0

        for t in rows:
            fr = t.get("first_gold_rank")
            bucket[rank_bucket(fr)] += 1
            if fr is None:
                n_zero += 1
            else:
                first_ranks.append(fr)
                if fr <= 10: n_gold_in_top10 += 1
                if fr <= 40: n_gold_in_top40 += 1

            hits = t.get("hits") or []
            gold = set(t.get("gold_dia_ids") or [])
            if gold:
                top10 = hits[:10]
                top40 = hits[:40]
                top10_gold = sum(1 for h in top10 if h.get("dia_id") in gold)
                top40_gold = sum(1 for h in top40 if h.get("dia_id") in gold)
                if top10:
                    gold_frac_top10.append(top10_gold / len(top10))
                    all_top10_gold_fractions.append(top10_gold / len(top10))
                if top40:
                    gold_frac_top40.append(top40_gold / len(top40))
                    all_top40_gold_fractions.append(top40_gold / len(top40))

            # Temporal probe: for temporal Qs, is session_date appearing anywhere in top-40?
            if cat == "temporal":
                for h in hits[:40]:
                    if h.get("session_date"):
                        session_date_present_in_topk += 1
                        break

        n = len(rows)
        out["per_category"][cat] = {
            "n": n,
            "zero_hit": n_zero,
            "gold_in_top10": n_gold_in_top10,
            "gold_in_top40": n_gold_in_top40,
            "pct_gold_in_top10": n_gold_in_top10 / n if n else None,
            "pct_gold_in_top40": n_gold_in_top40 / n if n else None,
            "first_rank_median": statistics.median(first_ranks) if first_ranks else None,
            "first_rank_p75": statistics.quantiles(first_ranks, n=4)[-1] if len(first_ranks) >= 4 else None,
            "first_rank_mean": statistics.mean(first_ranks) if first_ranks else None,
            "rank_histogram": {b: bucket[b] for b in RANK_ORDER if bucket[b]},
            "mean_gold_frac_top10": statistics.mean(gold_frac_top10) if gold_frac_top10 else None,
            "mean_gold_frac_top40": statistics.mean(gold_frac_top40) if gold_frac_top40 else None,
            "temporal_session_date_in_top40": session_date_present_in_topk if cat == "temporal" else None,
        }

        for b in RANK_ORDER:
            out["rank_distribution_overall"][b] += bucket[b]

    # Overall noise floor: mean fraction of top-K that is gold
    out["noise_floor"] = {
        "mean_gold_frac_top10": statistics.mean(all_top10_gold_fractions) if all_top10_gold_fractions else None,
        "mean_gold_frac_top40": statistics.mean(all_top40_gold_fractions) if all_top40_gold_fractions else None,
        "interpretation": "Fraction of the top-K that is gold. 1-this = noise fraction the answerer must filter.",
    }

    # Zero-hit diagnosis: gold dia_ids never appeared anywhere in the returned hits
    zero = []
    for t in trace:
        if t.get("first_gold_rank") is not None:
            continue
        gold = set(t.get("gold_dia_ids") or [])
        hits_dias = {h.get("dia_id") for h in (t.get("hits") or []) if h.get("dia_id")}
        zero.append({
            "idx": t["idx"],
            "category": t["category"],
            "q_signal": t.get("q_signal"),
            "question": t["question"][:140],
            "gold": sorted(gold),
            "any_overlap_with_retrieved": bool(gold & hits_dias),
            "retrieved_dia_count": len(hits_dias),
        })
    out["zero_hit_diagnosis"] = zero

    # Top-K composition: role balance, session spread, id-type spread
    role_counter = Counter()
    type_counter = Counter()
    session_spreads = []
    for t in trace:
        hits = (t.get("hits") or [])[:40]
        sess = set()
        for h in hits:
            if h.get("role"):
                role_counter[h["role"]] += 1
            if h.get("type"):
                type_counter[h["type"]] += 1
            if h.get("session_index") is not None:
                sess.add(h["session_index"])
        if hits:
            session_spreads.append(len(sess))
    out["top_k_composition"] = {
        "role_distribution_top40": dict(role_counter),
        "type_distribution_top40": dict(type_counter),
        "mean_unique_sessions_in_top40": statistics.mean(session_spreads) if session_spreads else None,
        "median_unique_sessions_in_top40": statistics.median(session_spreads) if session_spreads else None,
    }

    # Rank histogram overall (pretty)
    out["rank_distribution_overall"] = {b: out["rank_distribution_overall"][b]
                                         for b in RANK_ORDER
                                         if out["rank_distribution_overall"][b]}
    return out


def print_report(a: dict) -> None:
    print("=" * 72)
    print(f"Phase 1 handoff analysis — n={a['n']}")
    print("=" * 72)
    print()
    print("Where does the FIRST gold hit land? (bucket histogram, all Qs)")
    total = a["n"]
    for b in RANK_ORDER:
        c = a["rank_distribution_overall"].get(b, 0)
        if c:
            pct = 100 * c / total
            bar = "#" * int(pct / 2)
            print(f"  rank {b:>6}: {c:>3}  {pct:5.1f}%  {bar}")
    print()
    nf = a["noise_floor"]
    print("Noise floor (mean fraction of top-K that is gold):")
    print(f"  top-10: {nf['mean_gold_frac_top10']:.3f}  -> answerer sees {(1-nf['mean_gold_frac_top10'])*100:.0f}% noise")
    print(f"  top-40: {nf['mean_gold_frac_top40']:.3f}  -> answerer sees {(1-nf['mean_gold_frac_top40'])*100:.0f}% noise")
    print()
    print("Per-category breakdown:")
    for cat, s in a["per_category"].items():
        print(f"\n  [{cat}] n={s['n']}  zero_hit={s['zero_hit']}")
        print(f"    gold in top-10: {s['gold_in_top10']}/{s['n']} ({100*(s['pct_gold_in_top10'] or 0):.1f}%)")
        print(f"    gold in top-40: {s['gold_in_top40']}/{s['n']} ({100*(s['pct_gold_in_top40'] or 0):.1f}%)")
        if s["first_rank_median"] is not None:
            print(f"    first-gold-rank  median={s['first_rank_median']:.0f}  mean={s['first_rank_mean']:.0f}  p75={s['first_rank_p75']}")
        print(f"    mean gold-fraction in top-10: {s['mean_gold_frac_top10']:.3f}")
        print(f"    mean gold-fraction in top-40: {s['mean_gold_frac_top40']:.3f}")
        print(f"    rank histogram: {s['rank_histogram']}")
        if s.get("temporal_session_date_in_top40") is not None:
            print(f"    [temporal] Qs where session_date appears in top-40: {s['temporal_session_date_in_top40']}/{s['n']}")
    print()
    print("Zero-hit diagnosis:")
    zero = a["zero_hit_diagnosis"]
    print(f"  total: {len(zero)}")
    for z in zero:
        overlap = "OVERLAP" if z["any_overlap_with_retrieved"] else "NO-overlap (gold never retrieved)"
        print(f"  - idx={z['idx']} cat={z['category']:<12} sig={z['q_signal']:<10} [{overlap}]")
        print(f"    Q: {z['question']}")
        print(f"    gold: {z['gold']}  retrieved_dia_count={z['retrieved_dia_count']}")
    print()
    comp = a["top_k_composition"]
    print("Top-40 composition:")
    print(f"  role distribution: {comp['role_distribution_top40']}")
    print(f"  type distribution: {comp['type_distribution_top40']}")
    print(f"  unique sessions in top-40:  mean={comp['mean_unique_sessions_in_top40']:.1f}  median={comp['median_unique_sessions_in_top40']:.0f}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--trace", type=Path, required=True)
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    trace = load_trace(args.trace)
    a = analyze(trace)
    print_report(a)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(a, f, indent=2, default=str)
        print(f"\nwritten -> {args.out}")


if __name__ == "__main__":
    main()
