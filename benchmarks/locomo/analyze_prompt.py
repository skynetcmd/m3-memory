"""Phase 1: answerer-prompt anatomy and waste analysis.

Replays format_retrieved on every Q in a Phase-1 audit trace (using the hit
IDs the trace already captured — no re-retrieval needed) and measures:
  - Total prompt size (system + timeline + anchors + history + footer)
  - Whether gold dia_ids survive into the rendered history
  - Where gold appears (by char offset and by session block index)
  - How much of the prompt is "waste" by various definitions

Outputs:
  - prompt_analysis.jsonl  — one record per question
  - prompt_summary.json    — aggregate per-category and overall
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR / "bin"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("LLM_ENDPOINTS_CSV", "http://localhost:1234/v1")
logging.disable(logging.CRITICAL)

import bench_locomo  # noqa: E402
import memory_core  # noqa: E402


def load_trace(audit_dir: Path) -> list[dict]:
    with open(audit_dir / "retrieval_trace.jsonl", "r", encoding="utf-8") as f:
        return [json.loads(l) for l in f]


def load_dataset(dataset_path: Path) -> dict[str, dict]:
    with open(dataset_path, "r", encoding="utf-8") as f:
        ds = json.load(f)
    return {s["sample_id"]: s for s in ds}


def find_dia_id_content(sample: dict, dia_id: str) -> tuple[str, int, str] | None:
    m = re.match(r"D(\d+):(\d+)", dia_id)
    if not m:
        return None
    sess_idx = int(m.group(1))
    turn_idx = int(m.group(2)) - 1
    sess_key = f"session_{sess_idx}"
    session = sample.get("conversation", {}).get(sess_key)
    if not session or turn_idx >= len(session):
        return None
    turn = session[turn_idx]
    return (turn.get("text", ""), sess_idx, turn.get("speaker", ""))


def fetch_hits_from_db(hit_ids: list[str]) -> dict[str, dict]:
    """Batch-fetch full memory rows for a list of IDs. Returns id -> dict."""
    if not hit_ids:
        return {}
    out: dict[str, dict] = {}
    # Chunk to avoid SQLite variable-limit
    CHUNK = 500
    with memory_core._db() as db:
        for i in range(0, len(hit_ids), CHUNK):
            chunk = hit_ids[i : i + CHUNK]
            ph = ",".join("?" * len(chunk))
            rows = db.execute(
                f"SELECT id, title, content, metadata_json, conversation_id FROM memory_items "
                f"WHERE id IN ({ph})",
                chunk,
            ).fetchall()
            for r in rows:
                try:
                    meta = json.loads(r["metadata_json"] or "{}")
                except json.JSONDecodeError:
                    meta = {}
                out[r["id"]] = {
                    "id": r["id"],
                    "title": r["title"] or "",
                    "content": r["content"] or "",
                    "metadata": meta,
                    "conversation_id": r["conversation_id"] or "",
                }
    return out


def reconstruct_hits(trace_hits: list[dict], id_to_row: dict[str, dict]) -> list[dict]:
    """Reconstruct rich hits in their original ranked order."""
    out = []
    for th in trace_hits:
        mid = th.get("id")
        row = id_to_row.get(mid)
        if not row:
            continue
        out.append({
            "id": mid,
            "title": row["title"],
            "content": row["content"],
            "metadata": row["metadata"],
            "conversation_id": row["conversation_id"],
            "score": th.get("score", 0.0),
        })
    return out


def build_prompt(sample: dict, question: str, hits: list[dict], q_signal: str) -> dict:
    history, anchors = bench_locomo.format_retrieved(hits, q_signal=q_signal)
    last_d = "Unknown"
    for i in range(35, 0, -1):
        dk = f"session_{i}_date_time"
        if dk in sample["conversation"]:
            last_d = sample["conversation"][dk]
            break
    tl_lines = [
        f"Session {i}: {sample['conversation'][f'session_{i}_date_time']}"
        for i in range(1, 36)
        if f"session_{i}_date_time" in sample["conversation"]
    ]
    timeline = "\n".join(tl_lines)
    sys_prompt = bench_locomo.SYSTEM_PROMPTS.get(q_signal, bench_locomo.SYSTEM_PROMPTS["default"])
    user_prompt = bench_locomo.ANSWER_TEMPLATE.format(
        timeline=timeline, anchors=anchors, history=history, date=last_d, question=question
    )
    return {"system": sys_prompt, "user": user_prompt, "timeline": timeline,
            "anchors": anchors, "history": history, "date": last_d}


def locate_gold_in_prompt(prompt: str, gold_contents: list[str]) -> list[dict]:
    results = []
    q_marker_idx = prompt.rfind("Question:")
    prompt_len = len(prompt)
    for gc in gold_contents:
        if not gc or len(gc) < 15:
            results.append({"found": False, "reason": "too_short", "content": gc})
            continue
        needles = [gc[:80], gc[:50], gc[:30]]
        hit_offset = -1
        matched_needle = None
        for needle in needles:
            needle = needle.strip()
            if not needle:
                continue
            idx = prompt.find(needle)
            if idx >= 0:
                hit_offset = idx
                matched_needle = needle
                break
        if hit_offset < 0:
            results.append({"found": False, "reason": "not_in_prompt", "content_preview": gc[:80]})
        else:
            results.append({
                "found": True,
                "offset": hit_offset,
                "pct_through_prompt": round(hit_offset / prompt_len, 3),
                "chars_before": hit_offset,
                "chars_after_to_question": (q_marker_idx - hit_offset) if q_marker_idx > 0 else None,
                "needle_len": len(matched_needle),
            })
    return results


def section_breakdown(parts: dict) -> dict:
    return {
        "system_chars": len(parts["system"]),
        "user_chars": len(parts["user"]),
        "timeline_chars": len(parts["timeline"]),
        "anchors_chars": len(parts["anchors"]),
        "history_chars": len(parts["history"]),
        "history_lines": parts["history"].count("\n") + 1 if parts["history"] else 0,
        "total_chars": len(parts["system"]) + len(parts["user"]),
    }


def session_structure(history: str) -> dict:
    obs_block_chars = 0
    if history.startswith("[Observations and Summaries]"):
        end = history.find("\n[Session on ")
        obs_block_chars = len(history[:end]) if end > 0 else len(history)
    session_blocks = re.findall(r"\[Session on [^\]]+\][^\[]*", history)
    return {
        "observation_block_chars": obs_block_chars,
        "observation_lines": history[:obs_block_chars].count("\n") if obs_block_chars else 0,
        "n_session_blocks": len(session_blocks),
        "session_chars_total": sum(len(b) for b in session_blocks),
        "session_chars_mean": sum(len(b) for b in session_blocks) // max(1, len(session_blocks)),
        "session_chars_max": max((len(b) for b in session_blocks), default=0),
    }


def gold_session_indices(sample: dict, gold_dia_ids: list[str]) -> set[int]:
    out = set()
    for d in gold_dia_ids:
        m = re.match(r"D(\d+):", d)
        if m:
            out.add(int(m.group(1)))
    return out


def session_blocks_detail(history: str) -> list[dict]:
    blocks = []
    chunks = re.split(r"(\[Session on [^\]]+\])", history)
    i = 1
    while i < len(chunks):
        header = chunks[i]
        body = chunks[i + 1] if i + 1 < len(chunks) else ""
        date_m = re.search(r"\[Session on ([^\]]+)\]", header)
        blocks.append({
            "date": date_m.group(1) if date_m else "",
            "chars": len(header) + len(body),
            "n_lines": (header + body).count("\n"),
        })
        i += 2
    return blocks


def analyze_one(sample: dict, trace_row: dict, id_to_row: dict[str, dict]) -> dict:
    sid = trace_row["sample_id"]
    q = trace_row["question"]
    q_signal = trace_row["q_signal"]
    gold_dia_ids = trace_row.get("gold_dia_ids", []) or []

    gold_resolved = []
    for d in gold_dia_ids:
        info = find_dia_id_content(sample, d)
        if info:
            content, s_idx, role = info
            gold_resolved.append({"dia_id": d, "content": content, "session": s_idx, "role": role})

    hits = reconstruct_hits(trace_row["hits"], id_to_row)
    parts = build_prompt(sample, q, hits, q_signal)
    sect = section_breakdown(parts)
    struct = session_structure(parts["history"])

    gold_placement = locate_gold_in_prompt(parts["user"], [g["content"] for g in gold_resolved])
    gold_any_found = any(g["found"] for g in gold_placement)
    first_gold_offset = min((g["offset"] for g in gold_placement if g.get("found")), default=None)

    gold_sessions = gold_session_indices(sample, gold_dia_ids)
    date_to_idx = {}
    for i in range(1, 36):
        dk = f"session_{i}_date_time"
        if dk in sample["conversation"]:
            date_to_idx[sample["conversation"][dk]] = i
    blocks = session_blocks_detail(parts["history"])
    rendered_sessions_with_gold = 0
    rendered_sessions_without_gold = 0
    chars_in_sessions_without_gold = 0
    chars_in_sessions_with_gold = 0
    for b in blocks:
        s_idx = date_to_idx.get(b["date"], None)
        if s_idx is not None and s_idx in gold_sessions:
            rendered_sessions_with_gold += 1
            chars_in_sessions_with_gold += b["chars"]
        else:
            rendered_sessions_without_gold += 1
            chars_in_sessions_without_gold += b["chars"]

    return {
        "idx": trace_row["idx"],
        "sample_id": sid,
        "category": trace_row["category"],
        "q_signal": q_signal,
        "question_len": len(q),
        "n_hits": len(hits),
        "gold_dia_ids": gold_dia_ids,
        "n_gold": len(gold_dia_ids),
        "n_gold_resolved": len(gold_resolved),
        "gold_any_in_prompt": gold_any_found,
        "gold_first_offset": first_gold_offset,
        "gold_placement": gold_placement,
        "retrieval_first_rank": trace_row.get("first_gold_rank"),
        "retrieval_any_hit": trace_row.get("any_gold_hit"),
        "sections": sect,
        "structure": struct,
        "rendered_sessions": {
            "with_gold": rendered_sessions_with_gold,
            "without_gold": rendered_sessions_without_gold,
            "chars_with_gold": chars_in_sessions_with_gold,
            "chars_without_gold": chars_in_sessions_without_gold,
        },
    }


def aggregate(rows: list[dict]) -> dict:
    def init():
        return {
            "n": 0,
            "n_gold_resolved_total": 0,
            "n_prompts_with_gold": 0,
            "n_prompts_with_retrieval_hit": 0,
            "n_prompts_with_retrieval_hit_but_gold_missing": 0,
            "total_prompt_chars_sum": 0,
            "timeline_chars_sum": 0,
            "anchors_chars_sum": 0,
            "history_chars_sum": 0,
            "observation_chars_sum": 0,
            "gold_first_offset_sum": 0,
            "gold_first_offset_n": 0,
            "rendered_sessions_sum": 0,
            "rendered_sessions_with_gold_sum": 0,
            "chars_with_gold_sum": 0,
            "chars_without_gold_sum": 0,
        }
    overall = init()
    per_cat: dict[str, dict] = defaultdict(init)

    for r in rows:
        for bucket in (overall, per_cat[r["category"]]):
            bucket["n"] += 1
            bucket["n_gold_resolved_total"] += r["n_gold_resolved"]
            if r["gold_any_in_prompt"]:
                bucket["n_prompts_with_gold"] += 1
            if r["retrieval_any_hit"]:
                bucket["n_prompts_with_retrieval_hit"] += 1
            if r["retrieval_any_hit"] and not r["gold_any_in_prompt"]:
                bucket["n_prompts_with_retrieval_hit_but_gold_missing"] += 1
            bucket["total_prompt_chars_sum"] += r["sections"]["total_chars"]
            bucket["timeline_chars_sum"] += r["sections"]["timeline_chars"]
            bucket["anchors_chars_sum"] += r["sections"]["anchors_chars"]
            bucket["history_chars_sum"] += r["sections"]["history_chars"]
            bucket["observation_chars_sum"] += r["structure"]["observation_block_chars"]
            if r["gold_first_offset"] is not None:
                bucket["gold_first_offset_sum"] += r["gold_first_offset"]
                bucket["gold_first_offset_n"] += 1
            bucket["rendered_sessions_sum"] += (
                r["rendered_sessions"]["with_gold"] + r["rendered_sessions"]["without_gold"]
            )
            bucket["rendered_sessions_with_gold_sum"] += r["rendered_sessions"]["with_gold"]
            bucket["chars_with_gold_sum"] += r["rendered_sessions"]["chars_with_gold"]
            bucket["chars_without_gold_sum"] += r["rendered_sessions"]["chars_without_gold"]

    def finalize(b: dict) -> dict:
        n = max(1, b["n"])
        return {
            "n": b["n"],
            "gold_in_prompt_rate": round(b["n_prompts_with_gold"] / n, 4),
            "retrieval_any_hit_rate": round(b["n_prompts_with_retrieval_hit"] / n, 4),
            "hit_but_gold_missing": b["n_prompts_with_retrieval_hit_but_gold_missing"],
            "mean_prompt_chars": b["total_prompt_chars_sum"] // n,
            "mean_timeline_chars": b["timeline_chars_sum"] // n,
            "mean_anchors_chars": b["anchors_chars_sum"] // n,
            "mean_history_chars": b["history_chars_sum"] // n,
            "mean_observation_chars": b["observation_chars_sum"] // n,
            "mean_first_gold_offset": (
                b["gold_first_offset_sum"] // b["gold_first_offset_n"]
                if b["gold_first_offset_n"] else None
            ),
            "mean_rendered_sessions": round(b["rendered_sessions_sum"] / n, 2),
            "mean_rendered_sessions_with_gold": round(b["rendered_sessions_with_gold_sum"] / n, 2),
            "mean_chars_in_sessions_with_gold": b["chars_with_gold_sum"] // n,
            "mean_chars_in_sessions_without_gold": b["chars_without_gold_sum"] // n,
            "pct_chars_wasted_on_gold_free_sessions": round(
                b["chars_without_gold_sum"] / max(1, b["chars_with_gold_sum"] + b["chars_without_gold_sum"]), 4
            ),
        }

    return {
        "overall": finalize(overall),
        "per_category": {cat: finalize(b) for cat, b in per_cat.items()},
    }


def main(args):
    audit_dir = Path(args.audit_dir)
    trace = load_trace(audit_dir)
    samples = load_dataset(Path(args.dataset))
    if args.limit:
        trace = trace[: args.limit]

    # Collect all hit IDs up front and batch-fetch from DB in one pass.
    print(f"collecting hit IDs from {len(trace)} trace rows...")
    all_ids: set[str] = set()
    for t in trace:
        for h in t["hits"]:
            if h.get("id"):
                all_ids.add(h["id"])
    print(f"fetching {len(all_ids)} unique memory rows from DB...")
    id_to_row = fetch_hits_from_db(sorted(all_ids))
    print(f"fetched {len(id_to_row)} rows.")

    out_jsonl = audit_dir / "prompt_analysis.jsonl"
    out_summary = audit_dir / "prompt_summary.json"

    results = []
    with open(out_jsonl, "w", encoding="utf-8") as f:
        for i, t in enumerate(trace, start=1):
            sample = samples.get(t["sample_id"])
            if not sample:
                continue
            row = analyze_one(sample, t, id_to_row)
            results.append(row)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            if i % 50 == 0 or i == len(trace):
                print(f"  {i}/{len(trace)}", flush=True)

    summary = aggregate(results)
    with open(out_summary, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print()
    print(f"wrote {out_jsonl}")
    print(f"wrote {out_summary}")
    print()
    print("=== OVERALL ===")
    for k, v in summary["overall"].items():
        print(f"  {k}: {v}")
    print()
    print("=== PER CATEGORY ===")
    for cat, s in summary["per_category"].items():
        print(f"[{cat}]  n={s['n']}")
        for k, v in s.items():
            if k == "n":
                continue
            print(f"  {k}: {v}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--audit-dir", default="benchmarks/locomo/runs/audit_20260417_141947")
    p.add_argument("--dataset", default=str(BASE_DIR / "data" / "locomo" / "locomo10.json"))
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args()
    main(args)
