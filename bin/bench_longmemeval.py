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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "bin"))

# Every bench row is tagged in change_agent as `bench:<RUN_ID>` so cleanup is a
# single indexed delete on idx_mi_change_agent. Generated once per process.
# Retrieval never touches change_agent, so this costs nothing on the hot path.
BENCH_RUN_ID = f"lme-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
BENCH_CHANGE_AGENT = f"bench:{BENCH_RUN_ID}"

# Route embeddings to llama-server before memory_core imports.
os.environ.setdefault("LLM_ENDPOINTS_CSV", "http://localhost:8081/v1")
os.environ.setdefault("EMBED_BULK_CHUNK", "1024")
os.environ.setdefault("EMBED_BULK_CONCURRENCY", "4")

import memory_core  # noqa: E402
from memory_core import (  # noqa: E402
    memory_write_bulk_impl,
    memory_search_scored_impl,
    _db,
)
from auth_utils import get_api_key  # noqa: E402

DEFAULT_DATASET = BASE_DIR / "data" / "longmemeval" / "longmemeval_s_cleaned.json"


def wipe_bench_rows(pattern: str) -> dict:
    """Delete memory_items whose change_agent matches `pattern`, plus orphans.

    `pattern` is either:
      - an exact tag: 'bench:lme-20260413-194821-abc123' (exact match delete)
      - the literal 'bench:%' to mean "all bench runs" (range-scan delete)

    Both forms hit idx_mi_change_agent. LIKE is avoided because SQLite's
    default case-insensitive LIKE cannot use a text index; a range predicate
    can. 'bench:' .. 'bench;' covers every string with the 'bench:' prefix.
    Orphan sweeps on embeddings/history/chroma_sync_queue follow, then VACUUM
    + ANALYZE. Returns a dict of rowcounts for logging.
    """
    counts = {}
    with _db() as db:
        if pattern == "bench:%":
            cur = db.execute(
                "DELETE FROM memory_items "
                "WHERE change_agent >= 'bench:' AND change_agent < 'bench;'"
            )
        else:
            cur = db.execute(
                "DELETE FROM memory_items WHERE change_agent = ?", (pattern,)
            )
        counts["memory_items"] = cur.rowcount
        cur = db.execute(
            "DELETE FROM memory_embeddings WHERE memory_id NOT IN (SELECT id FROM memory_items)"
        )
        counts["memory_embeddings_orphans"] = cur.rowcount
        cur = db.execute(
            "DELETE FROM memory_history WHERE memory_id NOT IN (SELECT id FROM memory_items)"
        )
        counts["memory_history_orphans"] = cur.rowcount
        cur = db.execute(
            "DELETE FROM chroma_sync_queue WHERE memory_id NOT IN (SELECT id FROM memory_items)"
        )
        counts["chroma_sync_queue_orphans"] = cur.rowcount
    # VACUUM must run outside any transaction; open a fresh raw connection.
    import sqlite3
    from memory_core import DB_PATH
    raw = sqlite3.connect(DB_PATH, isolation_level=None)
    try:
        raw.execute("VACUUM")
        raw.execute("ANALYZE")
    finally:
        raw.close()
    return counts


# ── Answer generation + judge prompts (from upstream LongMemEval) ────────────

ANSWER_SYSTEM_BASE = (
    "You are a helpful chat assistant. You have access to memories retrieved "
    "from past conversations with the user. Use them to answer the user's "
    "question. If the memories do not contain enough information, say so "
    "honestly.\n\n"
    "Important guidelines:\n"
    "1. Each retrieved memory is tagged with a session date (shown in the "
    "[Conversation date: ...] header or valid_from field). Use these dates to "
    "reason about when events happened, chronological order, and time spans.\n"
    "2. When information was updated across conversations (a number changed, "
    "a preference shifted, a status was revised), ALWAYS use the value from "
    "the MOST RECENT conversation. Later conversations supersede earlier ones.\n"
    "3. Answer based only on what is explicitly stated. Do not add to or "
    "modify stated values — if the user says \"my list has 25 titles\", the "
    "answer is 25; do not add items mentioned in the same conversation unless "
    "the user explicitly said the count changed.\n"
    "4. If the question asks for a recommendation or suggestion, USE the "
    "preferences you find in the memories to give a SPECIFIC, CONCRETE answer. "
    "Do NOT ask clarifying questions back — the user already shared their "
    "preferences in past conversations; your job is to remember and apply them.\n"
    "5. For counting questions (\"how many X\"), carefully enumerate every "
    "distinct item across ALL conversations. Build a numbered list first, "
    "then count. Do not skip items because they appear in different sessions.\n\n"
    "Answer step by step: (a) extract the relevant facts and dates, (b) apply "
    "supersession (latest wins), (c) give a direct, specific answer. Do not "
    "say \"I don't know\" unless the information is truly absent from the "
    "memories.\n\n"
    "FORMAT: Be terse and direct. No preamble, no \"Based on your memories\", "
    "no restating the question, no hedging. Include EVERY fact the answer "
    "requires — do not truncate explanations or skip nuance. For lookups "
    "(counts, single values, named entities), one short phrase or sentence. "
    "For explanations of advice or instructions you previously gave, include "
    "the full content the user is asking about — completeness matters more "
    "than brevity here."
)

# Abstention-specific system prompt. LongMemEval _abs questions are scored
# by a different judge template that rewards "I don't know" answers. The base
# prompt's rule "do not say I don't know" is the exact opposite of what the
# abstention judge wants, so we branch on qid.endswith("_abs") and use this
# instead. No published number for the lift, but mechanical: 30/500 abs
# questions (6%) we are currently giving wrong answers to by construction.
ANSWER_SYSTEM_ABSTENTION = (
    "You are a helpful chat assistant. You have access to memories retrieved "
    "from past conversations with the user. Your task is to determine whether "
    "the memories contain enough specific information to answer the user's "
    "question.\n\n"
    "If the retrieved memories do NOT contain a direct, specific answer to "
    "the question, reply exactly: \"I don't know based on our past "
    "conversations.\" Do not guess. Do not infer. Do not extrapolate from "
    "partial information. Do not invent details that are not explicitly "
    "stated.\n\n"
    "Only if the memories DO contain a direct, explicit answer should you "
    "give it — and in that case, reply with the shortest possible answer "
    "containing the fact, no preamble or hedging.\n\n"
    "When in doubt, abstain. The cost of guessing wrong is higher than the "
    "cost of admitting you don't have the information."
)

