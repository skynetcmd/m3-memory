"""LongMemEval benchmark runner for m3-memory.

Loads the cleaned LongMemEval-S dataset, bulk-ingests every conversation turn
into m3-memory scoped by question_id (so each instance has its own isolated
haystack), then for each question retrieves the top-K most relevant turns and
asks an LLM to answer. An OpenAI judge (default gpt-4o-mini) scores the answer
using the official LongMemEval per-task prompts.

Routes embeddings through the new `memory_write_bulk_impl` / `_embed_many` path
and expects llama-server on http://localhost:8081/v1 (override with
LLM_ENDPOINTS_CSV).

Usage:
    python bin/bench_longmemeval.py                         # full 500 instances
    python bin/bench_longmemeval.py --limit 20              # subsample
    python bin/bench_longmemeval.py --skip-ingest           # reuse already-loaded DB
    python bin/bench_longmemeval.py --no-judge              # write hypotheses only
    python bin/bench_longmemeval.py --judge-only FILE       # judge an existing hyp file

Artifacts go to .scratch/longmemeval_run_<timestamp>/:
    hypotheses.jsonl   one line per question
    results.json       aggregate accuracy + per-type breakdown
    run.log            progress/errors
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "bin"))

# Route embeddings to llama-server before memory_core imports.
os.environ.setdefault("LLM_ENDPOINTS_CSV", "http://localhost:8081/v1")
os.environ.setdefault("EMBED_BULK_CHUNK", "1024")
os.environ.setdefault("EMBED_BULK_CONCURRENCY", "4")

import memory_core  # noqa: E402
from memory_core import (  # noqa: E402
    memory_write_bulk_impl,
    _embed,
    _batch_cosine,
    _unpack,
    _db,
)
from auth_utils import get_api_key  # noqa: E402

DEFAULT_DATASET = BASE_DIR / "data" / "longmemeval" / "longmemeval_s_cleaned.json"


# ── Answer generation + judge prompts (from upstream LongMemEval) ────────────

ANSWER_TEMPLATE = (
    "I will give you several history chats between you and a user. "
    "Please answer the question based on the relevant chat history.\n\n\n"
    "History Chats:\n\n{history}\n\n"
    "Current Date: {date}\nQuestion: {question}\nAnswer:"
)


def judge_prompt(qtype: str, question: str, answer: str, response: str, abstention: bool) -> str:
    if abstention:
        return (
            "I will give you an unanswerable question, an explanation, and a response "
            "from a model. Please answer yes if the model correctly identifies the question "
            "as unanswerable. The model could say that the information is incomplete, or some "
            "other information is given but the asked information is not.\n\n"
            f"Question: {question}\n\nExplanation: {answer}\n\nModel Response: {response}\n\n"
            "Does the model correctly identify the question as unanswerable? Answer yes or no only."
        )
    if qtype in ("single-session-user", "single-session-assistant", "multi-session"):
        return (
            "I will give you a question, a correct answer, and a response from a model. "
            "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
            "If the response is equivalent to the correct answer or contains all the intermediate "
            "steps to get the correct answer, you should also answer yes. If the response only "
            "contains a subset of the information required by the answer, answer no.\n\n"
            f"Question: {question}\n\nCorrect Answer: {answer}\n\nModel Response: {response}\n\n"
            "Is the model response correct? Answer yes or no only."
        )
    if qtype == "temporal-reasoning":
        return (
            "I will give you a question, a correct answer, and a response from a model. "
            "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
            "If the response is equivalent to the correct answer or contains all the intermediate "
            "steps to get the correct answer, you should also answer yes. If the response only "
            "contains a subset of the information required by the answer, answer no. In addition, "
            "do not penalize off-by-one errors for the number of days. If the question asks for the "
            "number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., "
            "predicting 19 days when the answer is 18), the model's response is still correct.\n\n"
            f"Question: {question}\n\nCorrect Answer: {answer}\n\nModel Response: {response}\n\n"
            "Is the model response correct? Answer yes or no only."
        )
    if qtype == "knowledge-update":
        return (
            "I will give you a question, a correct answer, and a response from a model. "
            "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
            "If the response contains some previous information along with an updated answer, the "
            "response should be considered as correct as long as the updated answer is the required "
            "answer.\n\n"
            f"Question: {question}\n\nCorrect Answer: {answer}\n\nModel Response: {response}\n\n"
            "Is the model response correct? Answer yes or no only."
        )
    if qtype == "single-session-preference":
        return (
            "I will give you a question, a rubric for desired personalized response, and a "
            "response from a model. Please answer yes if the response satisfies the desired "
            "response. Otherwise, answer no. The model does not need to reflect all the points in "
            "the rubric. The response is correct as long as it recalls and utilizes the user's "
            "personal information correctly.\n\n"
            f"Question: {question}\n\nRubric: {answer}\n\nModel Response: {response}\n\n"
            "Is the model response correct? Answer yes or no only."
        )
    raise ValueError(f"unknown question_type: {qtype}")


# ── Core helpers ─────────────────────────────────────────────────────────────

# Cap per-turn content so no input exceeds llama-server's ctx window.
# Qwen3-Embedding Q8_0 accepts up to --ctx-size tokens; we launched with 4096.
# Using a conservative character cap (~3 chars/token) so one long turn can't
# poison a whole embedding batch.
MAX_TURN_CHARS = 6000  # ~2000 tokens; fits in a 4096-per-slot ctx with headroom


def build_turn_items(instance: dict) -> list[dict]:
    """Flatten a LongMemEval instance into turn-level memory_write_bulk_impl inputs."""
    qid = instance["question_id"]
    items: list[dict] = []
    sessions: list[list[dict]] = instance["haystack_sessions"]
    session_ids: list[str] = instance["haystack_session_ids"]
    session_dates: list[str] = instance["haystack_dates"]

    for s_idx, (sess_id, sess_date, session) in enumerate(zip(session_ids, session_dates, sessions)):
        for t_idx, turn in enumerate(session):
            role = turn.get("role", "user")
            content = turn.get("content", "") or ""
            if len(content) > MAX_TURN_CHARS:
                content = content[:MAX_TURN_CHARS]
            has_answer = bool(turn.get("has_answer", False))
            items.append(
                {
                    "type": "message",
                    "title": f"{role}:{sess_id}:{t_idx}",
                    "content": content,
                    "user_id": qid,
                    "conversation_id": f"{qid}::{s_idx}",
                    "source": "longmemeval",
                    "embed": True,
                    "metadata": {
                        "role": role,
                        "session_id": sess_id,
                        "session_date": sess_date,
                        "session_index": s_idx,
                        "turn_index": t_idx,
                        "has_answer": has_answer,
                    },
                }
            )
    return items


async def ingest_instance(instance: dict) -> tuple[int, float]:
    items = build_turn_items(instance)
    t0 = time.perf_counter()
    await memory_write_bulk_impl(items)
    return len(items), time.perf_counter() - t0


async def retrieve_for_question(qid: str, question: str, k: int) -> list[dict]:
    """Cosine search scoped to this question's haystack only."""
    q_vec, _ = await _embed(question)
    if not q_vec:
        return []
    with _db() as db:
        rows = db.execute(
            "SELECT mi.id, mi.content, mi.title, mi.metadata_json, mi.conversation_id, "
            "me.embedding "
            "FROM memory_items mi JOIN memory_embeddings me ON mi.id = me.memory_id "
            "WHERE mi.is_deleted = 0 AND mi.user_id = ?",
            (qid,),
        ).fetchall()
    if not rows:
        return []
    vecs = [_unpack(r["embedding"]) for r in rows]
    scores = _batch_cosine(q_vec, vecs)
    scored = [
        {
            "id": r["id"],
            "content": r["content"],
            "title": r["title"],
            "metadata": json.loads(r["metadata_json"] or "{}"),
            "conversation_id": r["conversation_id"],
            "score": float(s),
        }
        for r, s in zip(rows, scores)
    ]
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:k]


