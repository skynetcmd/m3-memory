"""Long-session QA benchmark runner for m3-memory.

Loads the configured long-session dataset, bulk-ingests every conversation turn
into m3-memory scoped by question_id (so each instance has its own isolated
haystack), then for each question retrieves the top-K most relevant turns and
asks a generator LLM to answer. A separate judge LLM scores the answer using
the per-task judge prompts (model configured by --judge-model or
EVAL_JUDGE_MODEL).

Retrieval pipeline:
  1. Hybrid search (FTS5 BM25 + vector cosine, fused with MMR re-ranking)
  2. Graph expansion (1-hop traversal of knowledge graph from initial hits)
  3. Episodic cluster expansion (+/- N surrounding turns from same session)
  4. Timeline-aware answer prompt for temporal reasoning

Routes embeddings through `memory_write_bulk_impl` / `_embed_many` and expects
llama-server on http://localhost:8081/v1 (override with LLM_ENDPOINTS_CSV).

Usage:
    python bin/bench_longmemeval.py                         # full dataset
    python bin/bench_longmemeval.py --limit 20              # subsample
    python bin/bench_longmemeval.py --skip-ingest           # reuse already-loaded DB
    python bin/bench_longmemeval.py --no-judge              # write hypotheses only
    python bin/bench_longmemeval.py --cluster-size 0 --graph-depth 0  # ablation: hybrid only

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
import re
import sys
import time
import traceback
from collections import Counter
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
    memory_write_impl,
    memory_link_impl,
    memory_search_scored_impl,
    _embed,
    _batch_cosine,
    _unpack,
    _db,
)
from auth_utils import get_api_key  # noqa: E402
import temporal_utils  # noqa: E402

DEFAULT_DATASET = BASE_DIR / "data" / "longmemeval" / "longmemeval_s_cleaned.json"


# ── Answer generation + judge prompts (from upstream LongMemEval) ────────────

# ── System prompts (selected by question signal) ──

SYSTEM_PROMPT_TEMPORAL = (
    "You are an assistant with long-term memory. You are participating in a benchmark. "
    "IMPORTANT: You must use the 'CURRENT DATE' provided below as the absolute reference for 'today'. "
    "Ignore your internal clock. All relative time references (yesterday, last week, etc.) "
    "must be calculated relative to the session date they appear in, using the Timeline and "
    "Temporal Anchors provided. Reason step-by-step about the dates before giving the final answer."
)

SYSTEM_PROMPT_UPDATE = (
    "You are an assistant with long-term memory. You are participating in a benchmark. "
    "The user's preferences and facts may have changed over time across conversations. "
    "When the chat history contains multiple different answers for the same thing, "
    "always prefer the MOST RECENT information. Earlier mentions may be outdated."
)

SYSTEM_PROMPT_PREFERENCE = (
    "You are an assistant with long-term memory. You are participating in a benchmark. "
    "Focus on what the USER said about their own preferences, habits, and personal details. "
    "Ignore generic advice or suggestions from the assistant in the chat history — "
    "only the user's own statements about themselves matter for answering this question."
)

SYSTEM_PROMPT_DEFAULT = (
    "You are an assistant with long-term memory. You are participating in a benchmark. "
    "Answer the question based on the relevant chat history provided. Be concise and accurate."
)

# ── Answer templates ──

ANSWER_TEMPLATE_TEMPORAL = (
    "Session Timeline:\n{timeline}\n\n"
    "Temporal Anchors (Resolved Dates Found in Context):\n{anchors}\n\n"
    "History Chats:\n\n{history}\n\n"
    "CURRENT DATE: {date}\n"
    "Question: {question}\n\n"
    "Reason step-by-step about dates, then provide the final answer.\n"
    "Answer directly and concisely. Do not calculate, infer, or add information "
    "beyond what is stated in the chat history.\nFinal Answer:"
)

ANSWER_TEMPLATE_DEFAULT = (
    "History Chats:\n\n{history}\n\n"
    "Current Date: {date}\n"
    "Question: {question}\n\n"
    "Answer directly and concisely. Do not calculate, infer, or add information "
    "beyond what is stated in the chat history.\nFinal Answer:"
)

# ── Question classification (no oracle metadata) ──

_TEMPORAL_QUERY_RE = re.compile(
    r"\b(when|how long|how many (?:days|weeks|months|years)|"
    r"what date|what time|before|after|since|until|"
    r"first time|last time|most recent|recently|latest|earliest|"
    r"ago|duration|timeline|how old|started|ended|"
    r"how (?:much|many) time|"
    r"which .{0,60} first|"
    r"what (?:is|was) the order|order .* from first|"
    r"who .{0,30} first|"
    r"the (?:most|least) (?:in|during) \w+|"
    r"last (?:saturday|sunday|monday|tuesday|wednesday|thursday|friday|week|weekend|month)|"
    r"past (?:weekend|week|month)|"
    r"(?:valentine|christmas|new year|birthday|holiday)(?:'?s)? day)\b",
    re.IGNORECASE,
)

_UPDATE_QUERY_RE = re.compile(
    r"\b(current(?:ly)?|now(?:adays)?|these days|at the moment|presently|"
    r"still (?:using|have|do|live|work|go)|"
    r"what (?:is|are) my|what (?:do|does|did) I (?:currently|now)|"
    r"updated|changed to|switched to|moved to|"
    r"most recent (?:address|job|name|number|email|score|time|record))\b",
    re.IGNORECASE,
)

_PREFERENCE_QUERY_RE = re.compile(
    r"\b(recommend|suggest|preference|personali[sz]e|"
    r"what (?:kind|type|style|genre|brand) (?:of|do)|"
    r"what (?:do I|would I) (?:like|prefer|enjoy|want)|"
    r"tailor|suited (?:to|for) me|based on (?:my|what I)|"
    r"complement (?:my|the) (?:current|existing)|"
    r"interest(?:ing|ed)|hobby|favorite|favourite)\b",
    re.IGNORECASE,
)


def classify_question(question: str) -> str:
    """Classify question intent to select prompt/retrieval strategy.

    Returns one of: 'temporal', 'update', 'preference', 'default'.
    Temporal takes priority (a temporal update question needs date reasoning).
    """
    if _TEMPORAL_QUERY_RE.search(question):
        return "temporal"
    if _UPDATE_QUERY_RE.search(question):
        return "update"
    if _PREFERENCE_QUERY_RE.search(question):
        return "preference"
    return "default"


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


def build_turn_items(instance: dict, variant: str = "") -> list[dict]:
    """Flatten a LongMemEval instance into turn-level memory_write inputs."""
    qid = instance["question_id"]
    items: list[dict] = []
    sessions: list[list[dict]] = instance["haystack_sessions"]
    session_ids: list[str] = instance["haystack_session_ids"]
    session_dates: list[str] = instance["haystack_dates"]

    for s_idx, (sess_id, sess_date, session) in enumerate(zip(session_ids, session_dates, sessions)):
        anchor_dt = temporal_utils.parse_longmemeval_date(sess_date)
        for t_idx, turn in enumerate(session):
            role = turn.get("role", "user")
            content = turn.get("content", "") or ""
            if len(content) > MAX_TURN_CHARS:
                content = content[:MAX_TURN_CHARS]
            has_answer = bool(turn.get("has_answer", False))
            anchors = temporal_utils.resolve_temporal_expressions(content, anchor_dt)
            item = {
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
                    "temporal_anchors": anchors,
                },
            }
            if variant:
                item["variant"] = variant
            items.append(item)
    return items


async def ingest_instance(instance: dict, variant: str = "", per_item: bool = False) -> tuple[int, float]:
    items = build_turn_items(instance, variant=variant)
    t0 = time.perf_counter()
    if per_item:
        for it in items:
            meta = it.get("metadata", {})
            if isinstance(meta, dict):
                meta = json.dumps(meta)
            await memory_write_impl(
                type=it.get("type", "message"),
                content=it.get("content", ""),
                title=it.get("title", ""),
                metadata=meta,
                source=it.get("source", "longmemeval"),
                embed=it.get("embed", True),
                user_id=it.get("user_id", ""),
                conversation_id=it.get("conversation_id", ""),
                variant=it.get("variant"),
            )
    else:
        await memory_write_bulk_impl(items)
    return len(items), time.perf_counter() - t0


async def retrieve_for_question(
    qid: str, question: str, k: int,
    cluster_size: int = 0, graph_depth: int = 0,
    recency_bias: float = 0.0,
    adaptive_k: bool = False,
) -> list[dict]:
    """Hybrid FTS5+vector search with optional graph expansion and episodic clustering."""
    ranked = await memory_search_scored_impl(
        question, k=k, user_id=qid,
        extra_columns=["metadata_json", "conversation_id"],
        recency_bias=recency_bias,
        adaptive_k=adaptive_k,
    )
    if not ranked:
        return []

    hits = []
    seen_ids: set[str] = set()
    for score, item in ranked:
        item["score"] = score
        if "metadata_json" in item:
            item["metadata"] = json.loads(item["metadata_json"] or "{}")
        if "metadata" not in item:
            item["metadata"] = {}
        hits.append(item)
        seen_ids.add(item["id"])

    # Graph expansion: 1-hop traversal from initial hits
    if graph_depth > 0:
        graph_hits = []
        for h in hits:
            with _db() as db:
                rows = db.execute(
                    "SELECT mi.id, mi.content, mi.title, mi.metadata_json, mi.conversation_id "
                    "FROM memory_items mi JOIN memory_relationships mr "
                    "  ON (mi.id = mr.from_id OR mi.id = mr.to_id) "
                    "WHERE (mr.from_id = ? OR mr.to_id = ?) AND mi.id != ? "
                    "  AND mi.is_deleted = 0",
                    (h["id"], h["id"], h["id"]),
                ).fetchall()
                for r in rows:
                    if r["id"] not in seen_ids:
                        seen_ids.add(r["id"])
                        graph_hits.append({
                            "id": r["id"],
                            "content": r["content"],
                            "title": r["title"],
                            "metadata": json.loads(r["metadata_json"] or "{}"),
                            "conversation_id": r["conversation_id"],
                            "score": h["score"] * 0.8,
                        })
        hits.extend(graph_hits)

    # Episodic cluster expansion: pull surrounding turns from same session
    MIN_EXPANSION_SCORE = 0.3  # don't expand low-quality hits
    MAX_HITS_PER_SESSION = 8   # cap per-session at retrieval level
    if cluster_size > 0:
        expanded = list(hits)
        session_hit_counts: dict[str, int] = {}
        for h in hits:
            cid = h.get("conversation_id", "")
            session_hit_counts[cid] = session_hit_counts.get(cid, 0) + 1
        for h in hits:
            m = h.get("metadata", {})
            cid = h.get("conversation_id")
            if "turn_index" in m and cid:
                t_idx = m["turn_index"]
                with _db() as db:
                    rows = db.execute(
                        "SELECT id, content, title, metadata_json, conversation_id "
                        "FROM memory_items "
                        "WHERE conversation_id = ? AND is_deleted = 0",
                        (cid,),
                    ).fetchall()
                    for r in rows:
                        if r["id"] not in seen_ids:
                            rm = json.loads(r["metadata_json"] or "{}")
                            expansion_score = h["score"] * 0.9
                            if (
                                "turn_index" in rm
                                and abs(rm["turn_index"] - t_idx) <= cluster_size
                                and expansion_score >= MIN_EXPANSION_SCORE
                                and session_hit_counts.get(cid, 0) < MAX_HITS_PER_SESSION
                            ):
                                seen_ids.add(r["id"])
                                session_hit_counts[cid] = session_hit_counts.get(cid, 0) + 1
                                expanded.append({
                                    "id": r["id"],
                                    "content": r["content"],
                                    "title": r["title"],
                                    "metadata": rm,
                                    "conversation_id": r["conversation_id"],
                                    "score": expansion_score,
                                })
        return expanded

    return hits


MAX_TURNS_PER_SESSION = 6  # cap per-session turns to reduce noise


def format_retrieved(
    hits: list[dict],
    q_signal: str = "default",
) -> tuple[str, str]:
    """Return (history_text, temporal_anchors_text).

    q_signal controls noise reduction:
      - 'preference': deprioritize assistant turns (user statements matter most)
      - any: cap turns per session to MAX_TURNS_PER_SESSION by score
    """
    lines: list[str] = []
    by_session: dict[str, list[dict]] = {}
    high_value: list[dict] = []
    anchor_lines: list[str] = []

    for h in hits:
        m = h.get("metadata", {})
        # Collect temporal anchors
        for a in m.get("temporal_anchors", []):
            anchor_lines.append(f"- {a['absolute']}: '{a['ref']}' in {h.get('title', '?')}")
        if m.get("turn_index") is not None:
            by_session.setdefault(h.get("conversation_id") or "unknown", []).append(h)
        else:
            high_value.append(h)

    # For preference questions, deprioritize assistant turns
    if q_signal == "preference":
        for cid in by_session:
            for t in by_session[cid]:
                if t.get("metadata", {}).get("role") == "assistant":
                    t["score"] = t.get("score", 0) * 0.5

    # Ensure user-assistant turn pairs: if a user turn is present, pull adjacent
    # assistant turn from DB (helps single-session-assistant questions where the
    # answer is in the assistant reply but retrieval favored the user question).
    for cid in list(by_session.keys()):
        existing_indices = {t["metadata"].get("turn_index") for t in by_session[cid]}
        turns_to_add: list[dict] = []
        for t in list(by_session[cid]):
            m = t.get("metadata", {})
            if m.get("role") == "user" and "turn_index" in m:
                next_idx = m["turn_index"] + 1
                if next_idx not in existing_indices:
                    with _db() as db:
                        row = db.execute(
                            "SELECT id, content, title, metadata_json, conversation_id "
                            "FROM memory_items "
                            "WHERE conversation_id = ? AND is_deleted = 0 "
                            "  AND json_extract(metadata_json, '$.turn_index') = ?",
                            (cid, next_idx),
                        ).fetchone()
                        if row:
                            rm = json.loads(row["metadata_json"] or "{}")
                            existing_indices.add(next_idx)
                            turns_to_add.append({
                                "id": row["id"],
                                "content": row["content"],
                                "title": row["title"],
                                "metadata": rm,
                                "conversation_id": row["conversation_id"],
                                "score": t.get("score", 0) * 0.85,
                            })
        by_session[cid].extend(turns_to_add)

    # Surface observations/summaries first (hierarchical context)
    if high_value:
        lines.append("[Key Facts and Summaries]")
        high_value.sort(key=lambda x: x.get("metadata", {}).get("session_index", 0))
        for item in high_value:
            date = item.get("metadata", {}).get("session_date", "")
            prefix = f"({date}) " if date else ""
            lines.append(f"{prefix}{item.get('title', '')}: {item['content']}")
        lines.append("")

    # Sort sessions chronologically by session_index
    sorted_sessions = sorted(
        by_session.keys(),
        key=lambda cid: min(
            (t["metadata"].get("session_index", 0) for t in by_session[cid]),
            default=0,
        ),
    )
    n_sessions = len(sorted_sessions)
    for s_num, cid in enumerate(sorted_sessions, 1):
        turns = by_session[cid]
        # Cap turns per session: keep top-scored, then sort by turn_index for output
        # Ensure minimum diversity: keep at least 2 turns from each session
        cap = MAX_TURNS_PER_SESSION
        if n_sessions > 3 and len(turns) > 2:
            cap = max(cap, 2)  # guarantee at least 2 even if cap < 2
        if len(turns) > cap:
            turns.sort(key=lambda t: t.get("score", 0), reverse=True)
            turns = turns[:cap]
        turns.sort(key=lambda t: t["metadata"].get("turn_index", 0))
        date = turns[0]["metadata"].get("session_date", "")
        lines.append(f"[Session on {date} — Session #{s_num} of {n_sessions}]")
        for t in turns:
            role = t["metadata"].get("role", "?")
            lines.append(f"{role}: {t['content']}")
        lines.append("")

    history = "\n".join(lines).strip()
    anchors = "\n".join(sorted(set(anchor_lines))).strip() or "None found."
    return history, anchors


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


def answer_with_llm(
    client, model: str, history: str, date: str, question: str,
    timeline: str = "", anchors: str = "", q_signal: str = "default",
) -> tuple[str, int]:
    if q_signal == "temporal":
        prompt = ANSWER_TEMPLATE_TEMPORAL.format(
            timeline=timeline or "(not available)",
            anchors=anchors or "None found.",
            history=history,
            date=date,
            question=question,
        )
        sys_prompt = SYSTEM_PROMPT_TEMPORAL
    else:
        prompt = ANSWER_TEMPLATE_DEFAULT.format(
            history=history,
            date=date,
            question=question,
        )
        sys_prompt = {
            "update": SYSTEM_PROMPT_UPDATE,
            "preference": SYSTEM_PROMPT_PREFERENCE,
        }.get(q_signal, SYSTEM_PROMPT_DEFAULT)
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": prompt},
    ]
    max_tok = 1200 if q_signal == "temporal" else 800
    t0 = time.perf_counter()
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0,
                max_tokens=max_tok,
            )
            hyp = (resp.choices[0].message.content or "").strip()
            # Strip common prefixes that confuse the judge
            for prefix in ("Final Answer:", "Final answer:", "Answer:"):
                if hyp.startswith(prefix):
                    hyp = hyp[len(prefix):].strip()
                    break
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
    gen_client = None
    judge_client = None
    if not args.generator_model:
        raise SystemExit(
            "generator model is not set — pass --generator-model or set EVAL_GENERATOR_MODEL"
        )
    if not args.no_judge:
        if not args.judge_model:
            raise SystemExit(
                "judge model is not set — pass --judge-model or set EVAL_JUDGE_MODEL"
            )
        api_key = get_api_key("OPENAI_API_KEY")
        if not api_key:
            raise SystemExit("OPENAI_API_KEY not found (env / keyring / vault). Use `bin/setup_secret.py OPENAI_API_KEY`.")
        gen_client = _openai_client(api_key)
        judge_client = gen_client  # same account

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
                n, _dt = await ingest_instance(inst, variant=args.variant, per_item=args.per_item)
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
    signal_dist: Counter = Counter()

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

            q_signal = classify_question(question)
            signal_dist[q_signal] += 1

            # Smart retrieval logic: boost K for temporal reasoning
            effective_k = args.k
            if args.smart_retrieval and q_signal == "temporal":
                effective_k = max(effective_k, 30)  # Significantly larger K for date reasoning

            try:
                hits = await retrieve_for_question(
                    qid, question, effective_k,
                    cluster_size=args.cluster_size,
                    graph_depth=args.graph_depth,
                    recency_bias=0.15 if q_signal == "update" else 0.0,
                    adaptive_k=args.adaptive_k or args.smart_retrieval,
                )
            except Exception as e:
                log(f"  [{qid}] retrieval failed: {e}")
                hits = []

            retrieved_session_ids = {
                h["metadata"].get("session_id", "") for h in hits if h.get("metadata")
            }
            retrieval_hit = bool(evidence_sessions & retrieved_session_ids) if evidence_sessions else None
            if retrieval_hit is not None:
                retrieval_hit_stats.append(1.0 if retrieval_hit else 0.0)

            # Build session timeline for temporal reasoning
            session_dates = inst.get("haystack_dates", [])
            session_ids = inst.get("haystack_session_ids", [])
            timeline_lines = [
                f"Session {idx + 1} ({sid}): {sdate}"
                for idx, (sid, sdate) in enumerate(zip(session_ids, session_dates))
            ]
            timeline = "\n".join(timeline_lines)

            hypothesis = ""
            correct: bool | None = None
            if not args.no_judge:
                history, anchors = format_retrieved(hits, q_signal=q_signal)
                hypothesis, ans_ms = answer_with_llm(
                    gen_client, args.generator_model, history, qdate, question,
                    timeline=timeline, anchors=anchors, q_signal=q_signal,
                )
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
        "cluster_size": args.cluster_size,
        "graph_depth": args.graph_depth,
        "search_mode": "hybrid",
        "smart_retrieval": args.smart_retrieval,
        "adaptive_k": args.adaptive_k,
        "generator_model": args.generator_model,
        "judge_model": args.judge_model,
        "judged": not args.no_judge,
        "overall_accuracy": overall,
        "per_type": per_type,
        "signal_distribution": dict(signal_dist),
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
    log(f"signal distribution: {dict(signal_dist)}")
    log(f"hypotheses -> {hyp_path}")
    log(f"results    -> {results_path}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    p.add_argument("--limit", type=int, default=0, help="subsample first N instances (0 = all)")
    p.add_argument("--skip-ingest", action="store_true")
    p.add_argument("--no-judge", action="store_true")
    p.add_argument("--k", type=int, default=20, help="top-K retrieved turns per question")
    p.add_argument("--adaptive-k", action="store_true", help="Enable elbow trim for adaptive K")
    p.add_argument("--smart-retrieval", action="store_true", help="Enable temporal-aware smart retrieval")
    p.add_argument("--cluster-size", type=int, default=5,
                   help="episodic expansion: pull +/- N surrounding turns (0 = off)")
    p.add_argument("--graph-depth", type=int, default=1,
                   help="graph expansion hops from initial hits (0 = off)")
    p.add_argument("--generator-model",
                   default=os.environ.get("EVAL_GENERATOR_MODEL"))
    p.add_argument("--judge-model",
                   default=os.environ.get("EVAL_JUDGE_MODEL"))
    p.add_argument("--ingest-concurrency", type=int, default=4,
                   help="number of instances to ingest in parallel")
    p.add_argument("--per-item", action="store_true",
                   help="use memory_write_impl per-turn (enables Phase 1 enrichers). "
                        "Much slower than bulk path; default off.")
    p.add_argument("--variant", type=str, default="",
                   help="tag every ingested row with this variant label")
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
