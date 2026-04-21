"""Compare two Phase 1 runs side-by-side."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

BASE = Path(__file__).parent / "runs"

def load(name):
    with open(BASE / name / "summary.json") as f: s = json.load(f)
    handoff_path = BASE / name / "handoff_analysis.json"
    h = json.load(open(handoff_path)) if handoff_path.exists() else None
    return s, h

def main():
    p = argparse.ArgumentParser(description="Compare two Phase1 retrieval audit runs.")
    p.add_argument("--a", required=True, help="Baseline run dir name under benchmarks/locomo/runs/")
    p.add_argument("--b", required=True, help="Candidate run dir name under benchmarks/locomo/runs/")
    args = p.parse_args()

    a_s, a_h = load(args.a)
    b_s, b_h = load(args.b)

    print("=" * 78)
    print(f"{'':40}  {args.a[:22]:>22}  {args.b[:17]:>17}  delta")
    print("=" * 78)
    print("\nOverall:")
    for k in ["any_gold_hit_rate", "mean_first_gold_rank", "mean_r@1", "mean_r@3", "mean_r@5", "mean_r@10", "mean_r@20", "mean_r@40"]:
        a = a_s["overall"].get(k)
        b = b_s["overall"].get(k)
        if a is None or b is None:
            print(f"  {k:<40}  {str(a):>18}  {str(b):>15}")
            continue
        d = b - a
        sign = "+" if d >= 0 else ""
        better = ""
        if k == "mean_first_gold_rank":
            better = "  (lower=better)" if d < 0 else "  (WORSE)"
        else:
            better = "  (higher=better)" if d > 0 else ("  (WORSE)" if d < 0 else "")
        print(f"  {k:<40}  {a:>18.4f}  {b:>15.4f}  {sign}{d:+.4f}{better}")

    print("\nPer-category recall@40:")
    cats = ["single-hop", "multi-hop", "temporal", "open-domain", "adversarial"]
    for cat in cats:
        a = a_s["per_category"].get(cat, {})
        b = b_s["per_category"].get(cat, {})
        a_r40 = a.get("mean_r@40")
        b_r40 = b.get("mean_r@40")
        a_rank = a.get("mean_first_gold_rank")
        b_rank = b.get("mean_first_gold_rank")
        print(f"  {cat:<15}  n={a.get('n', 0):>3}  "
              f"r@40: {a_r40:.3f} -> {b_r40:.3f}  ({(b_r40-a_r40):+.3f})  "
              f"rank: {a_rank:.0f} -> {b_rank:.0f} ({(b_rank-a_rank):+.0f})")

    if a_h and b_h:
        print("\nPer-category: gold in top-K (from handoff analysis):")
        for cat in cats:
            a = a_h["per_category"].get(cat, {})
            b = b_h["per_category"].get(cat, {})
            print(f"  {cat:<15}  top-10: {a.get('gold_in_top10', 0):>2}/{a.get('n', 0):<2} -> {b.get('gold_in_top10', 0):>2}/{b.get('n', 0):<2}   "
                  f"top-40: {a.get('gold_in_top40', 0):>2}/{a.get('n', 0):<2} -> {b.get('gold_in_top40', 0):>2}/{b.get('n', 0):<2}")

        print("\nNoise floor (mean fraction of top-K that is gold — higher=better):")
        a_nf = a_h["noise_floor"]; b_nf = b_h["noise_floor"]
        print(f"  top-10:  {a_nf['mean_gold_frac_top10']:.4f} -> {b_nf['mean_gold_frac_top10']:.4f}  ({(b_nf['mean_gold_frac_top10']-a_nf['mean_gold_frac_top10']):+.4f})")
        print(f"  top-40:  {a_nf['mean_gold_frac_top40']:.4f} -> {b_nf['mean_gold_frac_top40']:.4f}  ({(b_nf['mean_gold_frac_top40']-a_nf['mean_gold_frac_top40']):+.4f})")

    print("\nq_signal distribution (shows classifier effect):")
    print(f"  baseline: {a_s['q_signal_distribution']}")
    print(f"  post:     {b_s['q_signal_distribution']}")

    print("\nZero-hit count:")
    print(f"  baseline: {a_s['zero_hit_count']}")
    print(f"  post:     {b_s['zero_hit_count']}")

if __name__ == "__main__":
    main()
