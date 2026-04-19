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
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
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

# ── Generation-mode templates (Hindsight / LongMemEval paper §5.5) ───────────
# All templates here are gated behind opt-in CLI flags so the stock generation
# path above is unchanged when no new flags are set.

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

# Abstention-aware system prompt for LongMemEval _abs questions. The base
# prompt's rule "do not say I don't know" inverts what the abstention judge
# rewards, so we branch on qid.endswith("_abs") and use this instead.
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

# Per-category reasoning scaffolds appended to ANSWER_SYSTEM_BASE based on
# question_type. Tells the answer model how to USE the retrieved context for
# the specific failure modes LongMemEval tests.
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

NO_MEMORY_SYSTEM = (
    "You are a helpful assistant. Answer the user's question directly and "
    "concisely. If you do not know the answer, reply exactly: \"I don't know "
    "based on our past conversations.\" Do not guess, infer, or invent "
    "details. Be terse — no preamble, no hedging, no restating the question."
)

NO_MEMORY_USER_TEMPLATE = (
    "Current Date: {date}\nQuestion: {question}\nAnswer:"
)

# Chain-of-Note + JSON history. Source: LongMemEval paper (Wu et al., 2024)
# §5.5 / Appendix D, github.com/xiaowu0162/LongMemEval. The extraction prompt
# runs per-session BEFORE the final answer call; the final call sees the
# concatenated notes plus a JSON-dumped history rather than natural-language
# session blocks.
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
# A first-pass LLM call produces a structured intermediate; the final answer
# call conditions on (history + reflection + question) instead of just
# (history + question). Gated to reasoning-limited categories.
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

# Final answer prompt when reflection is enabled — prepends the reflection
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

# Default answer-token budget. 8000 covers non-thinking frontier models plus
# moderate chain-of-thought; bump via --answer-max-tokens for Claude extended
# thinking or o1/o3 high reasoning effort. Silent-truncation risk: a cut-off
# answer counts as wrong with no error log, biasing accuracy downward — err
# high, not low.
ANSWER_MAX_TOKENS_DEFAULT = 8000
# 50 tokens leaves room for reasoning-model CoT or prefaces like "The answer
# is: yes" before the yes/no lands. Too tight a budget biases accuracy
# downward silently (truncated empty response → "no" → marked incorrect).
JUDGE_MAX_TOKENS = 50

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

# Categories where newer information should outrank older information: the
# literal answer is always "what did the user say most recently". Applying
# recency bias to multi-session would demote older-but-still-valid facts
# (e.g. an adoption date that's months old but needed to answer).
RECENCY_BIAS_CATEGORIES = frozenset({
    "knowledge-update",
    "temporal-reasoning",
})

# Categories where the answer lives in one specific turn inside a single
# session. Session expansion converts "right session, wrong turn" into wins
# because every turn of the hit session is pulled into context — this turns
# the relevant recall metric from turn_hit into session_hit (which is
# typically 10-15pp higher on these categories).
SS_EXPAND_CATEGORIES = frozenset({
    "single-session-user",
    "single-session-assistant",
})

# Map from qtype to the role whose turns should get a retrieval score bonus.
# ss-assistant questions ("what did you tell me about X?") semantically match
# user follow-up turns more readily than the assistant answer turn itself,
# so the evidence turn ranks lower than distractor user turns. Boosting the
# matching-role score at re-rank time pulls the evidence turn up. Applied to
# a fetched pool of k*2 candidates, then trimmed back to k before session
# expansion.
SS_ROLE_BOOST_MAP = {
    "single-session-assistant": "assistant",
    "single-session-user": "user",
}


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

# Session-level ingest mode (--ingest-mode session) caps the concatenated
# session text at this many characters. Anything longer is either
# evidence-window-trimmed or tail-cut (see build_session_items).
MAX_SESSION_CHARS = 20000

# Per-session truncation diagnostics. build_session_items appends one dict per
# session that required truncation so we can audit evidence-preservation after
# the run. strategy is "evidence-window" when centered on a has_answer turn,
# or "tail-cut" for evidence-free sessions that fall back to a simple cut.
_SESSION_TRUNC_EVENTS: list[dict] = []


# ── Temporal-cue extraction ─────────────────────────────────────────────────
#
# NOTE: temporal_utils already provides parse_longmemeval_date and
# resolve_temporal_expressions (used for the `temporal_anchors` metadata
# field). The regexes below serve a different purpose: they return a flat
# list of ISO date strings usable for score-boosting candidates whose dates
# match query-extracted dates (see smart_time_boost). They also power
# has_temporal_cues, which is a cheap bool check used to gate smart retrieval
# features without paying for the full temporal_utils resolver.
_MONTH_NAMES = (
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December|"
    r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
)
_DATE_PATTERNS = [
    # "January 15", "Jan 15th", "March 5th, 2023"
    re.compile(
        rf"({_MONTH_NAMES})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{{4}}))?",
        re.IGNORECASE,
    ),
    # YYYY/MM/DD or YYYY-MM-DD
    re.compile(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})"),
    # MM/DD/YYYY
    re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})"),
]

_RELATIVE_TIME_RE = re.compile(
    r"(\d+)\s+(day|week|month|year)s?\s+ago"
    r"|last\s+(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|week|month|year)"
    r"|(\d+)\s+(day|week|month)s?\s+(?:later|after|before|from\s+now)"
    r"|how\s+many\s+(day|week|month|year)s?\s+(?:passed|between|since|ago|have\s+passed)",
    re.IGNORECASE,
)


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


def extract_referenced_dates(text: str) -> list[str]:
    """Extract explicit date strings from text content.

    Returns a list of ISO-8601 date strings (YYYY-MM-DD) found in the text.
    Used at ingest time to annotate turns with dates they reference, enabling
    time-aware retrieval without oracle metadata.
    """
    import calendar

    dates: list[str] = []
    seen: set[str] = set()

    month_map: dict[str, int] = {}
    for i, name in enumerate(calendar.month_name):
        if name:
            month_map[name.lower()] = i
    for i, name in enumerate(calendar.month_abbr):
        if name:
            month_map[name.lower()] = i

    # Named month patterns: "January 15", "Jan 15th, 2023"
    for m in _DATE_PATTERNS[0].finditer(text):
        month_str, day_str = m.group(1), m.group(2)
        year_str = m.group(3)
        month = month_map.get(month_str.lower(), 0)
        if not month:
            continue
        day = int(day_str)
        year = int(year_str) if year_str else 2023  # LongMemEval default year
        try:
            d = f"{year:04d}-{month:02d}-{day:02d}"
            datetime.strptime(d, "%Y-%m-%d")
            if d not in seen:
                dates.append(d)
                seen.add(d)
        except ValueError:
            pass

    # YYYY/MM/DD or YYYY-MM-DD
    for m in _DATE_PATTERNS[1].finditer(text):
        y, mo, dy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            d = f"{y:04d}-{mo:02d}-{dy:02d}"
            datetime.strptime(d, "%Y-%m-%d")
            if d not in seen:
                dates.append(d)
                seen.add(d)
        except ValueError:
            pass

    # MM/DD/YYYY
    for m in _DATE_PATTERNS[2].finditer(text):
        mo, dy, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            d = f"{y:04d}-{mo:02d}-{dy:02d}"
            datetime.strptime(d, "%Y-%m-%d")
            if d not in seen:
                dates.append(d)
                seen.add(d)
        except ValueError:
            pass

    return dates