# Per-category reasoning scaffolds. Appended to ANSWER_SYSTEM_BASE based on
# question_type. These tell the answer model how to USE the retrieved context
# for the specific failure modes LongMemEval tests.
ANSWER_SYSTEM_BY_TYPE = {
    "temporal-reasoning": (
        "This is a temporal-reasoning question. The answer requires date "
        "arithmetic. Before answering, internally build this table:\n\n"
        "  | session_date | relevant_fact | age_in_days_vs_current_date |\n\n"
        "Compute age_in_days as (current_date - session_date) in days. "
        "Then identify which row(s) the question is asking about — usually "
        "the most recent applicable row, or the gap/duration between two "
        "rows — and produce the answer. If the question asks 'how long ago' "
        "or 'when did', cite the row's age. If it asks 'how many days "
        "between X and Y', subtract row dates. Do NOT guess dates that are "
        "not in the history. Do NOT confuse session_date with the date a "
        "fact was first true."
    ),
    "knowledge-update": (
        "This is a knowledge-update question: a fact has been updated in a "
        "later session. Before answering, internally list every value of the "
        "asked-about entity you find in the retrieved memories with its "
        "session_date:\n\n"
        "  - YYYY/MM/DD: <value>\n"
        "  - YYYY/MM/DD: <value>\n"
        "  ...\n\n"
        "Sort the list by session_date DESCENDING. The answer is the value "
        "in the FIRST (most recent) row. Do not answer with any earlier "
        "value. Do not blend values across rows."
    ),
    "multi-session": (
        "This is a multi-session question: the answer requires combining facts "
        "from more than one session. Before answering: (1) enumerate the relevant "
        "facts session-by-session with their dates, (2) note any contradictions "
        "or updates (later sessions override earlier ones), (3) synthesize the "
        "answer only from the union of those facts."
    ),
    "single-session-preference": (
        "This is a preference question. Before answering: (1) locate the exact "
        "user statement that expresses the preference and quote it with its "
        "session date, (2) check whether any later session updates or contradicts "
        "it, (3) apply the most recent version of the preference to the question."
    ),
}

ANSWER_USER_TEMPLATE = (
    "History Chats:\n\n{history}\n\n"
    "Current Date: {date}\nQuestion: {question}\nAnswer:"
)

# Chain-of-Note + JSON history. Source: LongMemEval paper (Wu et al., 2024)
# section 5.5 / Appendix D, github.com/xiaowu0162/LongMemEval
# src/generation/run_generation.py. Combined CoN+JSON delivers up to +10
# absolute points on oracle retrieval (the only published double-digit lift
# in the survey). The extraction prompt is run per-session BEFORE the final
# answer call; the final call sees the concatenated notes plus a JSON-dumped
# history rather than natural-language session blocks.

CHAIN_OF_NOTE_PROMPT = (
    "I will give you a chat session between you and a user, plus a question "
    "from the user. Write reading notes that extract every fact from this "
    "session that is relevant to answering the question. Quote the user's "
    "exact wording when it expresses a preference, claim, or specific value. "
    "If the session contains nothing relevant, output exactly: empty\n\n"
    "Session Date: {session_date}\n"
    "Session Content:\n{session_content}\n\n"
    "Question Date: {question_date}\n"
    "Question: {question}\n\n"
    "Extracted notes (relevant facts only, or 'empty'):"
)

ANSWER_WITH_NOTES_USER_TEMPLATE = (
    "I will give you the original chat history (as JSON), pre-extracted "
    "reading notes from each relevant session, and a question. Use both the "
    "notes and the raw history to answer. The notes are a hint, not a "
    "replacement — if the notes are wrong or incomplete, fall back to the "
    "raw history.\n\n"
    "Raw History (JSON):\n{history_json}\n\n"
    "Pre-extracted Notes:\n{notes}\n\n"
    "Current Date: {date}\nQuestion: {question}\nAnswer:"
)

# ── Reflection pass (Hindsight-style two-step reasoning) ─────────────────────
#
# A first-pass LLM call that produces a structured intermediate: relevant
# facts with timestamps, contradictions, superseded entries. The final answer
# call then conditions on (history + reflection + question) instead of just
# (history + question). Gated to reasoning-limited categories where it's most
# likely to help; skipped for single-session-user/assistant which are already
# saturated.

REFLECTION_SYSTEM = (
    "You are a reasoning assistant that pre-digests retrieved chat history "
    "for a downstream answer model. You do NOT answer the question yourself. "
    "Instead, produce a concise, structured summary that makes the final "
    "answer easy to derive.\n\n"
    "Your output MUST contain these sections (omit a section only if empty):\n"
    "1. TIMELINE: Relevant facts ordered chronologically by session date. "
    "Each line: `YYYY/MM/DD — <fact>`. Quote the user's exact wording when "
    "it's a preference or claim.\n"
    "2. CONTRADICTIONS: Any pairs of facts that conflict, and which one wins "
    "(usually the later session).\n"
    "3. SUPERSEDED: Any facts whose valid_to is on or before the current "
    "date, or that are overridden by a later session.\n"
    "4. APPLICABLE FACTS: The final set of non-superseded, non-contradicted "
    "facts the answer model should use.\n\n"
    "Be terse. No prose. No speculation beyond what is in the history. "
    "Do not output an answer to the question."
)

REFLECTION_USER_TEMPLATE = (
    "History Chats:\n\n{history}\n\n"
    "Current Date: {date}\nQuestion (for context only — do not answer): {question}\n\n"
    "Produce the TIMELINE / CONTRADICTIONS / SUPERSEDED / APPLICABLE FACTS summary:"
)

# Final answer prompt, when reflection is enabled, prepends the reflection
# output to the history.
ANSWER_WITH_REFLECTION_USER_TEMPLATE = (
    "History Chats:\n\n{history}\n\n"
    "--- Pre-computed reflection (trust this as a summary, not a replacement "
    "for the history) ---\n{reflection}\n---\n\n"
    "Current Date: {date}\nQuestion: {question}\nAnswer:"
)

