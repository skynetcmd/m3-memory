"""LOCOMO benchmark runner for m3-memory.

Loads the LOCOMO10 dataset, bulk-ingests every conversation turn
into m3-memory scoped by sample_id, then for each question retrieves 
the top-K most relevant turns and asks an LLM to answer. 
An OpenAI judge (default gpt-4o-mini) scores the answer.

Includes:
- Episodic Cluster Expansion (+/- N turns)
- Knowledge Graph Linking (Obs/Sum -> Evidence)
- Graph Expansion (1-hop traversal of retrieved hits)
- Temporal Resolution (relative dates -> absolute)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "bin"))

# Route embeddings to llama-server before memory_core imports.
os.environ.setdefault("LLM_ENDPOINTS_CSV", "http://localhost:1234/v1")

import memory_core  # noqa: E402
from memory_core import (  # noqa: E402
    memory_write_bulk_impl,
    memory_link_impl,
    memory_graph_impl,
    _embed,
    _db,
)
from auth_utils import get_api_key  # noqa: E402
import temporal_utils  # noqa: E402

DEFAULT_DATASET = BASE_DIR / "data" / "locomo" / "locomo10.json"

CATEGORIES = {
    1: "multi-hop",
    2: "temporal",
    3: "open-domain",
    4: "single-hop",
    5: "adversarial"
}

# --- Adaptive Prompts by Category ---

SYSTEM_PROMPTS = {
    "default": (
        "You are an assistant with long-term memory. You are participating in a benchmark. "
        "Use the provided History and Timeline to answer questions. "
        "IMPORTANT: If a 'Temporal Anchor' matches an event mentioned in history, that anchor date is the GROUND TRUTH for when that event happened. "
        "Always use the anchor date over the session date if they differ. "
        "Answer directly based on the evidence; do not infer or calculate beyond what is stated."
    ),
    "temporal": (
        "You are a temporal reasoning expert. Use the provided History, Timeline, and Anchors to answer the question. "
        "1. Identify the reference date (Today's Date).\n"
        "2. Locate events in the history.\n"
        "3. Use the 'Temporal Anchors' to resolve relative terms like 'yesterday' or 'last week' into absolute dates.\n"
        "4. Calculate durations or find the latest event state.\n"
        "Reason step-by-step, then provide a CONCISE final answer."
    ),
    "multi-hop": (
        "You are a multi-session reasoning assistant. You must connect facts across different conversation sessions. "
        "Pay close attention to how a topic mentioned in one session evolves or is referenced in another. "
        "Answer the question based on the cumulative history provided."
    ),
    "adversarial": (
        "You are participating in an adversarial memory benchmark. "
        "Some questions may be unanswerable based on the provided history. "
        "If the information is not present, say 'unanswerable' and explain what is missing. "
        "Do not hallucinate or make up details."
    )
}

ANSWER_TEMPLATE = (
    "Timeline of All Sessions:\n{timeline}\n\n"
    "Temporal Anchors (Resolved Dates Found in Context):\n{anchors}\n\n"
    "Relevant History:\n{history}\n\n"
    "TODAY\\'S DATE IN THE BENCHMARK: {date}\n"
    "Question: {question}\n\n"
    "Reason step-by-step using the anchors and timeline, then provide a CONCISE final answer."
)

def classify_question(query: str) -> str:
    """Regex-based question classifier to route to optimal prompting/retrieval strategy."""
    q = query.lower()
    # Temporal: When, how long, how many days, ago, date, year, month
    if any(x in q for x in ["when", "how long", "how many days", " ago", "date", "year", "month", "last week", "yesterday"]):
        return "temporal"
    # Adversarial: typically contains "not", "never", or is a "what if"
    if any(x in q for x in ["never", "not mentioned", "unanswerable"]):
        return "adversarial"
    # Multi-hop: usually "how did X change", "what is the sequence", "over time"
    if any(x in q for x in ["change", "sequence", "evolution", "progress", "history of"]):
        return "multi-hop"
    return "default"


def judge_prompt(qtype: str, question: str, answer: str, response: str) -> str:
    if qtype == "adversarial":
        return (
            "I will give you an unanswerable question, an explanation, and a response "
            "from a model. Please answer yes if the model correctly identifies the question "
            "as unanswerable.\n\n"
            f"Question: {question}\n\nExplanation: {answer}\n\nModel Response: {response}\n\n"
            "Does the model correctly identify the question as unanswerable? Answer yes or no only."
        )
    return (
        "I will give you a question, a correct answer, and a response from a model. "
        "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
        f"Question: {question}\n\nCorrect Answer: {answer}\n\nModel Response: {response}\n\n"
        "Is the model response correct? Answer yes or no only."
    )

MAX_TURN_CHARS = 6000

async def ingest_sample_with_graph(sample: dict) -> tuple[int, float]:
    sid = sample["sample_id"]
    conv = sample["conversation"]
    obs = sample.get("observation", {})
    sums = sample.get("session_summary", {})
    
    speaker_a = conv.get("speaker_a", "Speaker A")
    speaker_b = conv.get("speaker_b", "Speaker B")
    
    items: list[dict] = []
    dia_map: dict[str, str] = {}
    
    # 1. Build Message Items
    for i in range(1, 36):
        sess_key = f"session_{i}"
        if sess_key not in conv or not conv[sess_key]: continue
        
        sess_date_str = conv.get(f"session_{i}_date_time", "Unknown")
        anchor_dt = temporal_utils.parse_locomo_date(sess_date_str)
        
        for t_idx, turn in enumerate(conv[sess_key]):
            role = speaker_a if turn.get("speaker") == "speaker_a" else speaker_b
            dia_id = turn.get("dia_id")
            mid = str(uuid.uuid4())
            if dia_id: dia_map[dia_id] = mid
            
            content = turn.get("text", "") or ""
            anchors = temporal_utils.resolve_temporal_expressions(content, anchor_dt)
            
            items.append({
                "id": mid,
                "type": "message",
                "title": f"{role}:{sid}:S{i}:T{t_idx}",
                "content": content,
                "user_id": sid,
                "conversation_id": f"{sid}::S{i}",
                "source": "locomo",
                "embed": True,
                "metadata": {
                    "role": role, "session_id": f"S{i}", "session_date": sess_date_str,
                    "session_index": i, "turn_index": t_idx, "dia_id": dia_id,
                    "temporal_anchors": anchors
                }
            })
            
    # 2. Build Observation Items + Links
    obs_links: list[tuple[str, str]] = []
    for i in range(1, 36):
        ok = f"session_{i}_observation"
        if ok not in obs or not obs[ok]: continue
        
        sess_date_str = conv.get(f"session_{i}_date_time", "Unknown")
        anchor_dt = temporal_utils.parse_locomo_date(sess_date_str)
        obs_data = obs[ok]
        if not isinstance(obs_data, dict): continue
        
        for speaker, lines in obs_data.items():
            for line in lines:
                text = line[0]
                ev = line[1]
                anchors = temporal_utils.resolve_temporal_expressions(text, anchor_dt)
                mid = str(uuid.uuid4())
                items.append({
                    "id": mid,
                    "type": "observation",
                    "title": f"Obs:{speaker}:S{i}",
                    "content": text,
                    "user_id": sid,
                    "conversation_id": f"{sid}::S{i}",
                    "source": "locomo_obs",
                    "embed": True,
                    "metadata": {
                        "session_index": i, "session_date": sess_date_str, 
                        "speaker": speaker, "temporal_anchors": anchors
                    }
                })
                ev_list = ev if isinstance(ev, list) else [ev]
                for dia_id in ev_list:
                    if dia_id in dia_map:
                        obs_links.append((mid, dia_map[dia_id]))

    # 3. Build Summary Items
    for i in range(1, 36):
        sk = f"session_{i}_summary"
        if sk not in sums or not sums[sk]: continue
        sess_date_str = conv.get(f"session_{i}_date_time", "Unknown")
        anchor_dt = temporal_utils.parse_locomo_date(sess_date_str)
        content = sums[sk]
        anchors = temporal_utils.resolve_temporal_expressions(content, anchor_dt)
        items.append({
            "type": "note",
            "title": f"Sum:S{i}",
            "content": content,
            "user_id": sid,
            "conversation_id": f"{sid}::S{i}",
            "source": "locomo_sum",
            "embed": True,
            "metadata": {
                "session_index": i, "session_date": sess_date_str,
                "temporal_anchors": anchors
            }
        })

    t0 = time.perf_counter()
    await memory_write_bulk_impl(items)
    await asyncio.sleep(0.1)
    
    count = 0
    with _db() as db:
        if obs_links:
            for from_id, to_id in obs_links:
                res = memory_link_impl(from_id, to_id, "references", db=db)
                if "Linked:" in res: count += 1
        
        prev_sum_id = None
        for i in range(1, 36):
            title = f"Sum:S{i}"
            row = db.execute("SELECT id FROM memory_items WHERE title=? AND user_id=? AND is_deleted=0", (title, sid)).fetchone()
            if row:
                curr_id = row[0]
                if prev_sum_id:
                    memory_link_impl(prev_sum_id, curr_id, "precedes", db=db)
                    count += 1
                prev_sum_id = curr_id
                
    print(f"Created {count} relationships")
    return len(items), time.perf_counter() - t0

async def retrieve_for_question(sid: str, question: str, k: int, cluster_size: int = 0, graph_depth: int = 0, q_signal: str = "default") -> list[dict]:
    from memory_core import memory_search_scored_impl
    
    # Per-category K gating
    actual_k = k
    if q_signal == "temporal": actual_k = max(k, 20)
    elif q_signal == "adversarial": actual_k = min(k, 10)
    
    # Recency bias for temporal and multi-hop
    rb = 0.15 if q_signal in ["temporal", "multi-hop"] else 0.0
    
    ranked = await memory_search_scored_impl(
        question, k=actual_k, user_id=sid, 
        extra_columns=["metadata_json", "conversation_id"],
        recency_bias=rb
    )
    if not ranked: return []
    hits = []
    seen_ids = set()
    for score, item in ranked:
        item["score"] = score
        if "metadata_json" in item:
            item["metadata"] = json.loads(item["metadata_json"] or "{}")
        if "metadata" not in item: item["metadata"] = {}
        hits.append(item)
        seen_ids.add(item["id"])
    
    if graph_depth > 0:
        graph_hits = []
        for h in hits:
            with _db() as db:
                rows = db.execute(
                    "SELECT mi.id, mi.content, mi.title, mi.metadata_json, mi.conversation_id "
                    "FROM memory_items mi JOIN memory_relationships mr ON (mi.id = mr.from_id OR mi.id = mr.to_id) "
                    "WHERE (mr.from_id = ? OR mr.to_id = ?) AND mi.id != ? AND mi.is_deleted = 0",
                    (h["id"], h["id"], h["id"])
                ).fetchall()
                for r in rows:
                    if r["id"] not in seen_ids:
                        seen_ids.add(r["id"])
                        rm = json.loads(r["metadata_json"] or "{}")
                        graph_hits.append({
                            "id": r["id"], "content": r["content"], "title": r["title"],
                            "metadata": rm, "conversation_id": r["conversation_id"],
                            "score": h["score"] * 0.8
                        })
        hits.extend(graph_hits)

    if cluster_size > 0:
        expanded = []
        for h in hits:
            expanded.append(h)
            m = h.get("metadata", {})
            cid = h.get("conversation_id")
            if "turn_index" in m and cid:
                t_idx = m["turn_index"]
                with _db() as db:
                    rows = db.execute(
                        "SELECT id, content, title, metadata_json, conversation_id "
                        "FROM memory_items WHERE conversation_id = ? AND is_deleted = 0",
                        (cid,)
                    ).fetchall()
                    for r in rows:
                        rm = json.loads(r["metadata_json"] or "{}")
                        if "turn_index" in rm and abs(rm["turn_index"] - t_idx) <= cluster_size:
                            if r["id"] not in seen_ids:
                                seen_ids.add(r["id"])
                                expanded.append({
                                    "id": r["id"], "content": r["content"], "title": r["title"],
                                    "metadata": rm, "conversation_id": r["conversation_id"],
                                    "score": h["score"] * 0.9
                                })
        return expanded
    return hits

def format_retrieved(hits: list[dict], q_signal: str = "default") -> tuple[str, str]:
    lines, by_session, obs_and_sums, anchors = [], {}, [], []
    
    # Per-session turn capping: 8 turns max to reduce noise
    MAX_TURNS = 8
    
    for h in hits:
        m = h.get("metadata", {})
        if m.get("temporal_anchors"):
            for a in m["temporal_anchors"]:
                anchors.append(f"- {a['absolute']}: '{a['ref']}' in {h['title']}")
        if m.get("turn_index") is not None:
            session_id = h.get("conversation_id") or "unknown"
            session_list = by_session.setdefault(session_id, [])
            if len(session_list) < MAX_TURNS:
                session_list.append(h)
        else:
            obs_and_sums.append(h)
            # If it's a summary or observation, pull limited dialogue from that session
            if h.get("conversation_id"):
                cid = h["conversation_id"]
                with _db() as db:
                    # Pull first 5 turns (was 10) to reduce noise
                    rows = db.execute("SELECT content, title, metadata_json FROM memory_items WHERE conversation_id=? AND type='message' ORDER BY created_at ASC LIMIT 5", (cid,)).fetchall()
                    for r in rows:
                        rm = json.loads(r["metadata_json"] or "{}")
                        session_list = by_session.setdefault(cid, [])
                        existing_contents = [t["content"] for t in session_list]
                        if r["content"] not in existing_contents and len(session_list) < MAX_TURNS:
                            session_list.append({
                                "content": r["content"], "title": r["title"], "metadata": rm
                            })

    if obs_and_sums:
        lines.append("[Observations and Summaries]")
        # Cap observations to top 5 most relevant
        obs_and_sums.sort(key=lambda x: x.get("score", 0), reverse=True)
        for item in obs_and_sums[:5]:
            date = item.get("metadata", {}).get("session_date", "")
            lines.append(f"({date}) {item['title']}: {item['content']}")
        lines.append("")
    sorted_sessions = sorted(by_session.keys(), key=lambda x: int(x.split("::S")[-1]) if "::S" in x else 0)
    for cid in sorted_sessions:
        turns = by_session[cid]
        turns.sort(key=lambda t: t.get("metadata", {}).get("turn_index", 0))
        date = turns[0].get("metadata", {}).get("session_date", "")
        lines.append(f"[Session on {date}]")
        for t in turns:
            role = t.get("metadata", {}).get("role", "?")
            lines.append(f"{role}: {t['content']}")
        lines.append("")
    return "\n".join(lines).strip(), "\n".join(set(anchors)).strip() or "None found."

def _openai_client(api_key: str):
    try: from openai import OpenAI
    except ImportError: raise SystemExit("pip install openai")
    return OpenAI(api_key=api_key)

async def run(args: argparse.Namespace) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = BASE_DIR / ".scratch" / f"locomo_run_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    hyp_path, results_path, log_path = out_dir / "hypotheses.jsonl", out_dir / "results.json", out_dir / "run.log"
    def log(msg: str):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        with open(log_path, "a", encoding="utf-8") as f: f.write(line + "\n")
    log(f"loading dataset: {args.dataset}")
    with open(args.dataset, "r", encoding="utf-8") as f: dataset = json.load(f)
    if args.limit_samples: dataset = dataset[: args.limit_samples]
    api_key = get_api_key("OPENAI_API_KEY")
    client = _openai_client(api_key)
    if not args.skip_ingest:
        log("phase 1: ingest with graph linking + temporal resolution")
        total_items = 0
        for i, sample in enumerate(dataset):
            n, _ = await ingest_sample_with_graph(sample)
            total_items += n
            log(f"  sample {i+1}/{len(dataset)}: {n} items")
        log(f"ingest done: {total_items} items")
    log("phase 2: retrieve + answer + judge")
    qtype_correct, total_q = {}, 0
    with open(hyp_path, "w", encoding="utf-8") as hyp_f:
        for s_idx, sample in enumerate(dataset):
            sid, qa_list = sample["sample_id"], sample["qa"]
            log(f"processing sample {s_idx+1}/{len(dataset)} (ID: {sid})")
            for q_idx, qa in enumerate(qa_list):
                if args.limit_questions and total_q >= args.limit_questions: break
                total_q += 1
                q, a = qa["question"], qa["answer"]
                qtype_label = CATEGORIES.get(qa.get("category", 0), "unknown")
                qtype_correct.setdefault(qtype_label, [])
                
                # Adaptive routing
                q_signal = classify_question(q)
                sys_prompt = SYSTEM_PROMPTS.get(q_signal, SYSTEM_PROMPTS["default"])
                
                hits = await retrieve_for_question(sid, q, args.k, args.cluster_size, args.graph_depth, q_signal=q_signal)
                last_d = "Unknown"
                for i in range(35, 0, -1):
                    dk = f"session_{i}_date_time"
                    if dk in sample["conversation"]:
                        last_d = sample["conversation"][dk]; break
                history, anchors = format_retrieved(hits, q_signal=q_signal)
                tl_lines = [f"Session {i}: {sample['conversation'][f'session_{i}_date_time']}" for i in range(1, 36) if f'session_{i}_date_time' in sample['conversation']]
                timeline = "\n".join(tl_lines)
                
                messages = [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": ANSWER_TEMPLATE.format(timeline=timeline, anchors=anchors, history=history, date=last_d, question=q)}
                ]
                
                hyp = ""
                for attempt in range(3):
                    try:
                        resp = client.chat.completions.create(model=args.answer_model, messages=messages, temperature=0, max_tokens=800)
                        hyp = (resp.choices[0].message.content or "").strip(); break
                    except Exception: time.sleep(2**attempt)
                correct = False
                for attempt in range(3):
                    try:
                        resp = client.chat.completions.create(model=args.judge_model, messages=[{"role": "user", "content": judge_prompt(qtype_label, q, str(a), hyp)}], temperature=0, max_tokens=10)
                        correct = "yes" in (resp.choices[0].message.content or "").lower(); break
                    except Exception: time.sleep(2**attempt)
                qtype_correct[qtype_label].append(1 if correct else 0)
                hyp_f.write(json.dumps({"sample_id": sid, "question": q, "reference": a, "hypothesis": hyp, "correct": correct, "qtype": qtype_label, "signal": q_signal}, ensure_ascii=False) + "\n")
                hyp_f.flush()
            if args.limit_questions and total_q >= args.limit_questions: break
            log(f"  sample done. running_acc={sum(sum(v) for v in qtype_correct.values())/total_q:.3f}")
    total_correct = sum(sum(v) for v in qtype_correct.values())
    overall = total_correct / total_q if total_q else 0
    summary = {"n_questions": total_q, "overall_accuracy": overall, "per_type": {qt: {"n": len(v), "acc": sum(v)/len(v)} for qt, v in qtype_correct.items()}}
    with open(results_path, "w", encoding="utf-8") as f: json.dump(summary, f, indent=2)
    log(f"overall accuracy: {overall:.4f}\nresults -> {results_path}")

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    p.add_argument("--limit-samples", type=int, default=0)
    p.add_argument("--limit-questions", type=int, default=0)
    p.add_argument("--skip-ingest", action="store_true")
    p.add_argument("--k", type=int, default=20)
    p.add_argument("--cluster-size", type=int, default=5)
    p.add_argument("--graph-depth", type=int, default=1)
    p.add_argument("--answer-model", default="gpt-4o-mini")
    p.add_argument("--judge-model", default="gpt-4o-mini")
    return p.parse_args()

if __name__ == "__main__": asyncio.run(run(parse_args()))
