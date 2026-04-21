"""Probe specific issues identified in handoff analysis:

1) role distribution Melanie:7465 / Gina:24 — is Caroline missing? Check the
   raw dataset and the ingest rows.
2) dia_id format "D8:6; D9:17" in multi-hop gold — is that a single string with
   semicolon-joined evidence? That would break gold matching.
3) Zero-hit "gold: []" cases — empty gold list means the Q has no evidence to
   match against; these should be filtered out of the denominator or flagged.
4) For one zero-hit case with real gold (idx=45 'D11:4'), did we actually
   ingest that dia_id? Pull the row.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]

# Probe 1+2: inspect raw dataset structure
def probe_dataset():
    with open(BASE / "data" / "locomo" / "locomo10.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    print("=" * 72)
    print("RAW DATASET PROBE")
    print("=" * 72)
    samples = {s["sample_id"]: s for s in data}
    print(f"samples: {list(samples.keys())}")
    print()
    for sid in ["conv-26", "conv-30"]:
        s = samples.get(sid)
        if not s: continue
        conv = s["conversation"]
        speakers = Counter()
        session_count = 0
        turn_count = 0
        for key in conv:
            if key.startswith("session_") and not key.endswith("_date_time"):
                session_count += 1
                for turn in conv[key]:
                    speakers[turn.get("speaker", "?")] += 1
                    turn_count += 1
        print(f"[{sid}] sessions={session_count}  turns={turn_count}  speakers={dict(speakers)}")

        # Gold evidence format probe
        evs = Counter()
        weird = []
        for q in s.get("qa", []):
            ev = q.get("evidence")
            if not ev:
                evs["empty"] += 1
                continue
            for e in ev:
                if ";" in e or " " in e:
                    weird.append((q["question"][:60], ev))
                    evs["semicolon-joined"] += 1
                    break
                else:
                    evs["clean"] += 1
                    break
        print(f"  evidence format counts: {dict(evs)}")
        if weird:
            print(f"  first 3 weird evidence examples:")
            for q, ev in weird[:3]:
                print(f"    Q: {q!r}")
                print(f"    ev: {ev}")
        print()


# Probe 3: which dia_ids from conv-26 are actually in the DB?
def probe_db_dia_ids():
    print("=" * 72)
    print("DB dia_id PROBE — do we have D11:4 (conv-30)?")
    print("=" * 72)
    db_path = BASE / "memory" / "agent_memory.db"
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()

    # Count rows per user_id
    rows = cur.execute(
        "SELECT user_id, COUNT(*) FROM memory_items WHERE is_deleted = 0 "
        "AND user_id IN ('conv-26','conv-30') GROUP BY user_id"
    ).fetchall()
    print(f"row counts: {dict(rows)}")
    print()

    # Extract dia_ids via JSON metadata
    for sid in ["conv-26", "conv-30"]:
        print(f"[{sid}] sampling dia_ids from metadata...")
        dias = set()
        speakers = Counter()
        for (meta_json,) in cur.execute(
            "SELECT metadata_json FROM memory_items WHERE user_id=? AND is_deleted=0 AND metadata_json IS NOT NULL",
            (sid,),
        ):
            try:
                m = json.loads(meta_json)
            except Exception:
                continue
            d = m.get("dia_id")
            if d: dias.add(d)
            r = m.get("role")
            if r: speakers[r] += 1
        print(f"  unique dia_ids: {len(dias)}")
        print(f"  speakers (role metadata): {dict(speakers)}")
        # Check for D11:4 specifically in conv-30
        if sid == "conv-30":
            print(f"  D11:4 present? {'D11:4' in dias}")
            d_for_session_11 = sorted(d for d in dias if d.startswith("D11:"))
            print(f"  session 11 dia_ids in DB: {d_for_session_11[:30]}")
    con.close()


# Probe 4: what happens with semicolon-joined gold dia_ids
def probe_gold_matching():
    print()
    print("=" * 72)
    print("GOLD-MATCHING PROBE (semicolon-joined evidence strings)")
    print("=" * 72)
    trace_path = BASE / "benchmarks" / "Phase1" / "runs" / "baseline_pre_port" / "retrieval_trace.jsonl"
    n_semi = 0
    n_total = 0
    examples = []
    with open(trace_path, "r", encoding="utf-8") as f:
        for line in f:
            t = json.loads(line)
            n_total += 1
            gold = t.get("gold_dia_ids") or []
            for g in gold:
                if ";" in g or " " in g:
                    n_semi += 1
                    if len(examples) < 5:
                        examples.append({
                            "idx": t["idx"], "cat": t["category"],
                            "gold": gold, "first_gold_rank": t.get("first_gold_rank"),
                        })
                    break
    print(f"questions with malformed gold strings: {n_semi}/{n_total}")
    for e in examples:
        print(f"  idx={e['idx']} cat={e['cat']} first_rank={e['first_gold_rank']} gold={e['gold']}")


if __name__ == "__main__":
    probe_dataset()
    probe_db_dia_ids()
    probe_gold_matching()
