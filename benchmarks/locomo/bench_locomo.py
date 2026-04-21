"""Dialog-QA benchmark runner for m3-memory.

Loads the configured dialog-QA dataset, bulk-ingests every conversation turn
into m3-memory scoped by sample_id, then for each question retrieves
the top-K most relevant turns and asks a generator LLM to answer.
A separate judge LLM scores the answer (model configured by --judge-model
or the EVAL_JUDGE_MODEL env var).

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
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR / "bin"))

# Route embeddings to llama-server before memory_core imports.
os.environ.setdefault("LLM_ENDPOINTS_CSV", "http://localhost:1234/v1")

import temporal_utils  # noqa: E402
from auth_utils import get_api_key  # noqa: E402
from memory_core import (  # noqa: E402
    _db,
    memory_link_impl,
    memory_write_bulk_impl,
)

DEFAULT_DATASET = BASE_DIR / "data" / "locomo" / "locomo10.json"

_LOCOMO_DATE_RE = re.compile(
    r"(\d+):(\d+)\s+(am|pm)\s+on\s+(\d+)\s+([A-Za-z]+),\s+(\d+)"
)


def parse_locomo_date(date_str: str) -> datetime | None:
    """Parses LOCOMO date format like '1:56 pm on 8 May, 2023'"""
    try:
        match = _LOCOMO_DATE_RE.search(date_str)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2))
            meridiem = match.group(3).lower()
            day = int(match.group(4))
            month_name = match.group(5).lower()
            year = int(match.group(6))

            if meridiem == "pm" and hour < 12:
                hour += 12
            if meridiem == "am" and hour == 12:
                hour = 0

            month = temporal_utils.MONTHS.index(month_name) + 1
            return datetime(year, month, day, hour, minute)
    except Exception:
        pass
    return None


temporal_utils.register_anchor_parser(parse_locomo_date)

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
        "You are a temporal reasoning expert. Use the provided History, Timeline, and Anchors to answer the question.\n"
        "IMPORTANT: When a person says 'yesterday', 'last week', or 'last year', they are referring to the date of THAT SPECIFIC CONVERSATION SESSION, not 'Today\\'s Date'.\n"
        "1. Identify the session where the event is mentioned.\n"
        "2. Note the date of that session.\n"
        "3. Apply the relative time offset (e.g. 'last year') to that session's date.\n"
        "4. Use 'Temporal Anchors' if they provide an absolute date for the same event.\n"
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
            "from a model. Please answer 'yes' if the model correctly identifies the question "
            "as unanswerable, even if it provides a brief explanation.\n\n"
            f"Question: {question}\n\nExplanation: {answer}\n\nModel Response: {response}\n\n"
            "Does the model correctly identify the question as unanswerable? Answer 'yes' or 'no' only."
        )
    return (
        "I will give you a question, a correct ground-truth answer, and a response from a model. "
        "The model response might contain step-by-step reasoning before its final answer. "
        "Please answer 'yes' if the response contains the correct answer. Otherwise, answer 'no'. "
        "Focus on the semantic correctness of the final answer, ignoring reasoning steps.\n\n"
        f"Question: {question}\n\nCorrect Ground-truth Answer: {answer}\n\nModel Response: {response}\n\n"
        "Is the model response correct? Answer 'yes' or 'no' only."
    )

def _safe_loads(blob: str | None) -> dict:
    """Defensive JSON parse — returns {} on invalid/empty input."""
    if not blob: return {}
    try: return json.loads(blob)
    except (json.JSONDecodeError, TypeError): return {}

async def ingest_sample_with_graph(sample: dict, variant: str = "") -> tuple[int, float]:
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
        anchor_dt = parse_locomo_date(sess_date_str)

        for t_idx, turn in enumerate(conv[sess_key]):
            role = speaker_a if turn.get("speaker") == "speaker_a" else speaker_b
            dia_id = turn.get("dia_id")
            mid = str(uuid.uuid4())
            if dia_id: dia_map[dia_id] = mid

            content = turn.get("text", "") or ""
            anchors = temporal_utils.resolve_temporal_expressions(content, anchor_dt)
            ref_year = anchor_dt.year if isinstance(anchor_dt, datetime) else 2023
            ref_dates = temporal_utils.extract_referenced_dates(content, default_year=ref_year)
            # Promote resolved relative dates ("last Saturday") into referenced_dates.
            # LOCOMO turns rarely contain absolute dates; most temporal signal is relative.
            anchor_dates = [a["absolute"] for a in anchors if a.get("absolute")]
            if anchor_dates:
                ref_dates = sorted(set((ref_dates or []) + anchor_dates))

            meta = {
                "role": role, "session_id": f"S{i}", "session_date": sess_date_str,
                "session_index": i, "turn_index": t_idx, "dia_id": dia_id,
                "temporal_anchors": anchors,
            }
            if ref_dates:
                meta["referenced_dates"] = ref_dates
            items.append({
                "id": mid,
                "type": "message",
                "title": f"{role}:{sid}:S{i}:T{t_idx}",
                "content": content,
                "user_id": sid,
                "conversation_id": f"{sid}::S{i}",
                "source": "locomo",
                "embed": True,
                "metadata": meta,
            })

    # 2. Build Observation Items + Links
    obs_links: list[tuple[str, str]] = []
    for i in range(1, 36):
        ok = f"session_{i}_observation"
        if ok not in obs or not obs[ok]: continue

        sess_date_str = conv.get(f"session_{i}_date_time", "Unknown")
        anchor_dt = parse_locomo_date(sess_date_str)
        obs_data = obs[ok]
        if not isinstance(obs_data, dict): continue

        for speaker, lines in obs_data.items():
            for line in lines:
                text = line[0]
                ev = line[1]
                anchors = temporal_utils.resolve_temporal_expressions(text, anchor_dt)
                ref_year = anchor_dt.year if isinstance(anchor_dt, datetime) else 2023
                ref_dates = temporal_utils.extract_referenced_dates(text, default_year=ref_year)
                anchor_dates = [a["absolute"] for a in anchors if a.get("absolute")]
                if anchor_dates:
                    ref_dates = sorted(set((ref_dates or []) + anchor_dates))
                mid = str(uuid.uuid4())
                obs_meta = {
                    "session_index": i, "session_date": sess_date_str,
                    "speaker": speaker, "temporal_anchors": anchors,
                }
                if ref_dates:
                    obs_meta["referenced_dates"] = ref_dates
                items.append({
                    "id": mid,
                    "type": "observation",
                    "title": f"Obs:{speaker}:S{i}",
                    "content": text,
                    "user_id": sid,
                    "conversation_id": f"{sid}::S{i}",
                    "source": "locomo_obs",
                    "embed": True,
                    "metadata": obs_meta,
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
        anchor_dt = parse_locomo_date(sess_date_str)
        content = sums[sk]
        anchors = temporal_utils.resolve_temporal_expressions(content, anchor_dt)
        ref_year = anchor_dt.year if isinstance(anchor_dt, datetime) else 2023
        ref_dates = temporal_utils.extract_referenced_dates(content, default_year=ref_year)
        anchor_dates = [a["absolute"] for a in anchors if a.get("absolute")]
        if anchor_dates:
            ref_dates = sorted(set((ref_dates or []) + anchor_dates))
        sum_meta = {
            "session_index": i, "session_date": sess_date_str,
            "temporal_anchors": anchors,
        }
        if ref_dates:
            sum_meta["referenced_dates"] = ref_dates
        items.append({
            "type": "note",
            "title": f"Sum:S{i}",
            "content": content,
            "user_id": sid,
            "conversation_id": f"{sid}::S{i}",
            "source": "locomo_sum",
            "embed": True,
            "metadata": sum_meta,
        })

    t0 = time.perf_counter()
    await memory_write_bulk_impl(items, variant=variant)
    await asyncio.sleep(0.1)

    count = 0
    with _db() as db:
        if obs_links:
            for from_id, to_id in obs_links:
                res = memory_link_impl(from_id, to_id, "references", db=db)
                if "Linked:" in res: count += 1

        # 4. Link Consecutive Messages in Session (Optimal Strategy)
        for i in range(1, 36):
            cid = f"{sid}::S{i}"
            rows = db.execute(
                "SELECT id FROM memory_items WHERE conversation_id=? AND type='message' ORDER BY created_at ASC",
                (cid,)
            ).fetchall()
            prev_mid = None
            for row in rows:
                curr_mid = row[0]
                if prev_mid:
                    memory_link_impl(prev_mid, curr_mid, "precedes", db=db)
                    count += 1
                prev_mid = curr_mid

        # 5. Link Session Summaries to Messages
        for i in range(1, 36):
            sum_title = f"Sum:S{i}"
            cid = f"{sid}::S{i}"
            s_row = db.execute("SELECT id FROM memory_items WHERE title=? AND user_id=? AND is_deleted=0", (sum_title, sid)).fetchone()
            if s_row:
                s_id = s_row[0]
                # Link summary to the first 3 messages of the session as entry points
                m_rows = db.execute("SELECT id FROM memory_items WHERE conversation_id=? AND type='message' ORDER BY created_at ASC LIMIT 3", (cid,)).fetchall()
                for m_row in m_rows:
                    memory_link_impl(s_id, m_row[0], "references", db=db)
                    count += 1

        # 6. Link Consecutive Summaries
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

async def retrieve_for_question(
    sid: str, question: str, k: int,
    cluster_size: int = 0, graph_depth: int = 0, q_signal: str = "default",
    smart_time_boost: float = 0.0, smart_neighbor_sessions: int = 0,
) -> list[dict]:
    from memory_core import memory_search_scored_impl

    # Per-category K gating
    actual_k = k
    if q_signal == "temporal": actual_k = max(k, 20)
    elif q_signal == "adversarial": actual_k = min(k, 10)

    # Recency bias for temporal and multi-hop
    rb = 0.15 if q_signal in ["temporal", "multi-hop"] else 0.0

    ranked = await memory_search_scored_impl(
        question, k=actual_k, user_id=sid,
        extra_columns=["metadata_json", "conversation_id", "valid_from", "user_id"],
        recency_bias=rb,
        smart_time_boost=smart_time_boost,
        smart_neighbor_sessions=smart_neighbor_sessions,
    )
    if not ranked: return []
    hits = []
    seen_ids = set()
    for score, item in ranked:
        item["score"] = score
        if "metadata_json" in item:
            item["metadata"] = _safe_loads(item["metadata_json"])
        if "metadata" not in item: item["metadata"] = {}
        hits.append(item)
        seen_ids.add(item["id"])

    if graph_depth > 0 and hits:
        graph_hits = []
        with _db() as db:
            for h in hits:
                rows = db.execute(
                    "SELECT mi.id, mi.content, mi.title, mi.metadata_json, mi.conversation_id "
                    "FROM memory_items mi JOIN memory_relationships mr ON (mi.id = mr.from_id OR mi.id = mr.to_id) "
                    "WHERE (mr.from_id = ? OR mr.to_id = ?) AND mi.id != ? AND mi.is_deleted = 0",
                    (h["id"], h["id"], h["id"])
                ).fetchall()
                for r in rows:
                    if r["id"] not in seen_ids:
                        seen_ids.add(r["id"])
                        rm = _safe_loads(r["metadata_json"])
                        graph_hits.append({
                            "id": r["id"], "content": r["content"], "title": r["title"],
                            "metadata": rm, "conversation_id": r["conversation_id"],
                            "score": h["score"] * 0.8
                        })
        hits.extend(graph_hits)

    if cluster_size > 0:
        expanded = []
        # Cache per-conversation turn rows so we don't re-query for each hit in same session.
        session_cache: dict[str, list] = {}
        with _db() as db:
            db.execute("PRAGMA busy_timeout = 30000")
            for h in hits:
                expanded.append(h)
                m = h.get("metadata", {})
                cid = h.get("conversation_id")
                if "turn_index" in m and cid:
                    t_idx = m["turn_index"]
                    if cid not in session_cache:
                        session_cache[cid] = db.execute(
                            "SELECT id, content, title, metadata_json, conversation_id "
                            "FROM memory_items WHERE conversation_id = ? AND is_deleted = 0",
                            (cid,)
                        ).fetchall()
                    for r in session_cache[cid]:
                        rm = _safe_loads(r["metadata_json"])
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

    # First pass: classify hits and collect conversation_ids needing backfill.
    backfill_cids: set[str] = set()
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
            if h.get("conversation_id"):
                backfill_cids.add(h["conversation_id"])

    # Second pass: single DB session for all session-backfill fetches.
    if backfill_cids:
        with _db() as db:
            for cid in backfill_cids:
                rows = db.execute(
                    "SELECT content, title, metadata_json FROM memory_items "
                    "WHERE conversation_id=? AND type='message' AND is_deleted=0 "
                    "ORDER BY created_at ASC LIMIT 5",
                    (cid,),
                ).fetchall()
                session_list = by_session.setdefault(cid, [])
                existing_contents = {t["content"] for t in session_list}
                for r in rows:
                    if r["content"] not in existing_contents and len(session_list) < MAX_TURNS:
                        session_list.append({
                            "content": r["content"],
                            "title": r["title"],
                            "metadata": _safe_loads(r["metadata_json"]),
                        })
                        existing_contents.add(r["content"])

    if obs_and_sums:
        lines.append("[Observations and Summaries]")
        # Cap observations to top 5 most relevant
        obs_and_sums.sort(key=lambda x: x.get("score", 0), reverse=True)
        for item in obs_and_sums[:5]:
            date = item.get("metadata", {}).get("session_date", "")
            lines.append(f"({date}) {item['title']}: {item['content']}")
        lines.append("")
    def _session_sort_key(x: str) -> int:
        if "::S" not in x: return 0
        try: return int(x.rsplit("::S", 1)[-1])
        except ValueError: return 0
    sorted_sessions = sorted(by_session.keys(), key=_session_sort_key)
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

def _openai_client(api_key: str, base_url: str | None = None):
    try:
        from openai import OpenAI
    except ImportError:
        raise SystemExit("pip install openai") from None

    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
        logger.info(f"Connecting to provider at {base_url}")
    else:
        logger.info("Connecting to official OpenAI/Anthropic endpoint")
    return OpenAI(**kwargs)

async def run(args: argparse.Namespace) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = BASE_DIR / ".scratch" / f"locomo_run_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    hyp_path, results_path, log_path = out_dir / "hypotheses.jsonl", out_dir / "results.json", out_dir / "run.log"
    log_f = open(log_path, "a", encoding="utf-8", buffering=1)
    def log(msg: str):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        try: log_f.write(line + "\n")
        except Exception: pass
    log(f"loading dataset: {args.dataset}")
    with open(args.dataset, "r", encoding="utf-8") as f: dataset = json.load(f)
    if args.limit_samples: dataset = dataset[: args.limit_samples]

    # Priority: Env > Vault
    api_key = os.getenv("OPENAI_API_KEY") or get_api_key("OPENAI_API_KEY")

    if not args.generator_model:
        raise SystemExit(
            "generator model is not set — pass --generator-model or set EVAL_GENERATOR_MODEL"
        )
    if not args.judge_model:
        raise SystemExit(
            "judge model is not set — pass --judge-model or set EVAL_JUDGE_MODEL"
        )

    # Generator uses the provided base_url (e.g. local proxy / LM Studio)
    gen_client = _openai_client(api_key, base_url=args.openai_base_url)
    # Judge uses the default base_url so it can route independently
    judge_client = _openai_client(api_key, base_url=None)

    if not args.skip_ingest:
        log("phase 1: ingest with graph linking + temporal resolution")
        total_items = 0
        for i, sample in enumerate(dataset):
            n, _ = await ingest_sample_with_graph(sample, variant=args.variant)
            total_items += n
            log(f"  sample {i+1}/{len(dataset)}: {n} items")
        log(f"ingest done: {total_items} items")

    if args.ingest_only:
        log("Stopping after phase 1 as requested by --ingest-only")
        return

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

                # If using a model with a thinking/reasoning phase (e.g. Qwen or DeepSeek),
                # append /no_think to the system prompt to force direct answers for retrieval benchmarking.
                if any(k in args.generator_model.lower() for k in ["qwen", "deepseek"]):
                    if not sys_prompt.endswith("/no_think"):
                        sys_prompt = sys_prompt.strip() + " /no_think"

                smart_on = bool(getattr(args, "smart_retrieval", False))
                hits = await retrieve_for_question(
                    sid, q, args.k, args.cluster_size, args.graph_depth, q_signal=q_signal,
                    smart_time_boost=0.15 if smart_on else 0.0,
                    smart_neighbor_sessions=1 if smart_on else 0,
                )
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
                answer_ok = False
                for attempt in range(3):
                    try:
                        # Offload blocking HTTP call so the event loop stays responsive.
                        resp = await asyncio.to_thread(
                            gen_client.chat.completions.create,
                            model=args.generator_model, messages=messages,
                            temperature=0, max_tokens=1024,
                        )
                        msg = resp.choices[0].message
                        if args.verbose:
                            log(f"  DEBUG: msg object: {msg}")

                        # Capture standard content OR reasoning_content (common in local Qwen/DeepSeek)
                        hyp = (msg.content or "").strip()
                        if not hyp:
                            hyp = getattr(msg, "reasoning_content", "") or getattr(msg, "reasoning", "")
                            if hyp:
                                hyp = hyp.strip()

                        if not hyp:
                            log(f"  WARNING: Empty hypothesis from model {args.generator_model} (finish_reason={resp.choices[0].finish_reason}).")
                        answer_ok = True
                        break
                    except Exception as e:
                        log(f"  ERROR: completion failed (attempt {attempt+1}): {e}")
                        if attempt < 2:
                            await asyncio.sleep(2**attempt)
                if not answer_ok:
                    log(f"  FATAL: all {3} answer attempts exhausted for Q{total_q}; recording empty hypothesis.")

                correct = False
                judge_status = "skipped_empty_hyp"
                if hyp:
                    for attempt in range(3):
                        try:
                            resp = await asyncio.to_thread(
                                judge_client.chat.completions.create,
                                model=args.judge_model,
                                messages=[{"role": "user", "content": judge_prompt(qtype_label, q, str(a), hyp)}],
                                temperature=0,
                                max_tokens=100,
                            )
                            raw = (resp.choices[0].message.content or "")
                            finish = resp.choices[0].finish_reason
                            if not raw.strip():
                                log(f"  WARNING: empty judge response (finish_reason={finish}); retrying.")
                                judge_status = f"empty_response_finish={finish}"
                                if attempt < 2:
                                    await asyncio.sleep(2**attempt)
                                    continue
                                break
                            correct = "yes" in raw.lower()
                            judge_status = "ok"
                            break
                        except Exception as e:
                            log(f"  ERROR: judge failed (attempt {attempt+1}): {e}")
                            judge_status = f"error:{type(e).__name__}"
                            if attempt < 2:
                                await asyncio.sleep(2**attempt)
                    if judge_status.startswith(("error:", "empty_response")):
                        log(f"  FATAL: judge unusable for Q{total_q} ({judge_status}); marking correct=False.")
                qtype_correct[qtype_label].append(1 if correct else 0)
                hyp_f.write(json.dumps({
                    "sample_id": sid, "question": q, "reference": a,
                    "hypothesis": hyp, "correct": correct,
                    "qtype": qtype_label, "signal": q_signal,
                    "judge_status": judge_status,
                }, ensure_ascii=False) + "\n")
                hyp_f.flush()
            if args.limit_questions and total_q >= args.limit_questions: break
            log(f"  sample done. running_acc={sum(sum(v) for v in qtype_correct.values())/total_q:.3f}")
    total_correct = sum(sum(v) for v in qtype_correct.values())
    overall = total_correct / total_q if total_q else 0
    summary = {"n_questions": total_q, "overall_accuracy": overall, "per_type": {qt: {"n": len(v), "acc": sum(v)/len(v)} for qt, v in qtype_correct.items()}}
    with open(results_path, "w", encoding="utf-8") as f: json.dump(summary, f, indent=2)
    log(f"overall accuracy: {overall:.4f}\nresults -> {results_path}")
    try: log_f.close()
    except Exception: pass

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    p.add_argument("--limit-samples", type=int, default=0)
    p.add_argument("--limit-questions", type=int, default=0)
    p.add_argument("--skip-ingest", action="store_true")
    p.add_argument("--ingest-only", action="store_true")
    p.add_argument("--k", type=int, default=40)
    p.add_argument("--cluster-size", type=int, default=5)
    p.add_argument("--graph-depth", type=int, default=1)
    p.add_argument("--generator-model",
                   default=os.environ.get("EVAL_GENERATOR_MODEL"))
    p.add_argument("--judge-model",
                   default=os.environ.get("EVAL_JUDGE_MODEL"))
    p.add_argument("--openai-base-url", default=None, help="Custom base URL for OpenAI-compatible API (e.g. MCP proxy or LM Studio)")
    p.add_argument("--variant", default="", help="Pipeline identifier passed to bulk-insert and enrichers for A/B tracking.")
    p.add_argument("--smart-retrieval", action="store_true",
                   help="Enable smart_time_boost + neighbor-session expansion for time-aware retrieval.")
    p.add_argument("--verbose", action="store_true", help="Dump full msg objects per question into run.log")
    return p.parse_args()

if __name__ == "__main__": asyncio.run(run(parse_args()))