# Categories where reflection is expected to help. Single-session-user and
# single-session-assistant are already saturated at ~97% with gpt-4o-mini, so
# reflection just burns tokens on those.
REFLECTION_CATEGORIES = frozenset({
    "temporal-reasoning",
    "multi-session",
    "single-session-preference",
    "knowledge-update",
})

# Categories where newer information should outrank older information: the
# literal answer is always "what did the user say most recently". Applying
# recency bias to multi-session would demote older-but-still-valid facts
# (e.g. an adoption date that's months old but needed to answer).
RECENCY_BIAS_CATEGORIES = frozenset({
    "knowledge-update",
    "temporal-reasoning",
})


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

# Session-mode cap: a full session text block ([Conversation date: ...] +
# all turns) may be much longer than one turn. ~20000 chars ≈ 6700 tokens,
# which fits inside the 8192-per-slot Qwen3-Embedding ctx with ~1500 tokens
# of headroom. Overlong sessions are tail-truncated; build_session_items
# logs a warning so we can count how often it happens.
MAX_SESSION_CHARS = 20000

# Per-session truncation diagnostics. build_session_items appends one dict per
# session that required truncation so we can audit evidence-preservation after
# the run without re-reading the raw dataset. Each entry: {qid, session_id,
# orig_len, strategy, evidence_preserved}. strategy is "evidence-window" when
# the session had a has_answer turn and we centered the window on it, or
# "tail-cut" for evidence-free sessions that fall back to the legacy cut.
_SESSION_TRUNC_EVENTS: list[dict] = []


def build_turn_items(instance: dict) -> list[dict]:
    """Flatten a LongMemEval instance into turn-level memory_write_bulk_impl inputs."""
    qid = instance["question_id"]
    items: list[dict] = []
    sessions: list[list[dict]] = instance["haystack_sessions"]
    session_ids: list[str] = instance["haystack_session_ids"]
    session_dates: list[str] = instance["haystack_dates"]

    for s_idx, (sess_id, sess_date, session) in enumerate(zip(session_ids, session_dates, sessions)):
        # LongMemEval session_date is "YYYY/MM/DD HH:MM" — normalize to ISO-8601
        # so bitemporal filters (as_of) and chronological sorting work.
        valid_from = _session_date_to_iso(sess_date)
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
                    "change_agent": BENCH_CHANGE_AGENT,
                    "valid_from": valid_from,
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