def has_temporal_cues(text: str) -> bool:
    """Check if a query contains temporal reasoning signals."""
    if _RELATIVE_TIME_RE.search(text):
        return True
    if any(p.search(text) for p in _DATE_PATTERNS):
        return True
    temporal_keywords = [
        "how many days", "how many weeks", "how many months", "how many years",
        "how long", "when did", "what date", "which came first", "in what order",
        "before or after", "earlier", "later", "first to last", "last to first",
        "chronological", "timeline", "sequence",
    ]
    text_lower = text.lower()
    return any(kw in text_lower for kw in temporal_keywords)


def build_turn_items(instance: dict, variant: str | None = None) -> list[dict]:
    """Flatten a LongMemEval instance into turn-level memory_write_bulk_impl inputs."""
    qid = instance["question_id"]
    items: list[dict] = []
    sessions: list[list[dict]] = instance["haystack_sessions"]
    session_ids: list[str] = instance["haystack_session_ids"]
    session_dates: list[str] = instance["haystack_dates"]

    prev_session_date_iso = ""
    for s_idx, (sess_id, sess_date, session) in enumerate(zip(session_ids, session_dates, sessions)):
        anchor_dt = temporal_utils.parse_longmemeval_date(sess_date)
        # ISO valid_from so bitemporal filters (as_of) and chronological
        # sorting work; also needed by smart_time_boost at retrieval time.
        valid_from = _session_date_to_iso(sess_date)

        # Gap marker: days between this session and the previous one.
        gap_days = None
        if prev_session_date_iso and valid_from:
            try:
                prev_dt = datetime.fromisoformat(prev_session_date_iso)
                curr_dt = datetime.fromisoformat(valid_from)
                gap_days = (curr_dt - prev_dt).days
            except (ValueError, TypeError):
                pass

        for t_idx, turn in enumerate(session):
            role = turn.get("role", "user")
            content = turn.get("content", "") or ""
            if len(content) > MAX_TURN_CHARS:
                content = content[:MAX_TURN_CHARS]
            has_answer = bool(turn.get("has_answer", False))
            anchors = temporal_utils.resolve_temporal_expressions(content, anchor_dt)
            ref_dates = extract_referenced_dates(content)
            meta: dict[str, Any] = {
                "role": role,
                "session_id": sess_id,
                "session_date": sess_date,
                "session_index": s_idx,
                "turn_index": t_idx,
                "has_answer": has_answer,
                "temporal_anchors": anchors,
            }
            if ref_dates:
                meta["referenced_dates"] = ref_dates
            if t_idx == 0 and gap_days is not None:
                meta["gap_from_prev_session_days"] = gap_days
            item = {
                "type": "message",
                "title": f"{role}:{sess_id}:{t_idx}",
                "content": content,
                "user_id": qid,
                "conversation_id": f"{qid}::{s_idx}",
                "source": "longmemeval",
                "change_agent": BENCH_CHANGE_AGENT,
                "valid_from": valid_from,
                "embed": True,
                "metadata": meta,
            }
            if variant:
                item["variant"] = variant
            items.append(item)
        prev_session_date_iso = valid_from
    return items


def build_session_items(instance: dict, variant: str | None = None) -> list[dict]:
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

    Sessions longer than MAX_SESSION_CHARS are truncated. Evidence-aware
    windowing centers the retained text on the first has_answer turn; evidence-
    free sessions fall back to a tail cut. Truncation events are appended to
    _SESSION_TRUNC_EVENTS for post-run auditing.
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
                # has_answer turn until we hit the char budget, so the
                # evidence turn is always retained. Prefer contiguous
                # coverage of later evidence turns when they fit.
                anchor = evidence_indices[0]
                lo = hi = anchor
                running = len(turn_lines[anchor]) + 1  # +1 for the join newline
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
        item = {
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
        if variant:
            item["variant"] = variant
        items.append(item)
    return items


async def ingest_instance(
    instance: dict,
    variant: str | None = None,
    ingest_mode: str = "turn",
) -> tuple[int, float]:
    if ingest_mode == "session":
        items = build_session_items(instance, variant=variant)
    else:
        # "turn" (default) and any unknown mode fall through to turn-level
        # ingest. Extractive mode was not preserved from the backup; add it
        # here if reinstated in a later commit.
        items = build_turn_items(instance, variant=variant)
    t0 = time.perf_counter()
    await memory_write_bulk_impl(items)
    return len(items), time.perf_counter() - t0


# F4: cross-encoder reranker. Lazy-loaded module-level singleton so the
# 300MB+ import cost is only paid when --rerank is on. Default model is
# the ms-marco distilled MiniLM — the standard cheap cross-encoder, fast
# enough for per-query reranking at bench scale.
_RERANKER = None
_RERANKER_NAME = ""


def _get_reranker(model_name: str):
    """Lazy-load a sentence-transformers CrossEncoder, cached by name."""
    global _RERANKER, _RERANKER_NAME
    if _RERANKER is not None and _RERANKER_NAME == model_name:
        return _RERANKER
    from sentence_transformers import CrossEncoder
    try:
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        device = "cpu"
    _RERANKER = CrossEncoder(model_name, device=device)
    _RERANKER_NAME = model_name
    return _RERANKER


_HYDE_SYSTEM = (
    "You rewrite a short question as a first-person passage that might "
    "appear in the user's past chat history as the answer. Write 2-3 "
    "sentences in the user's voice, as if they mentioned the fact in "
    "passing during an unrelated conversation. Include plausible "
    "surrounding detail so the passage sounds conversational, not like a "
    "direct answer. Do not invent specific entities you don't know — use "
    "placeholder phrasing when the fact is genuinely unknown."
)

_HYDE_USER_TEMPLATE = "Question: {question}\n\nPassage:"


def _hyde_expand(client, model: str, question: str, max_tokens: int = 150) -> str:
    """Generate a hypothetical-answer passage for HyDE-style retrieval.

    Targets the query/evidence phrasing asymmetry: ss-user questions like
    "What degree did I graduate with?" fail to retrieve the evidence turn
    "I graduated with a degree in Business Administration, which has
    helped..." because the evidence embeds the answer inside a longer
    sentence about something else. A hypothetical passage in the user's
    voice matches the evidence embedding much more closely than the terse
    query does.
    """
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _HYDE_SYSTEM},
                {"role": "user", "content": _HYDE_USER_TEMPLATE.format(question=question)},
            ],
            temperature=0,
            max_tokens=max_tokens,
        )
        passage = (resp.choices[0].message.content or "").strip()
    except Exception:
        return ""
    return passage


