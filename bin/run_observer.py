#!/usr/bin/env python3
"""Phase D Mastra-style Observer drainer.

Pulls eligible (user_id, conversation_id) groups from observation_queue,
builds a JSON multi-turn block, calls the Observer SLM (qwen/qwen3-8b on
LM Studio /v1/messages by default per config/slm/observer_local.yaml),
parses {observations: [...]} output, and writes type='observation' rows
with three-date metadata:

  observation_date  → memory_items.created_at (when assistant logged it)
  referenced_date   → memory_items.valid_from (when fact is about)
  relative_date     → metadata_json.relative_date (audit-only)
  supersedes_hint   → metadata_json.supersedes_hint (Reflector input)
  confidence        → metadata_json.confidence

Usage modes:
  - Drain mode (default): work through the observation_queue, retrying with
    backoff. Used by the production CLI (`m3 observe-pending`) and the
    bench harness.
  - Variant mode (--source-variant + --target-variant): bench-style
    one-shot enrichment over a corpus snapshot, like run_fact_enrichment.py
    does for fact_enriched. Skips the queue entirely; pulls all eligible
    conversations from --source-variant.

Status: Phase D Task 3. Pairs with config/slm/observer_local.yaml.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Production-first sys.path: main bin first, bench bin appended at end.
_MAIN_BIN = REPO_ROOT / "bin"
if str(_MAIN_BIN) not in sys.path:
    sys.path.insert(0, str(_MAIN_BIN))
_BENCH_BIN = REPO_ROOT.parent / "m3-memory-bench" / "bin"
if _BENCH_BIN.exists() and str(_BENCH_BIN) not in sys.path:
    sys.path.append(str(_BENCH_BIN))

import httpx  # noqa: E402
import memory_core as mc  # noqa: E402
from agent_protocol import strip_code_fences  # noqa: E402
from auth_utils import get_api_key  # noqa: E402
from slm_intent import load_profile  # noqa: E402

PROFILE_NAME = os.environ.get("OBSERVER_PROFILE", "observer_local")


def _ingest_llm_enabled_from_env(flag: str) -> bool:
    """Truthy-string check on env. Mirrors memory_core._ingest_llm_enabled
    without importing it (keeps run_observer importable when memory_core
    fails to load — e.g. test fixtures with stubbed schemas)."""
    return os.environ.get(flag, "0").strip().lower() in ("1", "true", "yes", "on")


# Greedy regex to grab the outer JSON object including nested braces.
JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
# ISO-8601 date validator. Accepts both "2023-05-22" and "2023/05/22"
# (LM-S session_date format) — the latter is normalized in _normalize_date.
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# memory_write_impl returns a free-form result string; we extract the
# new memory's UUID from it. Hot path — fires per observation written
# (hundreds of thousands of times during a corpus enrichment run).
UUID_RE = re.compile(r"[0-9a-f-]{36}")


def _normalize_date(s: str | None) -> str | None:
    """Coerce date strings to ISO-8601. Accepts '2023-05-22', '2023/05/22',
    or '2023/05/22 (Mon) 14:30'. Returns None for null / empty / unparseable."""
    if s is None:
        return None
    s = str(s).strip()
    if not s or s.lower() in ("null", "none", ""):
        return None
    # Strip trailing weekday + time if present
    s = s.split(" ")[0].split("T")[0]
    s = s.replace("/", "-")
    return s if ISO_DATE_RE.match(s) else None


def parse_observations(text: str) -> list[dict]:
    """Parse Observer SLM output into a normalized list of observations.

    Strips code fences, extracts the outer JSON object, validates each
    observation has at least 'text' and 'observation_date'. Coerces dates
    to ISO-8601. Drops malformed entries silently — the drainer logs the
    miss count separately.
    """
    text = strip_code_fences(text)
    m = JSON_RE.search(text)
    if not m:
        return []
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    obs_in = obj.get("observations", [])
    if not isinstance(obs_in, list):
        return []
    out: list[dict] = []
    for o in obs_in:
        if not isinstance(o, dict):
            continue
        t = str(o.get("text", "")).strip()
        if not t or len(t) < 4:
            continue
        obs_date = _normalize_date(o.get("observation_date"))
        if not obs_date:
            continue
        ref_date = _normalize_date(o.get("referenced_date"))
        rel_date = o.get("relative_date")
        if rel_date is not None and not isinstance(rel_date, str):
            rel_date = None
        # Coerce literal string "null"/"none" (model emits when null was meant) to actual None.
        if isinstance(rel_date, str) and rel_date.lower().strip() in ("null", "none", ""):
            rel_date = None
        try:
            conf = float(o.get("confidence", 0.85))
        except (TypeError, ValueError):
            conf = 0.85
        if conf < 0.6:
            continue  # Per Observer prompt rule 5.
        sup_hint = o.get("supersedes_hint")
        if sup_hint is not None and not isinstance(sup_hint, str):
            sup_hint = None
        # Per-observation source turn. The Observer prompt asks for the
        # turn_index this fact came from; carry it through so each observation
        # links to the SPECIFIC turn it was extracted from (precise provenance /
        # citation), rather than the whole session. Coerce to int; None when the
        # model omitted/garbled it (content-overlap attribution then fills it).
        # Accept a single int or the first of a list.
        sti = o.get("source_turn_index")
        if isinstance(sti, list):
            sti = sti[0] if sti else None
        try:
            sti = int(sti) if sti is not None else None
        except (TypeError, ValueError):
            sti = None
        out.append({
            "text": t[:500],
            "observation_date": obs_date,
            "referenced_date": ref_date,
            "relative_date": rel_date,
            "confidence": max(0.0, min(1.0, conf)),
            "supersedes_hint": sup_hint,
            "source_turn_index": sti,
        })
    return out


def _attribute_turn(obs: dict, turns: list[tuple]) -> "int | None":
    """Resolve an observation to its source turn by content overlap when the
    model did not emit a usable source_turn_index.

    turns: (id, content, role, turn_index, ts) tuples. Returns the turn_index of
    the best-matching USER turn (token-overlap of the obs text against turn
    content), or None if no turn shares >=2 content tokens. Deterministic; no
    model call. A defensive fallback so observation provenance is still resolved
    when the model's self-reported index is missing or wrong."""
    import re as _re
    def toks(s: str) -> set:
        return {w for w in _re.findall(r"[a-z0-9]{3,}", (s or "").lower())}
    o_toks = toks(obs.get("text", ""))
    if not o_toks:
        return None
    best_idx, best_score = None, 0
    for t in turns:
        if (t[2] or "user") != "user":
            continue  # facts are about the user's turns
        score = len(o_toks & toks(str(t[1] or "")))
        if score > best_score:
            best_score, best_idx = score, t[3]
    return best_idx if best_score >= 2 else None


