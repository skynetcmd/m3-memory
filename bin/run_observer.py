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
from slm_intent import load_profile  # noqa: E402
from auth_utils import get_api_key  # noqa: E402

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
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
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
        out.append({
            "text": t[:500],
            "observation_date": obs_date,
            "referenced_date": ref_date,
            "relative_date": rel_date,
            "confidence": max(0.0, min(1.0, conf)),
            "supersedes_hint": sup_hint,
        })
    return out


async def call_observer(
    session_block: dict,
    profile,
    client: httpx.AsyncClient,
    token: str,
) -> list[dict]:
    """Call the Observer SLM and return parsed observations.

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
    return parse_observations(text)


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
) -> str | None:
    """Write a single observation as a type='observation' memory_items row.

    Three-date mapping per MASTRA_DESIGN.md section 3:
      observation_date → created_at (auto-set by m3 to "now"; we override
                          via valid_from-style explicit pass through metadata
                          for audit purposes)
      referenced_date  → valid_from
      relative_date    → metadata_json.relative_date
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
    m = re.search(r"[0-9a-f-]{36}", result)
    return m.group(0) if m else None


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
) -> None:
    """Build the session block, call Observer (possibly N chunks for long
    conversations), write all observations.

    Chunking semantics: a conversation whose serialized JSON would exceed
    profile.input_max_chars is split into chunks. Each chunk is sent as
    its own Observer call; observations from all chunks are collected
    and written together. The session_date is shared across all chunks
    (taken from the first turn's metadata)."""
    if not turns:
        counters["empty_groups"] += 1
        return
    # Use the earliest turn's session_date as the canonical observation_date.
    # Falls back to the row's created_at if no metadata.session_date.
    session_date = "unknown"
    for t in turns:
        meta = t[5] if len(t) > 5 else None
        if meta:
            try:
                m = json.loads(meta)
                if m.get("session_date"):
                    session_date = str(m["session_date"]).split(" ")[0].replace("/", "-")
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
        print(f"[observer] conv={conversation_id[:8]}: {len(turns)} turns → "
              f"{len(chunks)} chunks", flush=True)

    observations: list[dict] = []
    for ci, chunk in enumerate(chunks):
        block = _build_session_block(chunk, session_date)
        try:
            chunk_obs = await call_observer(block, profile, client, token)
            observations.extend(chunk_obs)
        except Exception as e:  # noqa: BLE001
            counters["failed"] += 1
            if counters["failed"] <= 5:
                print(f"[observer] FAIL conv={conversation_id[:8]} "
                      f"chunk={ci}/{len(chunks)}: {e}", flush=True)
            # Continue to next chunk rather than aborting the whole conversation.
            continue

    counters["processed"] += 1
    if not observations:
        counters["empty_groups"] += 1
        return

    source_turn_ids = [t[0] for t in turns]
    for obs in observations:
        obs_id = await write_observation(
            obs, target_variant, user_id, conversation_id, source_turn_ids
        )
        if obs_id:
            counters["written"] += 1


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
    with mc._db() as db:
        sql = """
        SELECT mi.id,
               mi.content,
               json_extract(mi.metadata_json, '$.role') AS role,
               COALESCE(json_extract(mi.metadata_json, '$.turn_index'), 0) AS turn_index,
               mi.created_at,
               mi.metadata_json,
               json_extract(mi.metadata_json, '$.session_id') AS conversation_id,
               mi.user_id
        FROM memory_items mi
        WHERE mi.variant = ?
          AND COALESCE(mi.is_deleted, 0) = 0
          AND mi.type IN ('message', 'conversation')
        """
        params: list = [args.source_variant]
        if qid_filter:
            placeholder = ",".join(["?"] * len(qid_filter))
            sql += f" AND mi.user_id IN ({placeholder})"
            params.extend(qid_filter)
        sql += " ORDER BY mi.user_id, conversation_id, turn_index"
        if args.limit:
            sql += " LIMIT ?"
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

    async with httpx.AsyncClient() as client:
        async def gated(uid: str, cid: str, turns: list[tuple]) -> None:
            async with sem:
                await process_conversation(
                    cid, uid, turns, args.target_variant,
                    profile, client, token, counters,
                )
                done = counters["processed"] + counters["empty_groups"] + counters["failed"]
                if done % 25 == 0:
                    elapsed = time.monotonic() - started
                    rate = done / max(elapsed, 1e-3)
                    eta = (total_groups - done) / max(rate, 1e-3)
                    print(
                        f"[observer] {done}/{total_groups}  "
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

    async with httpx.AsyncClient() as client:
        # Single-shot drain: pull up to --batch rows from the queue. Caller
        # invokes us repeatedly via the CLI (or cron) for ongoing drain.
        with mc._db() as db:
            queue_rows = db.execute(
                """
                SELECT id, conversation_id, user_id, attempts
                FROM observation_queue
                WHERE attempts < 5
                ORDER BY attempts ASC, enqueued_at ASC
                LIMIT ?
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
                        """
                        SELECT id, content,
                               json_extract(metadata_json, '$.role') AS role,
                               COALESCE(json_extract(metadata_json, '$.turn_index'), 0) AS turn_index,
                               created_at, metadata_json
                        FROM memory_items
                        WHERE COALESCE(json_extract(metadata_json, '$.session_id'),
                                       json_extract(metadata_json, '$.conversation_id'),
                                       conversation_id) = ?
                          AND COALESCE(is_deleted,0)=0
                          AND type IN ('message','conversation')
                        ORDER BY turn_index ASC
                        """,
                        (conv_id,)
                    ).fetchall()
                if not turns:
                    with mc._db() as db:
                        db.execute("DELETE FROM observation_queue WHERE id=?", (qid,))
                        db.commit()
                    counters["empty_groups"] += 1
                    return

                try:
                    await process_conversation(
                        conv_id, uid or "", list(turns),
                        args.target_variant or "", profile, client, token, counters,
                    )
                    with mc._db() as db:
                        db.execute("DELETE FROM observation_queue WHERE id=?", (qid,))
                        db.commit()
                except Exception as e:  # noqa: BLE001
                    with mc._db() as db:
                        db.execute(
                            "UPDATE observation_queue SET attempts=attempts+1, "
                            "last_error=?, last_attempt_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                            "WHERE id=?",
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
    ap = argparse.ArgumentParser(description="Phase D Observer drainer (Mastra-style three-date observations)")
    ap.add_argument("--source-variant", default=None,
                    help="Variant-mode: pull conversations from this variant. "
                         "When set, drains the entire variant; ignores observation_queue.")
    ap.add_argument("--target-variant", default="",
                    help="Variant tag for emitted observation rows. Empty = production default (NULL).")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap source rows in variant mode (for smokes).")
    ap.add_argument("--concurrency", type=int, default=4,
                    help="Concurrent Observer SLM calls.")
    ap.add_argument("--qids-file", default=None,
                    help="JSON dataset; scope variant-mode work to its question_ids.")
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
