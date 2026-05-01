#!/usr/bin/env python3
"""Phase D Mastra-style Reflector drainer.

Pulls eligible (user_id, conversation_id) groups from reflector_queue,
loads their existing + new observations from memory_items, calls the
Reflector SLM (qwen/qwen3-8b on LM Studio /v1/messages by default per
config/slm/reflector_local.yaml), parses {observations, supersedes}
output, and translates the supersedes list into memory_link_impl rows
with relationship_type='supersedes'.

m3's existing _check_contradictions does the embedding-based detection
on writes; the Reflector adds an LLM-based pass that catches semantic
contradictions the embedding similarity might miss (different wording,
different attributes).

Modes:
  - Drain mode (default): work through reflector_queue with backoff.
  - Force mode (--force-conversation CID): trigger Reflector immediately
    on a single conversation, bypass queue. Useful for tests.

Status: Phase D Task 4. Pairs with config/slm/reflector_local.yaml.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_MAIN_BIN = REPO_ROOT / "bin"
if str(_MAIN_BIN) not in sys.path:
    sys.path.insert(0, str(_MAIN_BIN))

import httpx  # noqa: E402
import memory_core as mc  # noqa: E402
from agent_protocol import strip_code_fences  # noqa: E402
from auth_utils import get_api_key  # noqa: E402
from slm_intent import load_profile  # noqa: E402

PROFILE_NAME = os.environ.get("REFLECTOR_PROFILE", "reflector_local")
JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_reflector_output(text: str) -> tuple[list[dict], list[dict]]:
    """Parse {observations, supersedes} JSON. Returns (observations, supersedes).
    Drops malformed entries silently."""
    text = strip_code_fences(text)
    m = JSON_RE.search(text)
    if not m:
        return [], []
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return [], []
    obs = obj.get("observations", [])
    sup = obj.get("supersedes", [])
    if not isinstance(obs, list):
        obs = []
    if not isinstance(sup, list):
        sup = []
    # Filter supersedes: drop entries where new_text == old_text (no-op merges
    # the Reflector sometimes emits) and entries with empty values.
    sup_clean = []
    for s in sup:
        if not isinstance(s, dict):
            continue
        nt = (s.get("new_text") or "").strip()
        ot = (s.get("old_text") or "").strip()
        if not nt or not ot or nt == ot:
            continue
        sup_clean.append({"new_text": nt, "old_text": ot})
    return obs, sup_clean


async def call_reflector(
    existing: list[dict],
    new: list[dict],
    profile,
    client: httpx.AsyncClient,
    token: str,
) -> tuple[list[dict], list[dict]]:
    """Call the Reflector SLM and return (observations, supersedes)."""
    user_text = json.dumps({"existing": existing, "new": new}, ensure_ascii=False)
    backend = getattr(profile, "backend", "openai")
    max_tokens = getattr(profile, "max_tokens", 8192)
    input_max_chars = getattr(profile, "input_max_chars", 40000)
    if input_max_chars and len(user_text) > input_max_chars:
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
            raise RuntimeError(f"reflector http {r.status_code}: {r.text[:200]}")
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
            raise RuntimeError(f"reflector http {r.status_code}: {r.text[:200]}")
        data = r.json()
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    return parse_reflector_output(text)


def _load_observations_for_conv(
    user_id: str,
    conversation_id: str,
    new_only: bool = False,
) -> list[tuple[str, dict]]:
    """Load all type='observation' rows for a (user_id, conversation_id) pair.
    Returns list of (memory_item_id, observation_dict) tuples sorted by
    observation_date.

    new_only: when True, return only observations whose Reflector-link edge
    does not already exist (i.e. unprocessed observations the latest pass
    must consider). Defers implementation — Phase D2.
    """
    with mc._db() as db:
        sql = """
        SELECT id, content, valid_from, created_at, metadata_json
        FROM memory_items
        WHERE type='observation'
          AND COALESCE(is_deleted,0)=0
          AND user_id = ?
          AND json_extract(metadata_json, '$.conversation_id') = ?
        ORDER BY json_extract(metadata_json, '$.observation_date') ASC, id ASC
        """
        rows = db.execute(sql, (user_id or "", conversation_id)).fetchall()
    out: list[tuple[str, dict]] = []
    for rid, content, vfrom, cat, meta in rows:
        try:
            md = json.loads(meta) if meta else {}
        except Exception:
            md = {}
        out.append((rid, {
            "text": content or "",
            "observation_date": md.get("observation_date") or (cat[:10] if cat else None),
            "referenced_date": md.get("referenced_date") or vfrom,
            "relative_date": md.get("relative_date"),
            "confidence": md.get("confidence", 0.85),
        }))
    return out


def _find_observation_id_by_text(
    rows: list[tuple[str, dict]], target_text: str
) -> str | None:
    """Linear scan for an observation row whose text matches the
    Reflector's text reference. Used to translate supersedes pairs into
    actual memory_item id pairs for memory_link_impl. Trims and lowercases
    for comparison since the Reflector may slightly rephrase."""
    target = target_text.strip().lower()
    for rid, obs in rows:
        if (obs["text"] or "").strip().lower() == target:
            return rid
    # Fallback: prefix match (Reflector may truncate)
    for rid, obs in rows:
        ot = (obs["text"] or "").strip().lower()
        if ot.startswith(target[:60]) or target.startswith(ot[:60]):
            return rid
    return None


async def reflect_conversation(
    user_id: str,
    conversation_id: str,
    profile,
    client: httpx.AsyncClient,
    token: str,
    counters: dict,
) -> None:
    """Load observations for a (user, conversation) pair, partition into
    existing + new (defer Phase D2 — for now treat all as new), call
    Reflector, and write supersedes edges from each new→old pair."""
    rows = _load_observations_for_conv(user_id, conversation_id)
    if len(rows) < 2:
        counters["empty_groups"] += 1
        return
    # For Phase D1 v1 we treat the entire observation set as `new` and pass
    # an empty `existing` — this lets the Reflector do dedup + supersede
    # detection across the entire conversation. Future enhancement: pass
    # observations from PRIOR sessions (via memory_search) as `existing`.
    existing: list[dict] = []
    new = [obs for _, obs in rows]
    try:
        merged_obs, supersedes = await call_reflector(existing, new, profile, client, token)
    except Exception as e:  # noqa: BLE001
        counters["failed"] += 1
        if counters["failed"] <= 5:
            print(f"[reflector] FAIL conv={conversation_id[:8]}: {e}", flush=True)
        return

    counters["processed"] += 1
    counters["sup_emitted"] += len(supersedes)
    if not supersedes:
        return

    # Translate text-pair supersedes into memory_id-pair edges.
    edges_written = 0
    for s in supersedes:
        new_id = _find_observation_id_by_text(rows, s["new_text"])
        old_id = _find_observation_id_by_text(rows, s["old_text"])
        if not new_id or not old_id or new_id == old_id:
            continue
        try:
            with mc._db() as db:
                mc.memory_link_impl(
                    from_id=new_id,
                    to_id=old_id,
                    relationship_type="supersedes",
                    db=db,
                )
            edges_written += 1
        except Exception as e:  # noqa: BLE001
            if counters["failed"] <= 5:
                print(f"[reflector] link FAIL ({new_id[:8]}->{old_id[:8]}): {e}", flush=True)
    counters["sup_written"] += edges_written


async def drain_queue_mode(args, profile, token: str) -> None:
    """Pop rows from reflector_queue, run Reflector, mark complete or update last_error."""
    sem = asyncio.Semaphore(args.concurrency)
    counters = {
        "processed": 0, "sup_emitted": 0, "sup_written": 0,
        "failed": 0, "empty_groups": 0,
    }
    started = time.monotonic()

    with mc._db() as db:
        queue_rows = db.execute(
            """
            SELECT id, conversation_id, user_id, attempts
            FROM reflector_queue
            WHERE attempts < 5
            ORDER BY attempts ASC, enqueued_at ASC
            LIMIT ?
            """,
            (args.batch,)
        ).fetchall()
    if not queue_rows:
        print("[reflector] queue empty; nothing to drain", flush=True)
        return
    print(f"[reflector] queue: {len(queue_rows)} rows pending", flush=True)

    async with httpx.AsyncClient() as client:
        async def drain_one(qid: int, conv_id: str, uid: str, _attempts: int) -> None:
            async with sem:
                try:
                    await reflect_conversation(
                        uid or "", conv_id, profile, client, token, counters,
                    )
                    with mc._db() as db:
                        db.execute("DELETE FROM reflector_queue WHERE id=?", (qid,))
                        db.commit()
                except Exception as e:  # noqa: BLE001
                    with mc._db() as db:
                        db.execute(
                            "UPDATE reflector_queue SET attempts=attempts+1, "
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
    print(f"\n[reflector] queue drain DONE in {elapsed/60:.1f}m", flush=True)
    print(f"  groups processed:   {counters['processed']}", flush=True)
    print(f"  supersedes emitted: {counters['sup_emitted']}", flush=True)
    print(f"  supersedes written: {counters['sup_written']}", flush=True)
    print(f"  empty groups:       {counters['empty_groups']}", flush=True)
    print(f"  failed groups:      {counters['failed']}", flush=True)


async def force_mode(args, profile, token: str) -> None:
    """Run Reflector immediately on a single (user_id, conversation_id) pair,
    bypassing the queue. Used for tests and one-off triggering."""
    counters = {
        "processed": 0, "sup_emitted": 0, "sup_written": 0,
        "failed": 0, "empty_groups": 0,
    }
    async with httpx.AsyncClient() as client:
        await reflect_conversation(
            args.force_user or "", args.force_conversation,
            profile, client, token, counters,
        )
    print(f"[reflector] force-mode result for conv={args.force_conversation[:12]}: ", flush=True)
    print(f"  processed:          {counters['processed']}", flush=True)
    print(f"  supersedes emitted: {counters['sup_emitted']}", flush=True)
    print(f"  supersedes written: {counters['sup_written']}", flush=True)
    print(f"  empty groups:       {counters['empty_groups']}", flush=True)
    print(f"  failed groups:      {counters['failed']}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Phase D Reflector drainer (Mastra-style merge/supersede LLM pass)"
    )
    ap.add_argument("--concurrency", type=int, default=2,
                    help="Concurrent Reflector SLM calls.")
    ap.add_argument("--batch", type=int, default=50,
                    help="Queue-mode batch size per invocation. Default 50.")
    ap.add_argument("--force-conversation", default=None,
                    help="Bypass queue: run Reflector on this conversation_id "
                         "right now. Useful for tests.")
    ap.add_argument("--force-user", default=None,
                    help="Required when --force-conversation is set: the user_id "
                         "to scope the observation lookup.")
    args = ap.parse_args()

    profile = load_profile(PROFILE_NAME)
    if not profile:
        sys.exit(f"ERROR: profile {PROFILE_NAME!r} not found. "
                 f"Set REFLECTOR_PROFILE env var or create config/slm/{PROFILE_NAME}.yaml.")
    token = get_api_key(profile.api_key_service) or ""
    if not token:
        sys.exit(f"ERROR: no token resolved for service {profile.api_key_service!r}")

    if args.force_conversation:
        asyncio.run(force_mode(args, profile, token))
    else:
        asyncio.run(drain_queue_mode(args, profile, token))


if __name__ == "__main__":
    main()