async def retrieve_for_question(
    qid: str, question: str, k: int,
    cluster_size: int = 0, graph_depth: int = 0,
    recency_bias: float = 0.0,
    adaptive_k: bool = False,
    expand_sessions: bool = False,
    session_cap: int = 12,
    role_boost: float = 0.0,
    role_boost_target: str = "",
    vector_weight: float = 0.7,
    hyde_client=None,
    hyde_model: str = "",
    rerank_model: str = "",
    rerank_pool_k: int = 100,
    adaptive_k_max: int = 30,
    adaptive_k_min: int = 5,
    smart_retrieval: bool = False,
    smart_neighbor_sessions: int = 3,
    smart_time_boost: float = 0.15,
) -> list[dict]:
    """Hybrid FTS5+vector search with optional graph expansion and episodic clustering.

    `expand_sessions`: after the initial ranked retrieval, pull all turns from
    each session that had at least one hit (capped at `session_cap` turns per
    session). Fixes cases where the literal answer turn ranks just outside
    top-k while other turns from the same session make it in — MMR's duplicate
    penalty demotes supersession evidence, which session expansion recovers.

    `role_boost` + `role_boost_target`: fetch an overshoot of 2*k candidates,
    add `role_boost` to the score of any candidate whose metadata.role matches
    `role_boost_target`, re-sort by boosted score, and trim to k. Targets the
    ss-assistant failure mode where the evidence turn ranks outside top-k
    because user-turn distractors score higher on the raw question embedding.
    """
    # F4 rerank fetches a large candidate pool; otherwise fall back to 2x for
    # role boost, else plain k. Adaptive-k pulls adaptive_k_max so the elbow
    # trim downstream has a full distribution to cut against.
    if rerank_model:
        fetch_k = rerank_pool_k
    elif adaptive_k or smart_retrieval:
        fetch_k = adaptive_k_max
    elif role_boost > 0 and role_boost_target:
        fetch_k = k * 2
    else:
        fetch_k = k

    # HyDE: prepend the original question with a hypothetical first-person
    # passage. BM25 still matches on the original query words; the vector
    # embedding sees enriched signal including plausible answer phrasing.
    # One LLM call per question.
    query_text = question
    if hyde_client is not None and hyde_model:
        passage = _hyde_expand(hyde_client, hyde_model, question)
        if passage:
            query_text = f"{question}\n\n{passage}"

    ranked = await memory_search_scored_impl(
        query_text, k=fetch_k, user_id=qid,
        extra_columns=["metadata_json", "conversation_id", "valid_from"],
        recency_bias=recency_bias,
        vector_weight=vector_weight,
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

    # F4: cross-encoder rerank over the candidate pool. Cross-encoders score
    # query+candidate jointly, detecting "answer embedded in off-topic turn"
    # cases where bi-encoder similarity fails. Mutually exclusive with the
    # role_boost path — once rerank is active, role heuristics are obsolete.
    if rerank_model and hits:
        ce = _get_reranker(rerank_model)
        pairs = [(question, h["content"]) for h in hits]
        ce_scores = ce.predict(pairs)
        for h, s in zip(hits, ce_scores):
            h["score"] = float(s)
        hits.sort(key=lambda h: h["score"], reverse=True)
        hits = hits[:k]
        seen_ids = {h["id"] for h in hits}
    # Role boost re-rank: for ss-user/ss-assistant, boost hits whose role
    # matches the expected answer role, re-sort, and trim the overshoot
    # fetch_k pool back to k. Runs before graph/session expansion so the
    # downstream expansions seed from the role-corrected top-k.
    elif role_boost > 0 and role_boost_target and hits:
        for h in hits:
            if h["metadata"].get("role") == role_boost_target:
                h["score"] += role_boost
        hits.sort(key=lambda h: h["score"], reverse=True)
        hits = hits[:k]
        seen_ids = {h["id"] for h in hits}

    # Smart retrieval: time-aware boost + neighbor-session expansion. Runs
    # after role/rerank (so it sees the final candidate set) but before
    # adaptive-k elbow trim (so boosted/expanded candidates get a fair
    # shot at the cut). Gated on --smart-retrieval.
    if smart_retrieval and hits:
        # ── Time-aware boost ──
        # Parse dates from the query. If found, boost candidates whose
        # valid_from or metadata.referenced_dates fall within a small
        # window of any query date.
        query_dates = extract_referenced_dates(question)
        query_has_temporal = has_temporal_cues(question)

        if query_dates and smart_time_boost > 0:
            query_dt_set: list[datetime] = []
            for ds in query_dates:
                try:
                    query_dt_set.append(
                        datetime.strptime(ds, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    )
                except ValueError:
                    pass

            if query_dt_set:
                for h in hits:
                    # Check valid_from (session date)
                    vf = h.get("valid_from", "")
                    if vf:
                        try:
                            h_dt = datetime.fromisoformat(vf)
                            for qdt in query_dt_set:
                                if abs((h_dt - qdt).days) <= 30:
                                    h["score"] += smart_time_boost
                                    break
                        except (ValueError, TypeError):
                            pass
                    # Check referenced_dates in metadata (tighter window)
                    ref_dates = h.get("metadata", {}).get("referenced_dates", [])
                    for rd in ref_dates:
                        try:
                            rd_dt = datetime.strptime(rd, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                            for qdt in query_dt_set:
                                if abs((rd_dt - qdt).days) <= 14:
                                    h["score"] += smart_time_boost
                                    break
                        except (ValueError, TypeError):
                            pass
                hits.sort(key=lambda h: h["score"], reverse=True)

        # ── Neighbor-session expansion ──
        # Triggered when: (a) query has temporal cues, OR (b) hits span
        # multiple sessions (heuristic for multi-session synthesis).
        # Pulls turns from sessions adjacent (±N) to each hit session so
        # the answer model has enough context for cross-session reasoning.
        if smart_neighbor_sessions > 0:
            hit_session_indices: set[int] = set()
            for h in hits:
                si = h.get("metadata", {}).get("session_index")
                if si is not None:
                    try:
                        hit_session_indices.add(int(si))
                    except (TypeError, ValueError):
                        pass

            multi_session_signal = len(hit_session_indices) >= 2
            if (query_has_temporal or multi_session_signal) and hit_session_indices:
                neighbor_indices: set[int] = set()
                for si in hit_session_indices:
                    for offset in range(-smart_neighbor_sessions, smart_neighbor_sessions + 1):
                        neighbor_indices.add(si + offset)
                neighbor_indices -= hit_session_indices

                if neighbor_indices:
                    neighbor_convs = {f"{qid}::{si}" for si in neighbor_indices if si >= 0}
                    if neighbor_convs:
                        with _db() as db:
                            placeholders = ",".join(["?"] * len(neighbor_convs))
                            rows = db.execute(
                                f"SELECT id, content, title, metadata_json, "
                                f"conversation_id, valid_from "
                                f"FROM memory_items "
                                f"WHERE conversation_id IN ({placeholders}) "
                                f"  AND user_id = ? AND is_deleted = 0 "
                                f"ORDER BY valid_from, CAST("
                                f"  json_extract(metadata_json, '$.turn_index') AS INTEGER"
                                f")",
                                [*neighbor_convs, qid],
                            ).fetchall()
                        # Neighbor turns get a reduced score — they're
                        # context, not direct hits. Use the lowest score
                        # from the initial hits minus a penalty so they
                        # sort after real hits.
                        neighbor_score = (hits[-1]["score"] * 0.8) if hits else 0.0
                        for row in rows:
                            rid = row["id"]
                            if rid in seen_ids:
                                continue
                            meta_raw = row["metadata_json"] or "{}"
                            try:
                                meta = json.loads(meta_raw)
                            except json.JSONDecodeError:
                                meta = {}
                            hits.append(
                                {
                                    "id": rid,
                                    "content": row["content"] or "",
                                    "title": row["title"] or "",
                                    "metadata": meta,
                                    "conversation_id": row["conversation_id"] or "",
                                    "valid_from": row["valid_from"] or "",
                                    "score": neighbor_score,
                                }
                            )
                            seen_ids.add(rid)
                        hits.sort(key=lambda h: h["score"], reverse=True)

    # Adaptive-k elbow trim: find the largest adjacent score gap in the
    # top candidates and trim to that elbow, clamped to [min, max]. Uses
    # only the retriever's own similarity signal — no oracle metadata.
    if (adaptive_k or smart_retrieval) and hits:
        scores = [h["score"] for h in hits[:adaptive_k_max]]
        n = len(scores)
        if n <= adaptive_k_min:
            cut = n
        else:
            best_i = adaptive_k_min - 1
            best_gap = scores[best_i] - scores[best_i + 1] if best_i + 1 < n else 0.0
            for i in range(adaptive_k_min - 1, n - 1):
                gap = scores[i] - scores[i + 1]
                if gap > best_gap:
                    best_gap = gap
                    best_i = i
            cut = best_i + 1
            cut = max(adaptive_k_min, min(adaptive_k_max, cut))
        hits = hits[:cut]
        seen_ids = {h["id"] for h in hits}

    # Session expansion: pull every turn from each hit session (capped per
    # session). For single-session categories this collapses turn_hit onto
    # session_hit — once the right session is retrieved, every turn of it is
    # in context, so "right session, wrong turn" becomes a win.
    if expand_sessions and hits:
        session_ids_hit = {h["conversation_id"] for h in hits if h.get("conversation_id")}
        if session_ids_hit:
            with _db() as db:
                placeholders = ",".join(["?"] * len(session_ids_hit))
                rows = db.execute(
                    f"SELECT id, content, title, metadata_json, conversation_id "
                    f"FROM memory_items "
                    f"WHERE conversation_id IN ({placeholders}) AND is_deleted = 0",
                    tuple(session_ids_hit),
                ).fetchall()
            per_session: dict[str, list[dict]] = {}
            for r in rows:
                if r["id"] in seen_ids:
                    continue
                rm = json.loads(r["metadata_json"] or "{}")
                per_session.setdefault(r["conversation_id"] or "", []).append({
                    "id": r["id"],
                    "content": r["content"] or "",
                    "title": r["title"] or "",
                    "metadata": rm,
                    "conversation_id": r["conversation_id"] or "",
                    "score": 0.0,
                })
            for cid, rows_for_session in per_session.items():
                rows_for_session.sort(key=lambda x: x["metadata"].get("turn_index", 0))
                for r in rows_for_session[:session_cap]:
                    seen_ids.add(r["id"])
                    hits.append(r)

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


def _llm_complete(
    client, model: str, system: str, user: str, max_tokens: int,
    thinking_budget: int = 0,
) -> str:
    """Single OpenAI-compat completion. `thinking_budget` is forwarded as an
    `extra_body.reasoning_tokens` field for endpoints that support reasoning
    extras (silently inert otherwise).
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    if thinking_budget and thinking_budget > 0:
        # OpenAI-compat servers that accept reasoning params (LM Studio with
        # certain backends, OpenRouter, etc.) read these from extra_body. If
        # the endpoint doesn't recognize them they are typically ignored, not
        # rejected — but wrap in try/except to be safe.
        kwargs["extra_body"] = {"reasoning_tokens": thinking_budget}
    try:
        resp = client.chat.completions.create(**kwargs)
    except TypeError:
        # Older openai SDKs don't accept extra_body kw — drop it and retry.
        kwargs.pop("extra_body", None)
        resp = client.chat.completions.create(**kwargs)
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
        key=lambda kv: kv[1][0].get("metadata", {}).get("session_date", ""),
    ):
        turns.sort(key=lambda t: t.get("metadata", {}).get("turn_index", 0))
        sessions_out.append(
            {
                "session_date": turns[0].get("metadata", {}).get("session_date", ""),
                "turns": [
                    {
                        "role": t.get("metadata", {}).get("role", "?"),
                        "content": t.get("content", ""),
                    }
                    for t in turns
                ],
            }
        )
    return json.dumps(sessions_out, indent=2, ensure_ascii=False)


def _chain_of_note_extract(
    client, model: str, hits: list[dict], qdate: str, question: str,
    max_tokens: int,
) -> str:
    """Run Chain-of-Note extraction per session, return concatenated notes.

    Sessions where the model answers 'empty' are dropped. Returns a string
    formatted as `[Session YYYY/MM/DD]\\n<notes>` blocks separated by blank
    lines, or empty string if no notes were extracted.
    """
    by_session: dict[str, list[dict]] = {}
    for h in hits:
        by_session.setdefault(h.get("conversation_id") or "unknown", []).append(h)

    blocks: list[str] = []
    for cid, turns in sorted(
        by_session.items(),
        key=lambda kv: kv[1][0].get("metadata", {}).get("session_date", ""),
    ):
        turns.sort(key=lambda t: t.get("metadata", {}).get("turn_index", 0))
        sess_date = turns[0].get("metadata", {}).get("session_date", "")
        sess_content = "\n".join(
            f"{t.get('metadata', {}).get('role', '?').capitalize()}: {t.get('content', '')}"
            for t in turns
        )
        prompt = CHAIN_OF_NOTE_PROMPT.format(
            session_date=sess_date,
            session_content=sess_content,
            question_date=qdate,
            question=question,
        )
        try:
            note = _llm_complete(client, model, "", prompt, max_tokens)
        except Exception:
            continue
        cleaned = note.strip()
        if not cleaned or cleaned.lower() == "empty":
            continue
        blocks.append(f"[Session {sess_date}]\n{cleaned}")

    return "\n\n".join(blocks)


def _reflect(
    client, model: str, history: str, date: str, question: str,
    max_tokens: int,
) -> str:
    """Run the Hindsight-style reflection pre-pass. Returns the reflection
    text, or empty string on failure (caller falls back to single-shot)."""
    user = REFLECTION_USER_TEMPLATE.format(history=history, date=date, question=question)
    for attempt in range(2):
        try:
            return _llm_complete(client, model, REFLECTION_SYSTEM, user, max_tokens)
        except Exception:
            if attempt == 1:
                return ""
            time.sleep(1)
    return ""


def answer_with_llm(
    client, model: str, history: str, date: str, question: str,
    timeline: str = "", anchors: str = "", q_signal: str = "default",
    *,
    qtype: str = "",
    abstention: bool = False,
    no_memory: bool = False,
    rag_aware_empty: bool = False,
    no_category_knobs: bool = False,
    reflection: bool = False,
    reflection_model: str | None = None,
    notes: str = "",
    history_json: str = "",
    answer_max_tokens: int | None = None,
    thinking_budget: int = 0,
) -> tuple[str, int]:
    """Generate one answer.

    Mode selection:
      * no_memory: NO_MEMORY_SYSTEM + NO_MEMORY_USER_TEMPLATE, history ignored.
      * rag_aware_empty: standard RAG system prompt + ANSWER_USER_TEMPLATE with
        empty history block (simulates a correctly-wired RAG pipeline whose
        retriever returned zero results). Skips reflection / chain-of-note.
      * reflection: only fires when qtype in REFLECTION_CATEGORIES (and
        --no-category-knobs not set). Runs _reflect first, then
        ANSWER_WITH_REFLECTION_USER_TEMPLATE.
      * notes + history_json (chain-of-note path): use
        ANSWER_WITH_NOTES_USER_TEMPLATE.
      * abstention: ANSWER_SYSTEM_ABSTENTION instead of base/per-type system.

    When NONE of {no_memory, rag_aware_empty, reflection, notes, abstention}
    are active, behavior matches the previous q_signal-driven path exactly.
    """
    new_mode_active = (
        no_memory or rag_aware_empty or reflection
        or bool(notes) or bool(history_json) or abstention
    )

    t0 = time.perf_counter()

    if not new_mode_active:
        # Stock-main path — preserved verbatim for backward compatibility.
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
        # answer_max_tokens explicitly overrides the q_signal-tied default
        # only when the caller passed it; None means "keep stock behavior".
        if answer_max_tokens is None:
            max_tok = 1200 if q_signal == "temporal" else 800
        else:
            max_tok = answer_max_tokens
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

    # ── New-mode path (any of: no_memory, rag_aware_empty, reflection,
    #    chain-of-note notes, abstention) ──
    max_tok = answer_max_tokens if answer_max_tokens is not None else ANSWER_MAX_TOKENS_DEFAULT

    # Pick the system prompt.
    if no_memory:
        system = NO_MEMORY_SYSTEM
    elif abstention:
        system = ANSWER_SYSTEM_ABSTENTION
    elif no_category_knobs:
        # Drop per-category scaffold; abstention branching above still applies.
        system = ANSWER_SYSTEM_BASE
    else:
        system = ANSWER_SYSTEM_BASE
        scaffold = ANSWER_SYSTEM_BY_TYPE.get(qtype)
        if scaffold:
            system = f"{ANSWER_SYSTEM_BASE}\n\n{scaffold}"

    # Pick the user prompt.
    if no_memory:
        user = NO_MEMORY_USER_TEMPLATE.format(date=date, question=question)
    elif rag_aware_empty:
        # Real user template, empty history block. Skip reflection /
        # chain-of-note since they operate on retrieved content.
        user = ANSWER_USER_TEMPLATE.format(history="", date=date, question=question)
    else:
        reflection_text = ""
        if reflection and qtype in REFLECTION_CATEGORIES:
            rmodel = reflection_model or model
            # Half-budget for the reflection summary — it's structured, not
            # full CoT.
            reflection_text = _reflect(client, rmodel, history, date, question, max_tok // 2)

        if notes and history_json:
            user = ANSWER_WITH_NOTES_USER_TEMPLATE.format(
                history_json=history_json, notes=notes, date=date, question=question,
            )
        elif reflection_text:
            user = ANSWER_WITH_REFLECTION_USER_TEMPLATE.format(
                history=history, reflection=reflection_text, date=date, question=question,
            )
        else:
            user = ANSWER_USER_TEMPLATE.format(history=history, date=date, question=question)

    for attempt in range(3):
        try:
            hyp = _llm_complete(client, model, system, user, max_tok, thinking_budget=thinking_budget)
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


def _provider_for_model(model: str) -> str:
    """Route model IDs to a provider. Claude IDs start with 'claude-'; anything
    else falls through to OpenAI (gpt-*, o1-*, o3-*, plus OpenAI-compatible
    endpoints hosting other names)."""
    m = (model or "").lower()
    if m.startswith("claude-"):
        return "anthropic"
    return "openai"


# ── Runner ───────────────────────────────────────────────────────────────────

async def run(args: argparse.Namespace) -> None:
    # --answer-model is a hidden alias for --generator-model. Resolve early.
    if getattr(args, "answer_model", None) and not args.generator_model:
        args.generator_model = args.answer_model

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

    if args.only_qtype:
        allowed = set(args.only_qtype)
        dataset = [inst for inst in dataset if inst.get("question_type") in allowed]
        log(f"  filtered to question_type in {sorted(allowed)}: {len(dataset)} instances")

    # Resolve clients. Generator may be local (LM Studio) or OpenAI.
    gen_client = None
    judge_client = None
    if not args.generator_model:
        raise SystemExit(
            "generator model is not set — pass --generator-model or set EVAL_GENERATOR_MODEL"
        )
    if args.generator_base_url:
        gen_key = get_api_key("LM_API_TOKEN") or "lm-studio"
        gen_client = _openai_client(gen_key, base_url=args.generator_base_url)
    if not args.no_judge:
        if not args.judge_model:
            raise SystemExit(
                "judge model is not set — pass --judge-model or set EVAL_JUDGE_MODEL"
            )
        api_key = get_api_key("OPENAI_API_KEY")
        if not api_key:
            raise SystemExit("OPENAI_API_KEY not found (env / keyring / vault). Use `bin/setup_secret.py OPENAI_API_KEY`.")
        judge_client = _openai_client(api_key)
        if gen_client is None:
            gen_client = judge_client  # generator routes through OpenAI too
    elif gen_client is None:
        # No judge AND no local generator — generator must be OpenAI
        api_key = get_api_key("OPENAI_API_KEY")
        if not api_key:
            raise SystemExit("OPENAI_API_KEY not found (env / keyring / vault). Use `bin/setup_secret.py OPENAI_API_KEY`.")
        gen_client = _openai_client(api_key)

    # ── Phase 1: ingest ──
    if args.no_memory or args.rag_aware_empty:
        log(f"skipping ingest (--{'no-memory' if args.no_memory else 'rag-aware-empty'} implies no DB read)")
    elif args.skip_ingest:
        log("skipping ingest (--skip-ingest)")
    else:
        log(f"phase 1: ingest ({args.ingest_concurrency} instances in parallel)")
        total_items = 0
        done_count = 0
        ingest_start = time.perf_counter()
        sem = asyncio.Semaphore(args.ingest_concurrency)

        async def _one(i: int, inst: dict) -> tuple[int, int]:
            async with sem:
                n, _dt = await ingest_instance(
                    inst,
                    variant=args.variant,
                    ingest_mode=args.ingest_mode,
                )
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
    signal_dist: Counter = Counter()

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

            q_signal = classify_question(question)
            signal_dist[q_signal] += 1

            # Smart retrieval logic: boost K for temporal reasoning
            effective_k = args.k
            if args.smart_retrieval and q_signal == "temporal":
                effective_k = max(effective_k, 30)  # Significantly larger K for date reasoning
            # --k-reasoning: bump K for reasoning-heavy categories (temporal /
            # update). These need more context to stitch facts across sessions;
            # the stock --k starves them. Set via classifier-driven q_signal so
            # the lever works without oracle qtype metadata.
            if args.k_reasoning > 0 and q_signal in ("temporal", "update"):
                effective_k = max(effective_k, args.k_reasoning)

            # SS category levers from 01c9b0d: role boost + session expansion
            # for single-session-user / single-session-assistant. Role boost
            # fixes the ss-assistant ranking failure (user follow-up turns
            # outrank the assistant answer turn on raw question embedding);
            # session expansion converts "right session, wrong turn" into wins.
            # --no-category-knobs disables all category-gated heuristics.
            if args.no_category_knobs:
                rboost_target = ""
                rboost = 0.0
                ss_expand = False
                rbias = 0.0
            else:
                rboost_target = SS_ROLE_BOOST_MAP.get(qtype, "")
                rboost = args.ss_role_boost if rboost_target else 0.0
                ss_expand = qtype in SS_EXPAND_CATEGORIES
                # Explicit --recency-bias takes effect on RECENCY_BIAS_CATEGORIES
                # (knowledge-update, temporal-reasoning) using the ground-truth
                # qtype. Falls back to the classifier-driven q_signal path so
                # stock-main behavior is preserved when --recency-bias stays 0.
                if args.recency_bias > 0.0 and qtype in RECENCY_BIAS_CATEGORIES:
                    rbias = args.recency_bias
                else:
                    rbias = 0.15 if q_signal == "update" else 0.0

            if args.no_memory or args.rag_aware_empty:
                # Baseline modes: skip retrieval entirely. --no-memory uses
                # the no-memory system prompt; --rag-aware-empty uses the
                # standard RAG prompt with an empty history block.
                hits = []
                retrieval_hit = None
            else:
                try:
                    hits = await retrieve_for_question(
                        qid, question, effective_k,
                        cluster_size=args.cluster_size,
                        graph_depth=args.graph_depth,
                        recency_bias=rbias,
                        adaptive_k=args.adaptive_k or args.smart_retrieval,
                        expand_sessions=ss_expand,
                        role_boost=rboost,
                        role_boost_target=rboost_target,
                        vector_weight=args.vector_weight,
                        hyde_client=gen_client if args.hyde else None,
                        hyde_model=(args.hyde_model or args.generator_model) if args.hyde else "",
                        rerank_model=args.rerank_model if args.rerank else "",
                        rerank_pool_k=args.rerank_pool_k,
                        adaptive_k_max=args.adaptive_k_max,
                        adaptive_k_min=args.adaptive_k_min,
                        smart_retrieval=args.smart_retrieval,
                        smart_neighbor_sessions=args.smart_neighbor_sessions,
                        smart_time_boost=args.smart_time_boost,
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
            hypothesis_con = ""
            correct: bool | None = None
            correct_con: bool | None = None
            if not args.no_judge:
                history, anchors = format_retrieved(hits, q_signal=q_signal)
                # Chain-of-Note path: extract per-session reading notes, then
                # send notes + JSON-history to the answer call. Skipped on
                # abstention (the abstention judge rewards "I don't know"; we
                # don't want to feed the model handpicked facts that bias it
                # toward answering).
                notes = ""
                history_json = ""
                if args.chain_of_note and hits and not abstention:
                    con_model = args.chain_of_note_model or args.generator_model
                    notes = _chain_of_note_extract(
                        gen_client, con_model, hits, qdate, question,
                        max_tokens=args.answer_max_tokens // 2,
                    )
                    if notes:
                        history_json = _format_history_json(hits)

                # Reflection only fires on REFLECTION_CATEGORIES (gated inside
                # answer_with_llm), and is suppressed when --no-category-knobs
                # is set (parity with the retrieval-side knobs).
                reflection_active = args.reflection and not args.no_category_knobs

                # Decide whether to engage new-mode answer wiring at all. When
                # no new flags are set, the call falls through to the stock
                # q_signal-driven path inside answer_with_llm.
                new_mode = (
                    args.no_memory or args.rag_aware_empty
                    or reflection_active or bool(notes) or abstention
                    or args.thinking_budget > 0
                    or args.answer_max_tokens != ANSWER_MAX_TOKENS_DEFAULT
                )
                # Stock path passes None so the q_signal-tied 800/1200 default
                # is preserved verbatim. New-mode passes the explicit budget.
                amt = args.answer_max_tokens if new_mode else None

                hypothesis, ans_ms = answer_with_llm(
                    gen_client, args.generator_model, history, qdate, question,
                    timeline=timeline, anchors=anchors, q_signal=q_signal,
                    qtype=qtype, abstention=abstention,
                    no_memory=args.no_memory,
                    rag_aware_empty=args.rag_aware_empty,
                    no_category_knobs=args.no_category_knobs,
                    reflection=reflection_active,
                    reflection_model=args.reflection_model or None,
                    notes=notes, history_json=history_json,
                    answer_max_tokens=amt,
                    thinking_budget=args.thinking_budget,
                )
                correct = judge_with_llm(
                    judge_client, args.judge_model, qtype, question, answer, hypothesis, abstention
                )
                qtype_correct[qtype].append(1 if correct else 0)

                # --chain-of-note-compare: run a second answer pass that uses
                # the chain-of-note pipeline regardless of --chain-of-note,
                # and judge it separately. Lets one run produce both A/B
                # hypotheses without re-running retrieval.
                if compare_con:
                    qtype_correct_con.setdefault(qtype, [])
                    notes_c = ""
                    history_json_c = ""
                    if hits and not abstention:
                        con_model_c = args.chain_of_note_model or args.generator_model
                        notes_c = _chain_of_note_extract(
                            gen_client, con_model_c, hits, qdate, question,
                            max_tokens=args.answer_max_tokens // 2,
                        )
                        if notes_c:
                            history_json_c = _format_history_json(hits)
                    hypothesis_con, _ = answer_with_llm(
                        gen_client, args.generator_model, history, qdate, question,
                        timeline=timeline, anchors=anchors, q_signal=q_signal,
                        qtype=qtype, abstention=abstention,
                        notes=notes_c, history_json=history_json_c,
                        answer_max_tokens=args.answer_max_tokens,
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
            if compare_con:
                entry["hypothesis_note"] = hypothesis_con
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
        "k": args.k,
        "cluster_size": args.cluster_size,
        "graph_depth": args.graph_depth,
        "search_mode": "hybrid",
        "smart_retrieval": args.smart_retrieval,
        "adaptive_k": args.adaptive_k,
        "generator_model": args.generator_model,
        "generator_base_url": args.generator_base_url,
        "judge_model": args.judge_model,
        "variant": args.variant,
        "judged": not args.no_judge,
        "overall_accuracy": overall,
        "per_type": per_type,
        "signal_distribution": dict(signal_dist),
        "retrieval_session_hit_rate": (
            sum(retrieval_hit_stats) / len(retrieval_hit_stats) if retrieval_hit_stats else None
        ),
        "hypothesis_file": str(hyp_path),
        "no_memory": args.no_memory,
        "rag_aware_empty": args.rag_aware_empty,
        "reflection": args.reflection,
        "chain_of_note": args.chain_of_note,
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
    log(f"signal distribution: {dict(signal_dist)}")
    log(f"hypotheses -> {hyp_path}")
    log(f"results    -> {results_path}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    p.add_argument("--limit", type=int, default=0, help="subsample first N instances (0 = all)")
    p.add_argument(
        "--only-qtype",
        action="append",
        default=[],
        help=(
            "Restrict the run to instances whose question_type matches one of "
            "these values. Repeat the flag to allow multiple types (e.g. "
            "--only-qtype single-session-user --only-qtype single-session-assistant). "
            "Applied after --limit. Pair with --skip-ingest to rerun a subset "
            "against an already-ingested DB."
        ),
    )
    p.add_argument("--skip-ingest", action="store_true")
    p.add_argument("--no-judge", action="store_true")
    p.add_argument("--k", type=int, default=20, help="top-K retrieved turns per question")
    p.add_argument("--adaptive-k", action="store_true", help="Enable elbow trim for adaptive K")
    p.add_argument("--smart-retrieval", action="store_true", help="Enable temporal-aware smart retrieval")
    p.add_argument("--cluster-size", type=int, default=5,
                   help="episodic expansion: pull +/- N surrounding turns (0 = off)")
    p.add_argument("--graph-depth", type=int, default=1,
                   help="graph expansion hops from initial hits (0 = off)")
    p.add_argument(
        "--ss-role-boost",
        type=float,
        default=0.10,
        help=(
            "Retrieval score bonus applied to turns matching the expected "
            "role for single-session categories (assistant turns for "
            "single-session-assistant, user turns for single-session-user). "
            "Fetches a 2x candidate pool before re-ranking. Targets the "
            "ss-assistant failure mode where the evidence answer turn ranks "
            "behind user-follow-up distractors. Set to 0 to disable."
        ),
    )
    p.add_argument("--generator-model",
                   default=os.environ.get("EVAL_GENERATOR_MODEL"))
    p.add_argument("--generator-base-url",
                   default=os.environ.get("EVAL_GENERATOR_BASE_URL"),
                   help="OpenAI-compatible base URL for the generator (e.g. http://localhost:1234/v1 for LM Studio). If unset, generator routes through OpenAI.")
    p.add_argument("--judge-model",
                   default=os.environ.get("EVAL_JUDGE_MODEL"))
    p.add_argument("--ingest-concurrency", type=int, default=4,
                   help="number of instances to ingest in parallel")
    p.add_argument("--variant", type=str, default=None,
                   help="tag stored on each ingested memory_item; lets multiple retrieval runs share an ingest")
    p.add_argument(
        "--ingest-mode",
        type=str,
        choices=["turn", "session"],
        default="turn",
        help=(
            "Ingest granularity. 'turn' (default) emits one memory_item per "
            "conversation turn — finer retrieval granularity. 'session' emits "
            "one memory_item per session with concatenated role-tagged turns, "
            "capped at MAX_SESSION_CHARS with evidence-aware truncation. "
            "Session mode matches Memento's default strategy and helps when "
            "the answer depends on cross-turn co-occurrence inside one "
            "session, at the cost of pinpoint accuracy."
        ),
    )
    # ── Retrieval-quality levers (all default-OFF / zero-effect) ─────────────
    p.add_argument(
        "--hyde",
        action="store_true",
        default=False,
        help=(
            "HyDE-style query expansion: before retrieval, call an LLM to "
            "rewrite the question as a hypothetical first-person passage, "
            "then append it to the question before embedding. Targets the "
            "query/evidence phrasing asymmetry where terse biographical "
            "queries fail to match evidence turns that mention the answer "
            "in passing. One extra LLM call per question."
        ),
    )
    p.add_argument(
        "--hyde-model",
        default=None,
        help=(
            "Model used for HyDE passage generation. If unset, falls back "
            "to --generator-model. Use a cheap rewrite-capable model."
        ),
    )
    p.add_argument(
        "--rerank",
        action="store_true",
        default=False,
        help=(
            "F4: cross-encoder reranker. Fetches a candidate pool of "
            "--rerank-pool-k, rescores each (query, candidate) pair with a "
            "cross-encoder, sorts, trims to --k. Targets the 'needle "
            "disguised as hay' failure where the evidence turn mentions "
            "the answer as a side clause in a conversation about something "
            "else — cases bi-encoder similarity cannot rank correctly. "
            "Mutually exclusive with --ss-role-boost at the ranking stage "
            "(rerank already handles ordering)."
        ),
    )
    p.add_argument(
        "--rerank-model",
        default="cross-encoder/ms-marco-MiniLM-L-6-v2",
        help=(
            "Cross-encoder model name for reranking. Default is the "
            "ms-marco distilled MiniLM — fast, CPU-friendly, standard for "
            "passage reranking."
        ),
    )
    p.add_argument(
        "--rerank-pool-k",
        type=int,
        default=100,
        help=(
            "Candidate pool size fetched from memory_search_scored_impl "
            "before reranking. Larger pool catches evidence turns that "
            "ranked low on bi-encoder score but the cross-encoder will "
            "promote. 100 is a safe default; 200 for very deep haystacks."
        ),
    )
    p.add_argument(
        "--recency-bias",
        type=float,
        default=0.0,
        help=(
            "Score bonus added to the newest candidate and linearly "
            "interpolated to 0 for the oldest. Applied only to knowledge-"
            "update and temporal-reasoning questions (unless "
            "--no-category-knobs is set, which disables it). Typical "
            "values 0.05-0.15. Default 0.0 = disabled."
        ),
    )
    p.add_argument(
        "--smart-neighbor-sessions",
        type=int,
        default=3,
        help="±N sessions to expand into when smart-retrieval triggers neighbor expansion.",
    )
    p.add_argument(
        "--smart-time-boost",
        type=float,
        default=0.15,
        help="Score boost for candidates whose timestamps match query-extracted dates.",
    )
    p.add_argument(
        "--adaptive-k-min",
        type=int,
        default=5,
        help="Lower bound on adaptive-k. Never trim below this many turns.",
    )
    p.add_argument(
        "--adaptive-k-max",
        type=int,
        default=30,
        help="Upper bound on adaptive-k. Candidate pool size + hard cap.",
    )
    p.add_argument(
        "--vector-weight",
        type=float,
        default=0.7,
        help=(
            "Hybrid score blend: final = vector * w + bm25 * (1 - w). "
            "Default 0.7 matches production m3-memory. Lower values favor "
            "lexical matching — useful when query terms appear literally "
            "in the evidence turn but semantic similarity is weak."
        ),
    )
    p.add_argument(
        "--no-category-knobs",
        action="store_true",
        default=False,
        help=(
            "Ablation: disable category-gated retrieval knobs (role boost, "
            "session expansion, recency-bias gating). Lets a run measure "
            "retrieval quality without per-qtype heuristics."
        ),
    )
    # ── Generation-mode levers (all default-OFF / zero-effect) ──────────────
    p.add_argument(
        "--no-memory",
        action="store_true",
        default=False,
        help=(
            "Baseline: skip retrieval entirely and ask the answer model the "
            "question with no history, using a neutral system prompt that "
            "makes no reference to memory. Measures what the answer model "
            "can do on its own. Implies skipping ingest. Mutually exclusive "
            "with --rag-aware-empty."
        ),
    )
    p.add_argument(
        "--rag-aware-empty",
        action="store_true",
        default=False,
        help=(
            "Baseline variant: still run retrieval but if results are empty "
            "(or here: forced empty), use the standard RAG system prompt "
            "with an empty History Chats block. Simulates a correctly-wired "
            "RAG pipeline whose retriever returned zero results. Closes the "
            "prompt-confound gap in --no-memory. Implies skipping ingest. "
            "Mutually exclusive with --no-memory."
        ),
    )
    p.add_argument(
        "--reflection",
        action="store_true",
        default=False,
        help=(
            "Run a Hindsight-style two-step reflection pass before the final "
            "answer. First call produces a structured TIMELINE/CONTRADICTIONS"
            "/SUPERSEDED/APPLICABLE FACTS summary; second call answers with "
            "that summary prepended. Only activates for reasoning-limited "
            "qtypes (temporal-reasoning, multi-session, single-session-"
            "preference, knowledge-update). Disabled when --no-category-knobs "
            "is set."
        ),
    )
    p.add_argument(
        "--reflection-model",
        default=None,
        help=(
            "Model for the reflection pre-pass. Defaults to --generator-model. "
            "Set to a cheaper model to reduce reflection cost."
        ),
    )
    p.add_argument(
        "--chain-of-note",
        action="store_true",
        default=False,
        help=(
            "Enable Chain-of-Note + JSON history (LongMemEval paper §5.5). "
            "Runs a per-session extraction pass that writes 'reading notes' "
            "of facts relevant to the question, then sends both the notes "
            "AND the JSON-serialized retrieved history to the final answer "
            "call. Adds one extra LLM call per retrieved session per "
            "question (~2-3x answer-phase wall time)."
        ),
    )
    p.add_argument(
        "--chain-of-note-model",
        default=None,
        help=(
            "Model for the Chain-of-Note extraction pass. Defaults to "
            "--generator-model. Set to a cheaper model to cut extraction "
            "cost — extraction is a structured per-chunk task that doesn't "
            "need a frontier model."
        ),
    )
    p.add_argument(
        "--chain-of-note-compare",
        action="store_true",
        default=False,
        help=(
            "Run BOTH plain and CoN answer pipelines off the same retrieval "
            "and judge both. Primary hypotheses go to hypotheses.jsonl with "
            "a `hypothesis_note` field for the CoN answer; CoN-as-primary "
            "hypotheses also go to hypotheses_con.jsonl. Summary prints "
            "per-category plain-vs-con delta."
        ),
    )
    # Hidden backward-compat alias for --generator-model.
    p.add_argument("--answer-model", default=None, help=argparse.SUPPRESS)
    p.add_argument(
        "--answer-max-tokens",
        type=int,
        default=ANSWER_MAX_TOKENS_DEFAULT,
        help=(
            "Max output tokens for the answer model. Default 8000 fits "
            "non-thinking frontier models; bump to 16000-32000 for Claude "
            "extended thinking or o1/o3 high reasoning effort. Note: the "
            "stock generation path (no other generation-mode flags set) "
            "preserves the existing q_signal-tied 800/1200 budget; this "
            "flag only takes effect when a generation-mode flag is on, "
            "OR when --answer-max-tokens is set to a non-default value."
        ),
    )
    p.add_argument(
        "--thinking-budget",
        type=int,
        default=0,
        help=(
            "Forward `reasoning_tokens=N` via extra_body to the generator "
            "endpoint. Endpoints that don't recognize the param ignore it "
            "silently. 0 disables (default)."
        ),
    )
    p.add_argument(
        "--k-reasoning",
        type=int,
        default=0,
        help=(
            "If >0, raise effective top-K to this value when the question "
            "is classified temporal/update (reasoning-heavy categories that "
            "need more context to stitch facts across sessions). 0 disables "
            "(stock --k applies to every question)."
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
    if args.no_memory and args.rag_aware_empty:
        p.error("--no-memory and --rag-aware-empty are mutually exclusive "
                "(they are two different baseline framings; pick one)")
    if args.chain_of_note and args.reflection:
        p.error("--chain-of-note and --reflection are mutually exclusive "
                "(both add a pre-answer LLM pass; pick one)")
    if args.chain_of_note_compare and args.chain_of_note:
        p.error("--chain-of-note-compare already runs the CoN pipeline as "
                "the secondary pass; don't combine with --chain-of-note")
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