def format_retrieved(hits: list[dict]) -> str:
    lines = []
    by_session: dict[str, list[dict]] = {}
    for h in hits:
        by_session.setdefault(h["conversation_id"] or "unknown", []).append(h)
    for cid, turns in by_session.items():
        turns.sort(key=lambda t: t["metadata"].get("turn_index", 0))
        date = turns[0]["metadata"].get("session_date", "")
        lines.append(f"[Session on {date}]")
        for t in turns:
            role = t["metadata"].get("role", "?")
            lines.append(f"{role}: {t['content']}")
        lines.append("")
    return "\n".join(lines).strip()


# ── LLM calls (answer + judge) ───────────────────────────────────────────────

def _openai_client(api_key: str, base_url: str | None = None):
    try:
        from openai import OpenAI
    except ImportError as e:
        raise SystemExit("openai package not installed. `pip install openai`") from e
    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


def answer_with_llm(client, model: str, history: str, date: str, question: str) -> tuple[str, int]:
    prompt = ANSWER_TEMPLATE.format(history=history, date=date, question=question)
    t0 = time.perf_counter()
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=400,
            )
            hyp = (resp.choices[0].message.content or "").strip()
            return hyp, int((time.perf_counter() - t0) * 1000)
        except Exception as e:
            if attempt == 2:
                return f"[ANSWER_ERROR:{type(e).__name__}: {e}]", int((time.perf_counter() - t0) * 1000)
            time.sleep(2 * (2 ** attempt))
    return "[ANSWER_ERROR:unreachable]", 0