async def call_observer(
    session_block: dict,
    profile,
    client: httpx.AsyncClient,
    token: str,
) -> tuple[list[dict], dict]:
    """Call the Observer SLM and return (parsed_observations, usage_meta).

    usage_meta is a dict with keys tokens_in, tokens_out, cost_usd (zeros
    when the upstream response doesn't include them). Used by the budget
    watchdog and per-row cost provenance in enrichment_groups.

    Cost computation: openai-shape responses sometimes carry
    cost_in_usd_ticks (xAI returns this in nanocents == USD * 1e9). When
    absent, callers can compute cost from profile.input_cost_per_mtok and
    profile.output_cost_per_mtok if those are set; we don't compute it
    here to keep this function provider-neutral.

    Dispatches on profile.backend (anthropic / openai), mirroring the
    same pattern as run_fact_enrichment.py. Anthropic shape is the default
    for qwen/qwen3-8b — the OAI-compat path leaks reasoning into
    reasoning_content and trips token caps.
    """
    user_text = json.dumps(session_block, ensure_ascii=False)
    backend = getattr(profile, "backend", "openai")
    max_tokens = getattr(profile, "max_tokens", 4096)
    input_max_chars = getattr(profile, "input_max_chars", 20000)
    if input_max_chars and len(user_text) > input_max_chars:
        # Drop trailing turns rather than mid-string truncate. Caller can
        # paginate by splitting the conversation in half.
        # For this iteration we just truncate; real pagination is task-future.
        user_text = user_text[:input_max_chars]

    usage_meta = {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}

    if backend == "anthropic":
        payload = {
            "model": profile.model,
            "max_tokens": max_tokens,
            "system": profile.system,
            "messages": [{"role": "user", "content": user_text}],
            "temperature": profile.temperature,
        }
        headers = {
            "Content-Type": "application/json",
            "x-api-key": token,
            "anthropic-version": getattr(profile, "anthropic_version", "2023-06-01"),
        }
        r = await client.post(profile.url, json=payload, headers=headers,
                              timeout=profile.timeout_s)
        if r.status_code != 200:
            raise RuntimeError(f"observer http {r.status_code}: {r.text[:200]}")
        data = r.json()
        blocks = data.get("content", [])
        text = "".join(
            b.get("text", "") for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
        # Anthropic usage shape: input_tokens / output_tokens
        u = data.get("usage") or {}
        usage_meta["tokens_in"] = int(u.get("input_tokens") or 0)
        usage_meta["tokens_out"] = int(u.get("output_tokens") or 0)
    else:
        payload = {
            "model": profile.model,
            "temperature": profile.temperature,
            "messages": [
                {"role": "system", "content": profile.system},
                {"role": "user", "content": user_text},
            ],
            "max_tokens": max_tokens,
        }
        # Profile may pass additional OpenAI-shape params (e.g.
        # reasoning_effort: "none" for Gemini 2.5 Flash to suppress thinking).
        extras = getattr(profile, "extra_params", None) or {}
        if extras:
            payload.update(extras)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        r = await client.post(profile.url, json=payload, headers=headers,
                              timeout=profile.timeout_s)
        if r.status_code != 200:
            raise RuntimeError(f"observer http {r.status_code}: {r.text[:200]}")
        data = r.json()
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        # OpenAI-compat usage shape: prompt_tokens / completion_tokens.
        # xAI extension: cost_in_usd_ticks (USD * 1e9).
        u = data.get("usage") or {}
        usage_meta["tokens_in"] = int(u.get("prompt_tokens") or 0)
        usage_meta["tokens_out"] = int(u.get("completion_tokens") or 0)
        ticks = u.get("cost_in_usd_ticks")
        if ticks is not None:
            try:
                usage_meta["cost_usd"] = float(ticks) / 1e9
            except (TypeError, ValueError):
                pass

    # Provider-neutral fallback: when cost_usd wasn't returned by the API
    # but the profile carries per-mtok pricing, compute from token counts.
    # Lets us track Anthropic + Gemini cost without provider-specific code
    # paths in the enricher hot loop. Fields are optional: profiles that
    # don't set them simply leave cost_usd at 0.
    if usage_meta["cost_usd"] == 0.0:
        in_per_m = getattr(profile, "input_cost_per_mtok", None)
        out_per_m = getattr(profile, "output_cost_per_mtok", None)
        if in_per_m or out_per_m:
            usage_meta["cost_usd"] = (
                (usage_meta["tokens_in"] / 1_000_000.0) * float(in_per_m or 0)
                + (usage_meta["tokens_out"] / 1_000_000.0) * float(out_per_m or 0)
            )

    return parse_observations(text), usage_meta


def _build_session_block(turns: list[tuple], session_date: str) -> dict:
    """Convert a sequence of memory_items rows into the JSON block the
    Observer expects. turns is a list of (id, content, role, turn_index,
    timestamp) tuples sorted by turn_index."""
    return {
        "session_date": session_date,
        "turns": [
            {
                "turn_index": t[3],
                "role": t[2] or "user",
                "text": str(t[1] or "")[:2000],  # per-turn cap; protects total budget
            }
            for t in turns
        ],
    }


async def write_observation(
    obs: dict,
    target_variant: str,
    user_id: str,
    conversation_id: str,
    source_turn_ids: list[str],
    source_group_id: int | None = None,
    session_id: str = "",
) -> str | None:
    """Write a single observation as a type='observation' memory_items row.

    Three-date mapping per MASTRA_DESIGN.md section 3:
      observation_date → created_at (auto-set by m3 to "now"; we override
                          via valid_from-style explicit pass through metadata
                          for audit purposes)
      referenced_date  → valid_from
      relative_date    → metadata_json.relative_date

    `session_id` (when non-empty) is the upstream session identifier copied
    from the source turn's metadata.session_id. Required for SHR scoring on
    LongMemEval bench runs; downstream callers can pass empty for non-bench
    contexts.
    """
    md = {
        "observation_date": obs["observation_date"],
        "referenced_date": obs["referenced_date"],
        "relative_date": obs["relative_date"],
        "confidence": obs["confidence"],
        "supersedes_hint": obs["supersedes_hint"],
        "source_turn_ids": source_turn_ids,
        "conversation_id": conversation_id,
    }
    if session_id:
        md["session_id"] = session_id
    valid_from = obs["referenced_date"] or obs["observation_date"]
    # When M3_OBSERVER_NO_EMBED is set, skip the embedding pass. Used by unit
    # tests that don't have the embedding endpoint available; in production
    # / bench mode the default embed=True path runs (needed for retrieval).
    embed = not _ingest_llm_enabled_from_env("M3_OBSERVER_NO_EMBED")
    result = await mc.memory_write_impl(
        type="observation",
        content=obs["text"],
        metadata=json.dumps(md),
        change_agent="observer",
        source="observer",
        variant=target_variant,
        user_id=user_id,
        valid_from=valid_from,
        embed=embed,
    )
    m = UUID_RE.search(result)
    obs_id = m.group(0) if m else None
    # Link the observation back to its source enrichment_groups row (when
    # provided). The column is nullable; absence means "this run wasn't
    # using the state machine" — backwards-compatible.
    if obs_id and source_group_id is not None:
        try:
            # Route through the backend-aware _db() (the previous raw
            # sqlite3.connect(M3_DATABASE) bypassed the seam and would edit a stale
            # SQLite file on a PG-primary deployment). On PG this hits the pool; on
            # SQLite it opens the same active-context connection as before.
            from memory.backends import dialect
            _p = dialect().param()
            with mc._db() as conn:
                conn.execute(
                    f"UPDATE memory_items SET source_group_id = {_p} WHERE id = {_p}",
                    (source_group_id, obs_id),
                )
                conn.commit()
        except Exception:
            # Don't break a successful write on a metadata-only failure.
            pass
    return obs_id


def _chunk_turns(turns: list[tuple], max_chunk_chars: int) -> list[list[tuple]]:
    """Split a turns list into chunks whose serialized JSON fits within
    max_chunk_chars. Each chunk preserves turn ordering. Used for long
    conversations (Claude Code sessions can have 2000+ turns / 1.5MB
    JSON; the SLM input cap is 20kB).

    The split is conservative — we sum each turn's content length plus
    overhead (~80 chars per turn for JSON wrappers) and break the chunk
    when adding the next turn would exceed max_chunk_chars * 0.85
    (15% safety margin for JSON serialization deltas).
    """
    if not turns:
        return []
    safe_budget = int(max_chunk_chars * 0.85) - 200  # 200 = session_date+turns wrapper
    chunks: list[list[tuple]] = []
    current: list[tuple] = []
    current_size = 0
    for t in turns:
        # t = (id, content, role, turn_index, created_at, metadata_json)
        # JSON: {"turn_index":N,"role":"user","text":"..."},  + per-turn ~60c overhead
        turn_size = len(str(t[1] or "")[:2000]) + 60
        if current and current_size + turn_size > safe_budget:
            chunks.append(current)
            current = []
            current_size = 0
        current.append(t)
        current_size += turn_size
    if current:
        chunks.append(current)
    return chunks


async def process_conversation(
    conversation_id: str,
    user_id: str,
    turns: list[tuple],
    target_variant: str,
    profile,
    client: httpx.AsyncClient,
    token: str,
    counters: dict,
    source_group_id: int | None = None,
) -> dict:
    """Build the session block, call Observer (possibly N chunks for long
    conversations), write all observations.

    Chunking semantics: a conversation whose serialized JSON would exceed
    profile.input_max_chars is split into chunks. Each chunk is sent as
    its own Observer call; observations from all chunks are collected
    and written together. The session_date is shared across all chunks
    (taken from the first turn's metadata).

    Returns a per-group result dict describing THIS group's outcome so
    concurrent callers can persist terminal state without reading it back
    out of the shared `counters` dict across an await boundary (that race
    misattributes counts and can leave a group unmarked, causing it to be
    re-fed to the LLM on the next --resume). Shape:
        {"outcome": "written"|"empty"|"failed"|"skipped",
         "written": int, "tokens_in": int, "tokens_out": int,
         "cost_usd": float, "partial_failure_chunks": int,
         "last_error": str | None}
    The shared `counters` dict is still updated for aggregate run reporting;
    existing callers that ignore the return value are unaffected."""
    if not turns:
        counters["empty_groups"] += 1
        return {"outcome": "skipped", "written": 0, "tokens_in": 0,
                "tokens_out": 0, "cost_usd": 0.0, "partial_failure_chunks": 0,
                "last_error": None}
    # Use the earliest turn's session_date as the canonical observation_date.
    # Falls back to the row's created_at if no metadata.session_date.
    # While scanning, also lift session_id from the source turns so observations
    # can be traced back to the LongMemEval session (or any upstream session
    # identifier) without a separate DB lookup. Bench SHR scoring requires
    # metadata.session_id on observation rows; without it observation hits are
    # invisible to the metric (memory 914843f8, 2026-05-05).
    session_date = "unknown"
    source_session_id = ""
    for t in turns:
        meta = t[5] if len(t) > 5 else None
        if meta:
            try:
                m = json.loads(meta)
                if not source_session_id:
                    sid = m.get("session_id")
                    if sid:
                        source_session_id = str(sid)
                if m.get("session_date") and session_date == "unknown":
                    session_date = str(m["session_date"]).split(" ")[0].replace("/", "-")
                if source_session_id and session_date != "unknown":
                    break
            except Exception:
                pass
    if session_date == "unknown" and turns:
        # last-resort: use created_at first 10 chars (ISO date prefix)
        ca = turns[0][4] if len(turns[0]) > 4 and turns[0][4] else None
        if ca and len(str(ca)) >= 10:
            session_date = str(ca)[:10]

    # Chunk if the conversation is bigger than what the profile can ingest
    # in one call. Single-chunk path (most conversations) is unchanged from
    # the pre-chunking implementation.
    input_max = getattr(profile, "input_max_chars", 20000) or 20000
    chunks = _chunk_turns(turns, input_max)
    if len(chunks) > 1:
        print(f"[observer] conv={conversation_id[:8]}: {len(turns)} turns -> "
              f"{len(chunks)} chunks", flush=True)

    observations: list[dict] = []
    chunk_fail_count = 0
    # Accumulate per-chunk usage so the caller can attribute cost to this
    # group on the enrichment_groups row.
    group_tokens_in = 0
    group_tokens_out = 0
    group_cost_usd = 0.0
    for ci, chunk in enumerate(chunks):
        block = _build_session_block(chunk, session_date)
        try:
            chunk_obs, chunk_usage = await call_observer(block, profile, client, token)
            observations.extend(chunk_obs)
            group_tokens_in += chunk_usage.get("tokens_in", 0)
            group_tokens_out += chunk_usage.get("tokens_out", 0)
            group_cost_usd += chunk_usage.get("cost_usd", 0.0)
        except Exception as e:  # noqa: BLE001
            chunk_fail_count += 1
            # Stash the most recent error so wrappers (e.g. m3_enrich's
            # state machine) can record a real message instead of a
            # generic "chunk(s) failed" placeholder. repr() preserves the
            # exception class even when str() is empty (httpx
            # ConnectError frequently has empty str).
            counters["last_error"] = f"{type(e).__name__}: {e!r}"
            if counters["failed"] + chunk_fail_count <= 5:
                print(f"[observer] FAIL conv={conversation_id[:8]} "
                      f"chunk={ci}/{len(chunks)}: {type(e).__name__}: {e!r}",
                      flush=True)
            # Continue to next chunk rather than aborting the whole conversation.
            continue

    # Surface usage to the caller. Cumulative session totals also tracked
    # in counters["total_*"] for cross-group budget queries that don't
    # round-trip through the DB.
    counters["last_tokens_in"] = group_tokens_in
    counters["last_tokens_out"] = group_tokens_out
    counters["last_cost_usd"] = group_cost_usd
    counters["total_tokens_in"] = counters.get("total_tokens_in", 0) + group_tokens_in
    counters["total_tokens_out"] = counters.get("total_tokens_out", 0) + group_tokens_out
    counters["total_cost_usd"] = counters.get("total_cost_usd", 0.0) + group_cost_usd

    if not observations:
        # Group produced nothing. If any chunk failed, classify as failed;
        # otherwise it's an empty group. Counters are group-scoped (one bump
        # per group, never per chunk) so `done = processed + empty + failed`
        # in the caller doesn't double-count.
        if chunk_fail_count > 0:
            counters["failed"] += 1
            return {"outcome": "failed", "written": 0,
                    "tokens_in": group_tokens_in, "tokens_out": group_tokens_out,
                    "cost_usd": group_cost_usd,
                    "partial_failure_chunks": chunk_fail_count,
                    "last_error": counters.get("last_error")}
        counters["empty_groups"] += 1
        return {"outcome": "empty", "written": 0,
                "tokens_in": group_tokens_in, "tokens_out": group_tokens_out,
                "cost_usd": group_cost_usd, "partial_failure_chunks": 0,
                "last_error": None}
    counters["processed"] += 1
    # Surface partial-failure count to the caller (m3_enrich) so it can
    # record it on the success row. Multi-chunk groups where some chunks
    # failed but others succeeded land here with chunk_fail_count > 0 —
    # the partial observations are valid and worth keeping, but the row
    # gets flagged for later audit/re-extraction.
    if chunk_fail_count > 0:
        counters["last_partial_failure_chunks"] = chunk_fail_count
    else:
        counters["last_partial_failure_chunks"] = 0

    all_turn_ids = [t[0] for t in turns]
    # Per-observation source-turn provenance (opt-in, default OFF for behavior
    # stability). DEFAULT: every observation carries the whole session's turn
    # list in source_turn_ids — the long-standing behavior every caller expects.
    # When M3_OBSERVER_PRECISE_PROVENANCE=1: link each observation to the SPECIFIC
    # turn it came from (model-reported source_turn_index, with a content-overlap
    # fallback), so downstream callers can cite the exact source turn, trace
    # supersession to a single statement, and link an observation to one message.
    # The fallback (_attribute_turn) only runs under the flag, so the default
    # path is unchanged and adds no per-observation work.
    precise = os.environ.get("M3_OBSERVER_PRECISE_PROVENANCE", "0") == "1"
    idx_to_id = {t[3]: t[0] for t in turns} if precise else {}
    group_written = 0
    for obs in observations:
        if precise:
            sti = obs.get("source_turn_index")
            if sti is None or sti not in idx_to_id:
                sti = _attribute_turn(obs, turns)  # deterministic fallback
            if sti is not None and sti in idx_to_id:
                src_ids = [idx_to_id[sti]]         # the specific source turn
            else:
                src_ids = all_turn_ids             # unresolved -> whole session
                counters["addr_fallback_session"] = counters.get("addr_fallback_session", 0) + 1
        else:
            src_ids = all_turn_ids                 # default: whole session (unchanged)
        obs_id = await write_observation(
            obs, target_variant, user_id, conversation_id, src_ids,
            source_group_id=source_group_id,
            session_id=source_session_id,
        )
        if obs_id:
            counters["written"] += 1
            group_written += 1

    if group_written == 0:
        # Every write_observation returned falsy (e.g. dedup/no-op). No
        # observations landed for this group; report empty rather than a
        # success with obs_emitted=0 so the caller doesn't mark it done-with-
        # zero and (worse) so a re-run isn't misled about what happened.
        return {"outcome": "empty", "written": 0,
                "tokens_in": group_tokens_in, "tokens_out": group_tokens_out,
                "cost_usd": group_cost_usd,
                "partial_failure_chunks": chunk_fail_count,
                "last_error": None}
    return {"outcome": "written", "written": group_written,
            "tokens_in": group_tokens_in, "tokens_out": group_tokens_out,
            "cost_usd": group_cost_usd,
            "partial_failure_chunks": chunk_fail_count,
            "last_error": None}


async def drain_variant_mode(args, profile, token: str) -> None:
    """Bench-style drain: pull all eligible conversations from --source-variant,
    skip the queue, write to --target-variant. Mirrors run_fact_enrichment.py
    but operates on conversation groups rather than per-turn."""

    qid_filter: list[str] = []
    if args.qids_file:
        with open(args.qids_file, encoding="utf-8") as f:
            data = json.load(f)
        qid_filter = [d["question_id"] for d in data if "question_id" in d]
        print(f"[observer] scoped to {len(qid_filter)} qids from {args.qids_file}", flush=True)

    # Pull all candidate turns grouped by (user_id, conversation_id).
    # The bench corpus stores conversation_id in metadata_json.session_id (its
    # original LME-S identifier) — we use that as the conversation grouping
    # key here.
    # Source memory types to drain. Default = live message/conversation. Corpora
    # stored under another type (e.g. an imported chat_log corpus) override via
    # --source-type. Allowlist-validated so the value is never unsafely interpolated.
    _ALLOWED_SRC_TYPES = {"message", "conversation", "chat_log"}
    src_types = [t.strip() for t in (getattr(args, "source_type", None) or "message,conversation").split(",") if t.strip()]
    bad = [t for t in src_types if t not in _ALLOWED_SRC_TYPES]
    if bad:
        sys.exit(f"ERROR: --source-type values not allowed: {bad} (allowed: {sorted(_ALLOWED_SRC_TYPES)})")
    from memory.backends import dialect
    _d = dialect()
    _p = _d.param()
    type_ph = _d.placeholder(len(src_types))
    # Variant filter: sentinel '__none__' selects NULL-variant rows (SQL '= NULL'
    # never matches — must be 'IS NULL'); any other value is an exact match.
    if args.source_variant == "__none__":
        variant_clause = "mi.variant IS NULL"
        variant_params: list = []
    else:
        variant_clause = f"mi.variant = {_p}"
        variant_params = [args.source_variant]
    # qid scoping column: production chatlog keys on user_id; corpora where the
    # scoping id lives in the conversation_id column (one row-group per instance)
    # use --qid-column conversation_id. Validated against an allowlist.
    qid_col = getattr(args, "qid_column", None) or "user_id"
    if qid_col not in ("user_id", "conversation_id"):
        sys.exit(f"ERROR: --qid-column must be user_id or conversation_id, got {qid_col!r}")
    _role = _d.json_extract_text("mi.metadata_json", "role")
    # turn_index/turn_idx are numeric (COALESCE'd with an int literal, ordered by):
    # extract as INTEGER so PG doesn't hit a text/int COALESCE type mismatch.
    _turn_i = _d.json_extract_int("mi.metadata_json", "turn_index")
    _turn_ix = _d.json_extract_int("mi.metadata_json", "turn_idx")
    _sess = _d.json_extract_text("mi.metadata_json", "session_id")
    with mc._db() as db:
        sql = f"""
        SELECT mi.id,
               mi.content,
               {_role} AS role,
               COALESCE({_turn_i}, {_turn_ix}, 0) AS turn_index,
               mi.created_at,
               mi.metadata_json,
               {_sess} AS conversation_id,
               mi.user_id
        FROM memory_items mi
        WHERE {variant_clause}
          AND COALESCE(mi.is_deleted, 0) = 0
          AND mi.type IN ({type_ph})
        """
        params: list = [*variant_params, *src_types]
        if qid_filter:
            placeholder = _d.placeholder(len(qid_filter))
            sql += f" AND mi.{qid_col} IN ({placeholder})"
            params.extend(qid_filter)
        sql += " ORDER BY mi.user_id, conversation_id, turn_index"
        if args.limit:
            sql += f" LIMIT {_p}"
            params.append(int(args.limit))
        rows = list(db.execute(sql, params).fetchall())

    # Group by (user_id, conversation_id).
    groups: dict[tuple, list[tuple]] = defaultdict(list)
    for row in rows:
        # row layout: id, content, role, turn_index, created_at, metadata_json, conversation_id, user_id
        conv_id = row[6] or row[0]  # fallback to row id if no session_id
        user_id = row[7] or ""
        groups[(user_id, conv_id)].append(row[:6])  # drop conv_id/user_id from per-turn tuple

    total_groups = len(groups)
    total_turns = sum(len(g) for g in groups.values())
    print(f"[observer] {total_groups} conversations, {total_turns} turns "
          f"under variant={args.source_variant!r}; writing to "
          f"variant={args.target_variant!r}", flush=True)
    if total_groups == 0:
        return

    sem = asyncio.Semaphore(args.concurrency)
    counters = {"processed": 0, "written": 0, "failed": 0, "empty_groups": 0}
    started = time.monotonic()

    from unified_ai import async_client_for_profile
    async with async_client_for_profile(profile) as client:
        async def gated(uid: str, cid: str, turns: list[tuple]) -> None:
            async with sem:
                await process_conversation(
                    cid, uid, turns, args.target_variant,
                    profile, client, token, counters,
                )
                done = counters["processed"] + counters["empty_groups"] + counters["failed"]
                if done % 50 == 0:
                    elapsed = time.monotonic() - started
                    rate = done / max(elapsed, 1e-3)
                    eta = (total_groups - done) / max(rate, 1e-3)
                    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    print(
                        f"[{ts}] [observer] {done}/{total_groups}  "
                        f"obs_written={counters['written']} "
                        f"empty={counters['empty_groups']} "
                        f"failed={counters['failed']}  "
                        f"rate={rate:.2f}conv/s eta={eta/60:.1f}m",
                        flush=True,
                    )

        await asyncio.gather(*(
            gated(uid, cid, turns)
            for (uid, cid), turns in groups.items()
        ))

    elapsed = time.monotonic() - started
    print(f"\n[observer] DONE in {elapsed/60:.1f}m", flush=True)
    print(f"  groups processed: {counters['processed']}", flush=True)
    print(f"  observations written: {counters['written']}", flush=True)
    print(f"  empty groups: {counters['empty_groups']}", flush=True)
    print(f"  failed groups: {counters['failed']}", flush=True)


async def drain_queue_mode(args, profile, token: str) -> None:
    """Production drain: pop rows from observation_queue with backoff,
    process each, mark complete or update last_error."""
    sem = asyncio.Semaphore(args.concurrency)
    counters = {"processed": 0, "written": 0, "failed": 0, "empty_groups": 0}
    started = time.monotonic()
    from memory.backends import dialect
    _d = dialect()
    _p = _d.param()
    _je_role = _d.json_extract_text("metadata_json", "role")
    # turn_index is compared/ordered numerically and COALESCE'd with an int
    # literal — on PG `->>` yields text, so a text/int COALESCE mismatch errors.
    # Extract as INTEGER so both COALESCE arms are integers on both backends.
    _je_ti = _d.json_extract_int("metadata_json", "turn_index")
    _je_sid = _d.json_extract_text("metadata_json", "session_id")
    _je_cid = _d.json_extract_text("metadata_json", "conversation_id")

    from unified_ai import async_client_for_profile
    async with async_client_for_profile(profile) as client:
        # Single-shot drain: pull up to --batch rows from the queue. Caller
        # invokes us repeatedly via the CLI (or cron) for ongoing drain.
        with mc._db() as db:
            queue_rows = db.execute(
                f"""
                SELECT id, conversation_id, user_id, attempts
                FROM observation_queue
                WHERE attempts < 5
                ORDER BY attempts ASC, enqueued_at ASC
                LIMIT {_p}
                """,
                (args.batch,)
            ).fetchall()
        if not queue_rows:
            print("[observer] queue empty; nothing to drain", flush=True)
            return

        print(f"[observer] queue: {len(queue_rows)} rows pending", flush=True)

        async def drain_one(qid: int, conv_id: str, uid: str, attempts: int) -> None:
            async with sem:
                # Pull turns for this conversation from its variant. Production
                # rows live under the default (NULL) variant; bench rows under
                # the named variant. We infer from a single turn's variant.
                with mc._db() as db:
                    turns = db.execute(
                        f"""
                        SELECT id, content,
                               {_je_role} AS role,
                               COALESCE({_je_ti}, 0) AS turn_index,
                               created_at, metadata_json
                        FROM memory_items
                        WHERE COALESCE({_je_sid}, {_je_cid}, conversation_id) = {_p}
                          AND COALESCE(is_deleted,0)=0
                          AND type IN ('message','conversation')
                        ORDER BY turn_index ASC
                        """,
                        (conv_id,)
                    ).fetchall()
                if not turns:
                    with mc._db() as db:
                        db.execute(f"DELETE FROM observation_queue WHERE id={_p}", (qid,))
                        db.commit()
                    counters["empty_groups"] += 1
                    return

                try:
                    await process_conversation(
                        conv_id, uid or "", list(turns),
                        args.target_variant or "", profile, client, token, counters,
                    )
                    with mc._db() as db:
                        db.execute(f"DELETE FROM observation_queue WHERE id={_p}", (qid,))
                        db.commit()
                except Exception as e:  # noqa: BLE001
                    with mc._db() as db:
                        db.execute(
                            f"UPDATE observation_queue SET attempts=attempts+1, "
                            f"last_error={_p}, last_attempt_at={_d.now()} "
                            f"WHERE id={_p}",
                            (str(e)[:500], qid),
                        )
                        db.commit()
                    counters["failed"] += 1

        await asyncio.gather(*(
            drain_one(r[0], r[1], r[2], r[3]) for r in queue_rows
        ))

    elapsed = time.monotonic() - started
    print(f"\n[observer] queue drain DONE in {elapsed/60:.1f}m", flush=True)
    print(f"  groups processed: {counters['processed']}", flush=True)
    print(f"  observations written: {counters['written']}", flush=True)
    print(f"  empty groups: {counters['empty_groups']}", flush=True)
    print(f"  failed groups: {counters['failed']}", flush=True)


def main() -> None:
    # Backend-agnostic: all DB access routes through the backend-aware mc._db()
    # and is dialected (placeholders + json_extract + now()), so the observer
    # drains the live primary store on both SQLite and PostgreSQL. (Previously
    # gated SQLite-only because it opened a raw sqlite3.connect; that write path
    # was moved onto the seam and verified on PG, so the gate was removed.)
    ap = argparse.ArgumentParser(description="Phase D Observer drainer (Mastra-style three-date observations)")
    ap.add_argument("--source-variant", default=None,
                    help="Variant-mode: pull conversations from this variant. "
                         "When set, drains the entire variant; ignores observation_queue. "
                         "Sentinel '__none__' selects rows whose variant IS NULL.")
    ap.add_argument("--source-type", default=None,
                    help="Comma-separated source memory types to drain "
                         "(default: message,conversation; allowed: message,conversation,chat_log).")
    ap.add_argument("--qid-column", default=None,
                    help="Column the --qids-file ids filter on: user_id (default) or "
                         "conversation_id (for corpora where the scoping id lives there).")
    ap.add_argument("--target-variant", default="",
                    help="Variant tag for emitted observation rows. Empty = production default (NULL).")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap source rows in variant mode (for smokes).")
    ap.add_argument("--concurrency", type=int, default=4,
                    help="Concurrent Observer SLM calls.")
    ap.add_argument("--qids-file", default=None,
                    help="Optional JSON file with a list of ids; scopes variant-mode work to those ids only.")
    ap.add_argument("--batch", type=int, default=100,
                    help="Queue-mode batch size per invocation. Default 100.")
    args = ap.parse_args()

    profile = load_profile(PROFILE_NAME)
    if not profile:
        sys.exit(f"ERROR: profile {PROFILE_NAME!r} not found. "
                 f"Set OBSERVER_PROFILE env var or create config/slm/{PROFILE_NAME}.yaml.")
    token = get_api_key(profile.api_key_service) or ""
    if not token:
        sys.exit(f"ERROR: no token resolved for service {profile.api_key_service!r}")

    if args.source_variant:
        if not args.target_variant:
            sys.exit("ERROR: variant-mode requires --target-variant")
        asyncio.run(drain_variant_mode(args, profile, token))
    else:
        asyncio.run(drain_queue_mode(args, profile, token))


if __name__ == "__main__":
    main()