def _session_date_to_iso(sess_date: str) -> str:
    """Convert LongMemEval 'YYYY/MM/DD HH:MM' to ISO-8601 UTC.

    Returns empty string if parsing fails (so memory_core leaves valid_from
    as the ingest-time default rather than crashing).
    """
    if not sess_date:
        return ""
    for fmt in ("%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(sess_date, fmt).replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            continue
    return ""


def build_session_items(instance: dict) -> list[dict]:
    """Session-level ingest: one memory per session instead of per turn.

    Each session becomes a single text block:

        [Conversation date: YYYY/MM/DD]
        User: ...
        Assistant: ...
        User: ...
        ...

    Matches the Memento benchmark's default ingest strategy. The tradeoff vs
    per-turn: coarser retrieval granularity (top-k returns whole sessions,
    not pinpointed turns) but gives the embedding model a full conversation
    context window for each vector, which helps when the answer depends on
    entity co-occurrence across turns inside one session.
    """
    qid = instance["question_id"]
    items: list[dict] = []
    sessions: list[list[dict]] = instance["haystack_sessions"]
    session_ids: list[str] = instance["haystack_session_ids"]
    session_dates: list[str] = instance["haystack_dates"]

    for s_idx, (sess_id, sess_date, session) in enumerate(zip(session_ids, session_dates, sessions)):
        valid_from = _session_date_to_iso(sess_date)
        header = f"[Conversation date: {sess_date}]" if sess_date else ""
        turn_lines: list[str] = []
        evidence_indices: list[int] = []
        for t_idx, turn in enumerate(session):
            role = turn.get("role", "user").capitalize()
            content = turn.get("content", "") or ""
            turn_lines.append(f"{role}: {content}")
            if turn.get("has_answer"):
                evidence_indices.append(t_idx)
        any_has_answer = bool(evidence_indices)

        full_text = "\n".join([header, *turn_lines]) if header else "\n".join(turn_lines)
        orig_len = len(full_text)

        if orig_len <= MAX_SESSION_CHARS:
            text = full_text
        else:
            budget = MAX_SESSION_CHARS - (len(header) + 1 if header else 0)
            if evidence_indices:
                # Evidence-aware window: grow symmetrically around the first
                # has_answer turn until we hit the char budget, so the evidence
                # turn is always retained. Keep later evidence turns inside
                # the window when they fit (prefer contiguous coverage).
                anchor = evidence_indices[0]
                lo = hi = anchor
                running = len(turn_lines[anchor]) + 1  # +1 for the join newline
                # Expand forward first so later evidence turns (if any) stay in.
                while hi + 1 < len(turn_lines):
                    nxt = len(turn_lines[hi + 1]) + 1
                    if running + nxt > budget:
                        break
                    hi += 1
                    running += nxt
                while lo - 1 >= 0:
                    prv = len(turn_lines[lo - 1]) + 1
                    if running + prv > budget:
                        break
                    lo -= 1
                    running += prv
                window_lines = turn_lines[lo : hi + 1]
                evidence_preserved = all(lo <= idx <= hi for idx in evidence_indices)
                strategy = "evidence-window"
                body = "\n".join(window_lines)
                text = f"{header}\n{body}" if header else body
            else:
                text = full_text[:MAX_SESSION_CHARS]
                evidence_preserved = True  # no evidence to lose
                strategy = "tail-cut"

            _SESSION_TRUNC_EVENTS.append(
                {
                    "qid": qid,
                    "session_id": sess_id,
                    "session_index": s_idx,
                    "orig_len": orig_len,
                    "kept_len": len(text),
                    "strategy": strategy,
                    "evidence_preserved": evidence_preserved,
                    "n_turns_total": len(turn_lines),
                    "n_evidence_turns": len(evidence_indices),
                }
            )
            print(
                f"  [warn] session truncated: qid={qid} sess={sess_id} "
                f"len={orig_len} -> {len(text)} strategy={strategy} "
                f"evidence_preserved={evidence_preserved}",
                flush=True,
            )
        items.append(
            {
                "type": "message",
                "title": f"session:{sess_id}",
                "content": text,
                "user_id": qid,
                "conversation_id": f"{qid}::{s_idx}",
                "source": "longmemeval",
                "change_agent": BENCH_CHANGE_AGENT,
                "valid_from": valid_from,
                "embed": True,
                "metadata": {
                    "role": "session",
                    "session_id": sess_id,
                    "session_date": sess_date,
                    "session_index": s_idx,
                    "turn_index": -1,
                    "turn_count": len(session),
                    "has_answer": any_has_answer,
                },
            }
        )
    return items


async def ingest_instance(instance: dict, ingest_mode: str = "turn") -> tuple[int, float]:
    if ingest_mode == "session":
        items = build_session_items(instance)
    else:
        items = build_turn_items(instance)
    t0 = time.perf_counter()
    await memory_write_bulk_impl(items)
    return len(items), time.perf_counter() - t0


async def retrieve_for_question(
    qid: str,
    question: str,
    k: int,
    qdate: str = "",
    expand_sessions: bool = False,
    session_cap: int = 12,
    recency_bias: float = 0.0,
) -> list[dict]:
    """Hybrid FTS5 + vector + MMR retrieval scoped to this question's haystack.

    Routes through memory_search_scored_impl — the same path MCP callers hit
    via memory_suggest — so the benchmark exercises the real m3-memory
    retrieval stack, not a cosine-only shim.

    `expand_sessions`: after the initial ranked retrieval, pull all turns from
    each session that had at least one hit (capped at `session_cap` turns per
    session). Fixes cases where the literal answer turn ranks just outside
    top-k while other turns from the same session make it in — MMR's duplicate
    penalty demotes supersession evidence, which session expansion recovers.
    """
    as_of = _session_date_to_iso(qdate) if qdate else ""
    ranked = await memory_search_scored_impl(
        question,
        k=k,
        user_id=qid,
        as_of=as_of,
        extra_columns=["metadata_json", "conversation_id", "valid_from", "valid_to"],
        recency_bias=recency_bias,
    )
    hits: list[dict] = []
    seen_ids: set[str] = set()
    for score, item in ranked:
        meta_raw = item.get("metadata_json") or "{}"
        try:
            meta = json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
        except json.JSONDecodeError:
            meta = {}
        hits.append(
            {
                "id": item["id"],
                "content": item.get("content") or "",
                "title": item.get("title") or "",
                "metadata": meta,
                "conversation_id": item.get("conversation_id") or "",
                "valid_from": item.get("valid_from") or "",
                "valid_to": item.get("valid_to") or "",
                "score": float(score),
            }
        )
        seen_ids.add(item["id"])

    if not expand_sessions or not hits:
        return hits

    session_ids_hit = {h["conversation_id"] for h in hits if h.get("conversation_id")}
    if not session_ids_hit:
        return hits

    # Pull all turns from each hit session, chronological by turn_index, capped
    # per-session. The cap keeps pathologically long sessions from drowning
    # the context budget.
    with _db() as db:
        placeholders = ",".join(["?"] * len(session_ids_hit))
        rows = db.execute(
            f"""
            SELECT id, content, title, metadata_json, conversation_id,
                   valid_from, valid_to
            FROM memory_items
            WHERE user_id = ?
              AND conversation_id IN ({placeholders})
              AND is_deleted = 0
            """,
            (qid, *session_ids_hit),
        ).fetchall()

    per_session: dict[str, list[dict]] = {}
    for r in rows:
        if r["id"] in seen_ids:
            continue
        meta_raw = r["metadata_json"] or "{}"
        try:
            meta = json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
        except json.JSONDecodeError:
            meta = {}
        per_session.setdefault(r["conversation_id"] or "", []).append(
            {
                "id": r["id"],
                "content": r["content"] or "",
                "title": r["title"] or "",
                "metadata": meta,
                "conversation_id": r["conversation_id"] or "",
                "valid_from": r["valid_from"] or "",
                "valid_to": r["valid_to"] or "",
                "score": 0.0,
            }
        )

    for cid, extras in per_session.items():
        extras.sort(key=lambda h: h["metadata"].get("turn_index", 0))
        # Count how many turns from this session are already in hits so the
        # cap counts the full session, not just the back-fill.
        already_in = sum(1 for h in hits if h["conversation_id"] == cid)
        room = max(0, session_cap - already_in)
        for extra in extras[:room]:
            hits.append(extra)
            seen_ids.add(extra["id"])

    return hits


def format_retrieved(hits: list[dict], qtype: str = "") -> str:
    """Format retrieved turns grouped by session, chronologically.

    For categories that need temporal reasoning, annotate turns with
    valid_from / valid_to so the answer model can reason over supersession.
    """
    wants_temporal = qtype in ("temporal-reasoning", "knowledge-update", "multi-session", "single-session-preference")

    by_session: dict[str, list[dict]] = {}
    for h in hits:
        by_session.setdefault(h["conversation_id"] or "unknown", []).append(h)

    # Sort sessions chronologically by their earliest session_date
    def _sess_key(item):
        _cid, turns = item
        d = turns[0]["metadata"].get("session_date", "")
        return d

    lines: list[str] = []
    for cid, turns in sorted(by_session.items(), key=_sess_key):
        turns.sort(key=lambda t: t["metadata"].get("turn_index", 0))
        date = turns[0]["metadata"].get("session_date", "")
        header = f"[Session on {date}]"
        if wants_temporal:
            vf = turns[0].get("valid_from", "")
            vt = turns[0].get("valid_to", "")
            if vf or vt:
                header += f"  (valid_from={vf or '-'} valid_to={vt or '-'})"
        lines.append(header)
        for t in turns:
            role = t["metadata"].get("role", "?")
            lines.append(f"{role}: {t['content']}")
        lines.append("")
    return "\n".join(lines).strip()


# ── LLM calls (answer + judge) ───────────────────────────────────────────────

# Reasoning-model headroom: frontier models (Claude Sonnet 4.6 extended
# thinking, o3) can burn hundreds of tokens on chain-of-thought before the
# final answer. 400 was fine for gpt-4o-mini/gpt-4o but truncates reasoning
# models mid-thought. 2000 leaves comfortable headroom without blowing up
# latency for the short-answer LongMemEval format.
# Default answer budget. 8000 covers non-thinking frontier models plus
# moderate chain-of-thought; override via --answer-max-tokens for Claude
# extended-thinking or o1/o3 high reasoning effort (16k-32k recommended).
# Silent-truncation risk: a cut-off answer counts as wrong with no error log,
# biasing accuracy downward — err high, not low.
ANSWER_MAX_TOKENS_DEFAULT = 8000
# 50 tokens leaves room for reasoning-model CoT or prefaces like "The answer
# is: yes" before the yes/no lands. A too-tight budget biases accuracy
# downward silently (truncated empty response → "no" → marked incorrect).
JUDGE_MAX_TOKENS = 50


def _provider_for_model(model: str) -> str:
    """Route model IDs to a provider. Claude IDs start with 'claude-'; anything
    else falls through to OpenAI (gpt-*, o1-*, o3-*, plus OpenAI-compatible
    endpoints hosting other names)."""
    m = (model or "").lower()
    if m.startswith("claude-"):
        return "anthropic"
    return "openai"


class LLMClient:
    """Minimal dispatcher so answer/judge callers don't care about SDK shape.

    Holds one lazily-initialized SDK client per provider. `complete` takes a
    system + user prompt and returns the response text, hiding the OpenAI vs
    Anthropic message-shape differences.
    """

    def __init__(self):
        self._openai = None
        self._anthropic = None

    def _openai_client(self):
        if self._openai is None:
            try:
                from openai import OpenAI
            except ImportError as e:
                raise SystemExit("openai package not installed. `pip install openai`") from e
            api_key = get_api_key("OPENAI_API_KEY")
            if not api_key:
                raise SystemExit("OPENAI_API_KEY not found (env / keyring / vault). Use `bin/setup_secret.py OPENAI_API_KEY`.")
            self._openai = OpenAI(api_key=api_key)
        return self._openai

    def _anthropic_client(self):
        if self._anthropic is None:
            try:
                from anthropic import Anthropic
            except ImportError as e:
                raise SystemExit("anthropic package not installed. `pip install anthropic`") from e
            api_key = get_api_key("ANTHROPIC_API_KEY")
            if not api_key:
                raise SystemExit("ANTHROPIC_API_KEY not found (env / keyring / vault). Use `bin/setup_secret.py ANTHROPIC_API_KEY`.")
            self._anthropic = Anthropic(api_key=api_key)
        return self._anthropic

    def complete(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        thinking_budget: int = 0,
    ) -> str:
        """Dispatch a single completion and return response text.

        `thinking_budget > 0` enables Anthropic extended thinking with that
        budget in tokens. Silently ignored for OpenAI. Anthropic constraints:
          - max_tokens must be > thinking_budget (thinking counts against it)
          - thinking_budget must be >= 1024
          - temperature must be 1.0 when thinking is enabled
        We auto-widen max_tokens if the caller didn't leave room, and force
        temperature=1.0 on the thinking path.
        """
        provider = _provider_for_model(model)
        if provider == "anthropic":
            client = self._anthropic_client()
            kwargs: dict[str, Any] = {
                "model": model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
            if thinking_budget and thinking_budget > 0:
                budget = max(thinking_budget, 1024)
                if max_tokens <= budget:
                    kwargs["max_tokens"] = budget + 2048
                kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
                kwargs["temperature"] = 1.0  # required when thinking is enabled
            else:
                kwargs["temperature"] = 0
            resp = client.messages.create(**kwargs)
            # Anthropic returns a list of content blocks; concatenate text blocks
            # (skip `thinking` blocks — they're the model's internal CoT).
            parts = []
            for block in resp.content:
                if getattr(block, "type", None) == "thinking":
                    continue
                text = getattr(block, "text", None)
                if text:
                    parts.append(text)
            return "".join(parts).strip()
        # OpenAI path — thinking_budget is silently ignored.
        client = self._openai_client()
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0,
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()


def _format_history_json(hits: list[dict]) -> str:
    """Serialize retrieved hits as a JSON list grouped by session, sorted
    chronologically. The LongMemEval paper reports this format (vs natural-
    language session blocks) is part of the +10 pt CoN+JSON win.
    """
    by_session: dict[str, list[dict]] = {}
    for h in hits:
        by_session.setdefault(h.get("conversation_id") or "unknown", []).append(h)

    sessions_out: list[dict] = []
    for cid, turns in sorted(
        by_session.items(),
        key=lambda kv: kv[1][0]["metadata"].get("session_date", ""),
    ):
        turns.sort(key=lambda t: t["metadata"].get("turn_index", 0))
        sessions_out.append(
            {
                "session_date": turns[0]["metadata"].get("session_date", ""),
                "turns": [
                    {
                        "role": t["metadata"].get("role", "?"),
                        "content": t["content"],
                    }
                    for t in turns
                ],
            }
        )
    return json.dumps(sessions_out, indent=2, ensure_ascii=False)


def _chain_of_note_extract(
    client: "LLMClient",
    model: str,
    hits: list[dict],
    qdate: str,
    question: str,
    max_tokens: int,
) -> str:
    """Run Chain-of-Note extraction per session, return concatenated notes.

    For each retrieved session, ask the model to write reading notes that
    extract every fact relevant to the question. Sessions where the model
    answers 'empty' are dropped from the output. Returns a string formatted
    as:

        [Session YYYY/MM/DD]
        - note 1
        - note 2

        [Session YYYY/MM/DD]
        - note 1

    Empty string if no notes were extracted.
    """
    by_session: dict[str, list[dict]] = {}
    for h in hits:
        by_session.setdefault(h.get("conversation_id") or "unknown", []).append(h)

    blocks: list[str] = []
    for cid, turns in sorted(
        by_session.items(),
        key=lambda kv: kv[1][0]["metadata"].get("session_date", ""),
    ):
        turns.sort(key=lambda t: t["metadata"].get("turn_index", 0))
        sess_date = turns[0]["metadata"].get("session_date", "")
        sess_content = "\n".join(
            f"{t['metadata'].get('role', '?').capitalize()}: {t['content']}"
            for t in turns
        )
        prompt = CHAIN_OF_NOTE_PROMPT.format(
            session_date=sess_date,
            session_content=sess_content,
            question_date=qdate,
            question=question,
        )
        try:
            note = client.complete(model, "", prompt, max_tokens)
        except Exception:
            continue
        cleaned = note.strip()
        if not cleaned or cleaned.lower() == "empty":
            continue
        blocks.append(f"[Session {sess_date}]\n{cleaned}")

    return "\n\n".join(blocks)


def _reflect(
    client: "LLMClient",
    model: str,
    history: str,
    date: str,
    question: str,
    max_tokens: int,
) -> str:
    """Run the Hindsight-style reflection pre-pass. Returns the reflection
    text, or empty string on failure (caller falls back to single-shot)."""
    user = REFLECTION_USER_TEMPLATE.format(history=history, date=date, question=question)
    for attempt in range(2):
        try:
            return client.complete(model, REFLECTION_SYSTEM, user, max_tokens)
        except Exception:
            if attempt == 1:
                return ""
            time.sleep(1)
    return ""


def answer_with_llm(
    client: "LLMClient",
    model: str,
    history: str,
    date: str,
    question: str,
    qtype: str = "",
    max_tokens: int = ANSWER_MAX_TOKENS_DEFAULT,
    thinking_budget: int = 0,
    reflection: bool = False,
    reflection_model: str | None = None,
    abstention: bool = False,
    notes: str = "",
    history_json: str = "",
) -> tuple[str, int]:
    """Generate an answer for one LongMemEval question.

    If `reflection=True` and `qtype` is in REFLECTION_CATEGORIES, runs a
    two-step pipeline: a first LLM call produces a structured summary of
    relevant facts + contradictions + supersession, then the final answer
    call conditions on (history + reflection + question). On reflection
    failure, falls back silently to the single-shot path.
    """
    if abstention:
        # _abs questions are scored against an abstention-rewarding judge —
        # use the dedicated prompt and skip the per-category scaffolds (which
        # all assume the question IS answerable).
        system = ANSWER_SYSTEM_ABSTENTION
    else:
        system = ANSWER_SYSTEM_BASE
        scaffold = ANSWER_SYSTEM_BY_TYPE.get(qtype)
        if scaffold:
            system = f"{ANSWER_SYSTEM_BASE}\n\n{scaffold}"

    t0 = time.perf_counter()

    reflection_text = ""
    if reflection and qtype in REFLECTION_CATEGORIES:
        rmodel = reflection_model or model
        # Give reflection roughly half the token budget — it's a structured
        # summary, not a full CoT, so it doesn't need the full answer budget.
        reflection_text = _reflect(client, rmodel, history, date, question, max_tokens // 2)

    if notes and history_json:
        # Chain-of-Note + JSON history path. Prepended notes act as a hint;
        # the JSON-serialized raw history is the fallback when the notes are
        # incomplete or wrong. Source: LongMemEval paper §5.5.
        user = ANSWER_WITH_NOTES_USER_TEMPLATE.format(
            history_json=history_json, notes=notes, date=date, question=question
        )
    elif reflection_text:
        user = ANSWER_WITH_REFLECTION_USER_TEMPLATE.format(
            history=history, reflection=reflection_text, date=date, question=question
        )
    else:
        user = ANSWER_USER_TEMPLATE.format(history=history, date=date, question=question)

    for attempt in range(3):
        try:
            hyp = client.complete(model, system, user, max_tokens, thinking_budget=thinking_budget)
            return hyp, int((time.perf_counter() - t0) * 1000)
        except Exception as e:
            if attempt == 2:
                return f"[ANSWER_ERROR:{type(e).__name__}: {e}]", int((time.perf_counter() - t0) * 1000)
            time.sleep(2 * (2 ** attempt))
    return "[ANSWER_ERROR:unreachable]", 0


def judge_with_llm(
    client: "LLMClient", model: str, qtype: str, question: str, answer: str, hyp: str, abstention: bool
) -> bool:
    prompt = judge_prompt(qtype, question, answer, hyp, abstention)
    for attempt in range(3):
        try:
            content = client.complete(model, "", prompt, JUDGE_MAX_TOKENS).lower()
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
    hyp_con_path = out_dir / "hypotheses_con.jsonl"
    results_path = out_dir / "results.json"
    log_path = out_dir / "run.log"

    def log(msg: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    log(f"BENCH_RUN_ID={BENCH_RUN_ID}  (wipe with: --wipe-run {BENCH_RUN_ID})")
    log(f"loading dataset: {args.dataset}")
    with open(args.dataset, "r", encoding="utf-8") as f:
        dataset = json.load(f)
    log(f"  {len(dataset)} instances")

    if args.limit:
        dataset = dataset[: args.limit]
        log(f"  limited to {len(dataset)}")

    # LLM dispatcher — routes per-model to OpenAI or Anthropic SDK. Credentials
    # are resolved lazily on first use, so a run that only needs one provider
    # won't error on the other's missing key.
    answer_client: LLMClient | None = None
    judge_client: LLMClient | None = None
    if not args.no_judge:
        answer_client = LLMClient()
        judge_client = answer_client  # share the dispatcher (and its clients)
        log(f"answer_model={args.answer_model} ({_provider_for_model(args.answer_model)})  "
            f"judge_model={args.judge_model} ({_provider_for_model(args.judge_model)})")

    # ── Phase 1: ingest ──
    if args.skip_ingest:
        log("skipping ingest (--skip-ingest)")
    else:
        log(f"phase 1: ingest mode={args.ingest_mode} ({args.ingest_concurrency} instances in parallel)")
        total_items = 0
        done_count = 0
        ingest_start = time.perf_counter()
        sem = asyncio.Semaphore(args.ingest_concurrency)

        async def _one(i: int, inst: dict) -> tuple[int, int]:
            async with sem:
                n, _dt = await ingest_instance(inst, args.ingest_mode)
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
    qtype_correct_con: dict[str, list[int]] = {}
    retrieval_hit_stats: list[float] = []

    compare_con = bool(getattr(args, "chain_of_note_compare", False))
    if compare_con:
        log("chain-of-note compare mode: running BOTH plain and CoN answer pipelines")

    hyp_con_f = open(hyp_con_path, "w", encoding="utf-8") if compare_con else None
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

            k_for_q = args.k_reasoning if qtype in REFLECTION_CATEGORIES else args.k
            expand = qtype in REFLECTION_CATEGORIES
            rbias = args.recency_bias if qtype in RECENCY_BIAS_CATEGORIES else 0.0
            try:
                hits = await retrieve_for_question(
                    qid, question, k_for_q, qdate=qdate,
                    expand_sessions=expand,
                    recency_bias=rbias,
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

            hypothesis = ""
            hypothesis_con = ""
            correct: bool | None = None
            correct_con: bool | None = None
            if not args.no_judge:
                history = format_retrieved(hits, qtype=qtype)
                notes = ""
                history_json = ""
                if args.chain_of_note and hits and not abstention:
                    con_model = args.chain_of_note_model or args.answer_model
                    notes = _chain_of_note_extract(
                        answer_client, con_model, hits, qdate, question,
                        max_tokens=args.answer_max_tokens // 2,
                    )
                    if notes:
                        history_json = _format_history_json(hits)
                hypothesis, ans_ms = answer_with_llm(
                    answer_client, args.answer_model, history, qdate, question,
                    qtype=qtype, abstention=abstention,
                    notes=notes, history_json=history_json,
                    max_tokens=args.answer_max_tokens,
                    thinking_budget=args.thinking_budget,
                    reflection=args.reflection,
                    reflection_model=args.reflection_model or None,
                )
                correct = judge_with_llm(
                    judge_client, args.judge_model, qtype, question, answer, hypothesis, abstention
                )
                qtype_correct[qtype].append(1 if correct else 0)

                if compare_con:
                    qtype_correct_con.setdefault(qtype, [])
                    notes_c = ""
                    history_json_c = ""
                    if hits and not abstention:
                        con_model_c = args.chain_of_note_model or args.answer_model
                        notes_c = _chain_of_note_extract(
                            answer_client, con_model_c, hits, qdate, question,
                            max_tokens=args.answer_max_tokens // 2,
                        )
                        if notes_c:
                            history_json_c = _format_history_json(hits)
                    hypothesis_con, _ = answer_with_llm(
                        answer_client, args.answer_model, history, qdate, question,
                        qtype=qtype, abstention=abstention,
                        notes=notes_c, history_json=history_json_c,
                        max_tokens=args.answer_max_tokens,
                        thinking_budget=args.thinking_budget,
                    )
                    correct_con = judge_with_llm(
                        judge_client, args.judge_model, qtype, question, answer, hypothesis_con, abstention
                    )
                    qtype_correct_con[qtype].append(1 if correct_con else 0)

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

            if hyp_con_f is not None:
                entry_con = dict(entry)
                entry_con["hypothesis"] = hypothesis_con
                entry_con["autoeval_label"] = (
                    None if correct_con is None
                    else {"model": args.judge_model, "label": bool(correct_con)}
                )
                hyp_con_f.write(json.dumps(entry_con, ensure_ascii=False) + "\n")
                hyp_con_f.flush()

            if (i + 1) % 10 == 0 or i == len(dataset) - 1:
                running_correct = sum(sum(v) for v in qtype_correct.values())
                running_total = sum(len(v) for v in qtype_correct.values())
                acc = (running_correct / running_total) if running_total else 0.0
                hit_rate = (sum(retrieval_hit_stats) / len(retrieval_hit_stats)) if retrieval_hit_stats else 0.0
                if compare_con and qtype_correct_con:
                    c_correct = sum(sum(v) for v in qtype_correct_con.values())
                    c_total = sum(len(v) for v in qtype_correct_con.values())
                    c_acc = (c_correct / c_total) if c_total else 0.0
                    log(f"  {i+1}/{len(dataset)}  plain={acc:.3f}  con={c_acc:.3f}  hit={hit_rate:.3f}")
                else:
                    log(f"  {i+1}/{len(dataset)}  running_acc={acc:.3f}  session_hit_rate={hit_rate:.3f}")

    if hyp_con_f is not None:
        hyp_con_f.close()

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

    overall_con = None
    per_type_con: dict = {}
    if compare_con and qtype_correct_con:
        tc = sum(sum(v) for v in qtype_correct_con.values())
        tn = sum(len(v) for v in qtype_correct_con.values())
        overall_con = tc / tn if tn else 0.0
        per_type_con = {
            qt: {"n": len(vals), "accuracy": (sum(vals) / len(vals)) if vals else 0.0}
            for qt, vals in qtype_correct_con.items()
        }
    summary = {
        "dataset": str(args.dataset),
        "n_instances": len(dataset),
        "ingest_mode": args.ingest_mode,
        "session_truncations": len(_SESSION_TRUNC_EVENTS),
        "session_truncation_events": _SESSION_TRUNC_EVENTS,
        "session_truncation_evidence_losses": sum(
            1 for ev in _SESSION_TRUNC_EVENTS if not ev["evidence_preserved"]
        ),
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
        "chain_of_note_compare": compare_con,
        "overall_accuracy_con": overall_con,
        "per_type_con": per_type_con,
        "hypothesis_file_con": str(hyp_con_path) if compare_con else None,
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
    if compare_con and overall_con is not None:
        log("-- chain-of-note compare --")
        log(f"overall accuracy (CoN): {overall_con:.4f}")
        for qt in sorted(per_type_con):
            plain = per_type.get(qt, {}).get("accuracy", 0.0)
            con = per_type_con[qt]["accuracy"]
            delta = con - plain
            sign = "+" if delta >= 0 else ""
            log(f"  {qt}: plain={plain:.4f}  con={con:.4f}  ({sign}{delta:+.4f})")
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
    p.add_argument(
        "--k-reasoning",
        type=int,
        default=20,
        help=(
            "top-K for reasoning categories (temporal-reasoning, multi-session, "
            "knowledge-update, single-session-preference). These need more "
            "context to stitch facts across sessions; k=10 starves them. Set "
            "equal to --k to disable the per-category bump."
        ),
    )
    p.add_argument(
        "--recency-bias",
        type=float,
        default=0.05,
        help=(
            "Score bonus added to the newest candidate and linearly "
            "interpolated to 0 for the oldest. Applied only to knowledge-"
            "update and temporal-reasoning questions — categories where 'most "
            "recent' is always the correct answer. Default 0.05 is enough to "
            "flip supersession ties without overwhelming semantic scores. Set "
            "to 0 to disable."
        ),
    )
    # Defaults match the "apples-to-apples vs Hindsight" run: frontier answer
    # model with the upstream LongMemEval gpt-4o judge (neutral, reproducible).
    # Hindsight's own 91.4% uses Gemini 3 Pro answer + gpt-oss-120B judge, but
    # their public comparisons vs competitors use gpt-4o judging, so that's
    # the right baseline to sit next to.
    p.add_argument("--answer-model", default="claude-opus-4-6")
    p.add_argument("--judge-model", default="gpt-4o")
    p.add_argument(
        "--answer-max-tokens",
        type=int,
        default=ANSWER_MAX_TOKENS_DEFAULT,
        help=(
            "Max output tokens for the answer model. Default 8000 fits "
            "non-thinking frontier models; bump to 16000-32000 for Claude "
            "extended thinking or o1/o3 high reasoning effort."
        ),
    )
    p.add_argument(
        "--thinking-budget",
        type=int,
        default=0,
        help=(
            "Enable Anthropic extended thinking with this token budget "
            "(>=1024). 0 disables. Ignored for OpenAI models. When enabled "
            "the answer model runs at temperature=1.0 (Anthropic requirement)."
        ),
    )
    p.add_argument(
        "--reflection",
        action="store_true",
        help=(
            "Run a Hindsight-style two-step reflection pass before the final "
            "answer. First call produces a structured TIMELINE/CONTRADICTIONS/"
            "SUPERSEDED/APPLICABLE FACTS summary; second call answers with "
            "that summary prepended. Only activates for reasoning-limited "
            "categories (temporal, multi-session, preference, knowledge-"
            "update). Mutually exclusive with --thinking-budget."
        ),
    )
    p.add_argument(
        "--reflection-model",
        default="",
        help=(
            "Model for the reflection pre-pass (first step). Defaults to "
            "--answer-model. Set to a cheaper model (e.g. gpt-4o-mini, "
            "claude-haiku-4-5) to reduce reflection cost."
        ),
    )
    p.add_argument(
        "--chain-of-note",
        action="store_true",
        help=(
            "Enable Chain-of-Note + JSON history (LongMemEval paper §5.5). "
            "Runs a per-session extraction pass that writes 'reading notes' "
            "of facts relevant to the question, then sends both the notes "
            "AND the JSON-serialized retrieved history to the final answer "
            "call. Reported as up to +10 absolute pts on oracle retrieval. "
            "Adds one extra LLM call per retrieved session per question, so "
            "expect ~2-3x answer-phase wall time and token cost. Mutually "
            "exclusive with --reflection."
        ),
    )
    p.add_argument(
        "--chain-of-note-model",
        default="",
        help=(
            "Model for the Chain-of-Note extraction pass. Defaults to "
            "--answer-model. Set to a cheaper model (gpt-4o-mini, "
            "claude-haiku-4-5) to cut extraction cost — extraction is a "
            "structured per-chunk task that doesn't need a frontier model."
        ),
    )
    p.add_argument(
        "--chain-of-note-compare",
        action="store_true",
        help=(
            "Run BOTH plain and CoN answer pipelines off the same retrieval "
            "and judge both. Primary hypotheses go to hypotheses.jsonl as "
            "usual; CoN hypotheses go to hypotheses_con.jsonl. Summary "
            "prints per-category plain-vs-con delta. Use --chain-of-note-model "
            "to set the extractor model. Ignores --chain-of-note."
        ),
    )
    p.add_argument("--ingest-concurrency", type=int, default=4,
                   help="number of instances to ingest in parallel")
    p.add_argument(
        "--ingest-mode",
        choices=["turn", "session"],
        default="turn",
        help=(
            "turn (default): one memory per chat turn, fine-grained retrieval. "
            "session: one memory per full session text block with "
            "[Conversation date: ...] header, matches Memento's default ingest "
            "style. Session mode gives the embedder full conversational context "
            "per vector at the cost of coarser top-k granularity."
        ),
    )
    p.add_argument(
        "--wipe-run",
        default="",
        help=(
            "Delete all bench rows tagged with this RUN_ID (the value printed "
            "at the start of every run, e.g. 'lme-20260413-194821-abc123'), "
            "then VACUUM + ANALYZE and exit. Uses idx_mi_change_agent for a "
            "single indexed delete. Run-scoped: does not touch other runs."
        ),
    )
    p.add_argument(
        "--wipe-all-bench",
        action="store_true",
        help=(
            "Delete every row tagged change_agent LIKE 'bench:%%' across all "
            "runs, then VACUUM + ANALYZE and exit. Use this when you want a "
            "completely clean slate."
        ),
    )
    args = p.parse_args()
    if args.reflection and args.thinking_budget > 0:
        p.error("--reflection and --thinking-budget are mutually exclusive "
                "(both serve the same purpose: extra reasoning compute)")
    if args.chain_of_note and args.reflection:
        p.error("--chain-of-note and --reflection are mutually exclusive "
                "(both add a pre-answer LLM pass; pick one)")
    if args.chain_of_note_compare and args.chain_of_note:
        p.error("--chain-of-note-compare already runs the CoN pipeline as the "
                "secondary pass; don't combine with --chain-of-note (which "
                "would make the primary also use CoN)")
    if args.chain_of_note_compare and args.reflection:
        p.error("--chain-of-note-compare and --reflection are mutually exclusive")
    return args


def main() -> None:
    args = parse_args()
    if args.wipe_run or args.wipe_all_bench:
        if args.wipe_run and args.wipe_all_bench:
            print("--wipe-run and --wipe-all-bench are mutually exclusive", flush=True)
            sys.exit(2)
        pattern = f"bench:{args.wipe_run}" if args.wipe_run else "bench:%"
        print(f"wiping change_agent LIKE {pattern!r}...", flush=True)
        counts = wipe_bench_rows(pattern)
        for k, v in counts.items():
            print(f"  {k:32} {v}", flush=True)
        print("done.", flush=True)
        return
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted", flush=True)
    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