def judge_with_llm(client, model: str, qtype: str, question: str, answer: str, hyp: str, abstention: bool) -> bool:
    prompt = judge_prompt(qtype, question, answer, hyp, abstention)
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=10,
            )
            content = (resp.choices[0].message.content or "").strip().lower()
            return "yes" in content
        except Exception as e:
            if attempt == 2:
                print(f"  judge error: {e}", flush=True)
                return False
            time.sleep(2 * (2 ** attempt))
    return False


# ── Runner ───────────────────────────────────────────────────────────────────

async def run(args: argparse.Namespace) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = BASE_DIR / ".scratch" / f"longmemeval_run_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    hyp_path = out_dir / "hypotheses.jsonl"
    results_path = out_dir / "results.json"
    log_path = out_dir / "run.log"

    def log(msg: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    log(f"loading dataset: {args.dataset}")
    with open(args.dataset, "r", encoding="utf-8") as f:
        dataset = json.load(f)
    log(f"  {len(dataset)} instances")

    if args.limit:
        dataset = dataset[: args.limit]
        log(f"  limited to {len(dataset)}")

    # Resolve OpenAI credentials via vault (unless --no-judge).
    answer_client = None
    judge_client = None
    if not args.no_judge:
        api_key = get_api_key("OPENAI_API_KEY")
        if not api_key:
            raise SystemExit("OPENAI_API_KEY not found (env / keyring / vault). Use `bin/setup_secret.py OPENAI_API_KEY`.")
        answer_client = _openai_client(api_key)
        judge_client = answer_client  # same account

    # ── Phase 1: ingest ──
    if args.skip_ingest:
        log("skipping ingest (--skip-ingest)")
    else:
        log(f"phase 1: ingest ({args.ingest_concurrency} instances in parallel)")
        total_items = 0
        done_count = 0
        ingest_start = time.perf_counter()
        sem = asyncio.Semaphore(args.ingest_concurrency)

        async def _one(i: int, inst: dict) -> tuple[int, int]:
            async with sem:
                n, _dt = await ingest_instance(inst)
                return i, n

        tasks = [asyncio.create_task(_one(i, inst)) for i, inst in enumerate(dataset)]
        for fut in asyncio.as_completed(tasks):
            i, n = await fut
            total_items += n
            done_count += 1
            if done_count % 10 == 0 or done_count == len(dataset):
                elapsed = time.perf_counter() - ingest_start
                rate = total_items / elapsed if elapsed else 0
                log(f"  {done_count}/{len(dataset)}  items={total_items}  {rate:.0f}/s")
        log(f"ingest done: {total_items} turns in {time.perf_counter()-ingest_start:.1f}s")

    # ── Phase 2: retrieve + answer + judge ──
    log("phase 2: retrieve + answer + judge")
    qtypes_seen: set[str] = set()
    qtype_correct: dict[str, list[int]] = {}
    retrieval_hit_stats: list[float] = []

    with open(hyp_path, "w", encoding="utf-8") as hyp_f:
        for i, inst in enumerate(dataset):
            qid = inst["question_id"]
            qtype = inst["question_type"]
            question = inst["question"]
            answer = inst["answer"]
            qdate = inst.get("question_date", "")
            abstention = "_abs" in qid
            evidence_sessions = set(inst.get("answer_session_ids", []))

            qtypes_seen.add(qtype)
            qtype_correct.setdefault(qtype, [])

            try:
                hits = await retrieve_for_question(qid, question, args.k)
            except Exception as e:
                log(f"  [{qid}] retrieval failed: {e}")
                hits = []

            retrieved_session_ids = {
                h["metadata"].get("session_id", "") for h in hits if h.get("metadata")
            }
            retrieval_hit = bool(evidence_sessions & retrieved_session_ids) if evidence_sessions else None
            if retrieval_hit is not None:
                retrieval_hit_stats.append(1.0 if retrieval_hit else 0.0)

            hypothesis = ""
            correct: bool | None = None
            if not args.no_judge:
                history = format_retrieved(hits)
                hypothesis, ans_ms = answer_with_llm(answer_client, args.answer_model, history, qdate, question)
                correct = judge_with_llm(
                    judge_client, args.judge_model, qtype, question, answer, hypothesis, abstention
                )
                qtype_correct[qtype].append(1 if correct else 0)

            entry = {
                "question_id": qid,
                "question_type": qtype,
                "question": question,
                "reference_answer": answer,
                "hypothesis": hypothesis,
                "retrieved": [
                    {
                        "id": h["id"],
                        "score": h["score"],
                        "session_id": h["metadata"].get("session_id", ""),
                        "session_date": h["metadata"].get("session_date", ""),
                        "role": h["metadata"].get("role", ""),
                        "has_answer": h["metadata"].get("has_answer", False),
                    }
                    for h in hits
                ],
                "retrieval_session_hit": retrieval_hit,
                "autoeval_label": (
                    None if correct is None
                    else {"model": args.judge_model, "label": bool(correct)}
                ),
            }
            hyp_f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            hyp_f.flush()

            if (i + 1) % 10 == 0 or i == len(dataset) - 1:
                running_correct = sum(sum(v) for v in qtype_correct.values())
                running_total = sum(len(v) for v in qtype_correct.values())
                acc = (running_correct / running_total) if running_total else 0.0
                hit_rate = (sum(retrieval_hit_stats) / len(retrieval_hit_stats)) if retrieval_hit_stats else 0.0
                log(f"  {i+1}/{len(dataset)}  running_acc={acc:.3f}  session_hit_rate={hit_rate:.3f}")

    # ── Phase 3: aggregate ──
    if not args.no_judge:
        total_correct = sum(sum(v) for v in qtype_correct.values())
        total_count = sum(len(v) for v in qtype_correct.values())
        overall = total_correct / total_count if total_count else 0.0
        per_type = {
            qt: {"n": len(vals), "accuracy": (sum(vals) / len(vals)) if vals else 0.0}
            for qt, vals in qtype_correct.items()
        }
    else:
        overall = None
        per_type = {}
    summary = {
        "dataset": str(args.dataset),
        "n_instances": len(dataset),
        "k": args.k,
        "answer_model": args.answer_model,
        "judge_model": args.judge_model,
        "judged": not args.no_judge,
        "overall_accuracy": overall,
        "per_type": per_type,
        "retrieval_session_hit_rate": (
            sum(retrieval_hit_stats) / len(retrieval_hit_stats) if retrieval_hit_stats else None
        ),
        "hypothesis_file": str(hyp_path),
    }
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    log("-- summary --")
    if overall is not None:
        log(f"overall accuracy: {overall:.4f}  ({total_correct}/{total_count})")
        for qt, stats in sorted(per_type.items()):
            log(f"  {qt}: {stats['accuracy']:.4f}  ({stats['n']})")
    else:
        log("(judging skipped — run with --judge-only to score later)")
    if retrieval_hit_stats:
        log(f"session hit-rate @k={args.k}: {summary['retrieval_session_hit_rate']:.4f}")
    log(f"hypotheses -> {hyp_path}")
    log(f"results    -> {results_path}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    p.add_argument("--limit", type=int, default=0, help="subsample first N instances (0 = all)")
    p.add_argument("--skip-ingest", action="store_true")
    p.add_argument("--no-judge", action="store_true")
    p.add_argument("--k", type=int, default=10, help="top-K retrieved turns per question")
    p.add_argument("--answer-model", default="gpt-4o-mini")
    p.add_argument("--judge-model", default="gpt-4o-mini")
    p.add_argument("--ingest-concurrency", type=int, default=4,
                   help="number of instances to ingest in parallel")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted", flush=True)
    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
