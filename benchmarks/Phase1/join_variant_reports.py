"""Join multiple retrieval_audit summary.json files into one comparison report.

Finds the most recent audit run for each requested variant by walking
benchmarks/Phase1/runs/audit_*/summary.json and matching by the variant
recorded in metadata. If a variant has no run yet, it is omitted.

Writes a markdown report to stdout (or --out) summarizing:
- overall any-gold-hit rate, mean_first_gold_rank, zero-hit count
- recall@K columns side-by-side for K in (1, 3, 5, 10, 20, 40)
- per-category table for any-gold-hit and r@10
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
RUNS_DIR = BASE / "runs"

DEFAULT_VARIANTS = ["baseline", "heuristic_c1c4", "llm_v1", "llm_only"]


def discover_runs() -> dict[str, dict]:
    """Return {variant_tag: summary_dict_with_path} for the latest run of each
    variant we can find. Variant tag is inferred from either a
    `variant` field in summary.json or from a newline in the summary's
    run.log (the audit script logs the variant filter on start).
    """
    found: dict[str, tuple[str, dict]] = {}
    for summary_path in sorted(RUNS_DIR.glob("audit_*/summary.json")):
        run_dir = summary_path.parent
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        variant = summary.get("variant")
        if not variant:
            variant = _sniff_variant_from_log(run_dir)
        if not variant:
            variant = "__none__"

        prev = found.get(variant)
        if prev is None or run_dir.name > prev[0]:
            found[variant] = (run_dir.name, {**summary, "_run_dir": str(run_dir)})
    return {k: v[1] for k, v in found.items()}


def _sniff_variant_from_log(run_dir: Path) -> str | None:
    log = run_dir / "run.log"
    if not log.exists():
        return None
    try:
        for line in log.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "variant=" in line:
                idx = line.find("variant=") + len("variant=")
                tail = line[idx:].strip()
                for sep in (" ", ",", "'", '"'):
                    if sep in tail:
                        tail = tail.split(sep)[0]
                if tail:
                    return tail
    except Exception:
        return None
    return None


def _fmt_pct(v: float) -> str:
    return f"{v*100:5.1f}%"


def _fmt_delta(cur: float, base: float) -> str:
    d = (cur - base) * 100
    sign = "+" if d >= 0 else ""
    return f"{sign}{d:.1f}pp"


def render(report: dict[str, dict], variants: list[str], baseline_key: str = "baseline") -> str:
    lines: list[str] = []
    lines.append("# LOCOMO Phase 1 — variant comparison\n")

    present = [v for v in variants if v in report]
    missing = [v for v in variants if v not in report]
    lines.append(f"Variants reported: {', '.join(present)}")
    if missing:
        lines.append(f"Variants missing:  {', '.join(missing)}")
    lines.append("")

    for v in present:
        r = report[v]
        lines.append(f"- **{v}** — n={r['n_questions']}, run_dir={Path(r['_run_dir']).name}")
    lines.append("")

    metrics = [
        "any_gold_hit_rate",
        "mean_r@1", "mean_r@3", "mean_r@5",
        "mean_r@10", "mean_r@20", "mean_r@40",
    ]

    header = "| metric | " + " | ".join(present) + " |"
    rule = "|" + "---|" * (1 + len(present))
    lines.append("## Overall\n")
    lines.append(header)
    lines.append(rule)
    for m in metrics:
        row = [m]
        for v in present:
            row.append(_fmt_pct(report[v]["overall"][m]))
        lines.append("| " + " | ".join(row) + " |")

    lines.append("")
    extras = [("mean_first_gold_rank", lambda v: f"{v:.1f}"), ("zero_hit_count", lambda v: f"{v}")]
    for key, fmt in extras:
        row = [key]
        for v in present:
            if key == "zero_hit_count":
                row.append(fmt(report[v].get(key, 0)))
            else:
                row.append(fmt(report[v]["overall"].get(key, 0.0)))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    if baseline_key in present:
        lines.append(f"## Deltas vs `{baseline_key}`\n")
        others = [v for v in present if v != baseline_key]
        if others:
            lines.append("| metric | " + " | ".join(others) + " |")
            lines.append("|" + "---|" * (1 + len(others)))
            for m in metrics:
                base = report[baseline_key]["overall"][m]
                row = [m]
                for v in others:
                    cur = report[v]["overall"][m]
                    row.append(_fmt_delta(cur, base))
                lines.append("| " + " | ".join(row) + " |")
            lines.append("")

    cats = ["temporal", "open-domain", "multi-hop", "single-hop", "adversarial"]
    for section_metric in ("any_gold_hit_rate", "mean_r@10"):
        lines.append(f"## Per-category {section_metric}\n")
        lines.append("| category | n | " + " | ".join(present) + " |")
        lines.append("|" + "---|" * (2 + len(present)))
        for cat in cats:
            row_any = report[present[0]]["per_category"].get(cat, {})
            n = row_any.get("n", 0)
            row = [cat, str(n)]
            for v in present:
                data = report[v]["per_category"].get(cat, {})
                val = data.get(section_metric)
                row.append(_fmt_pct(val) if val is not None else "-")
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")

    return "\n".join(lines)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--variants", nargs="+", default=DEFAULT_VARIANTS,
                   help="Variant ordering in report (default: baseline, heuristic_c1c4, llm_v1, llm_only)")
    p.add_argument("--baseline", default="baseline",
                   help="Variant to show deltas against")
    p.add_argument("--out", type=Path, default=None,
                   help="Write report here instead of stdout")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    report = discover_runs()
    md = render(report, args.variants, baseline_key=args.baseline)
    if args.out:
        args.out.write_text(md, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.buffer.write(md.encode("utf-8"))
        sys.stdout.buffer.write(b"\n")
