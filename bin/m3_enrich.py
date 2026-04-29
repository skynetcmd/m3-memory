#!/usr/bin/env python3
"""m3_enrich — User-facing enrichment CLI for core memory + chatlogs.

Wraps Phase D Mastra Observer + Reflector into a single tool that any
m3 user can run on their own DBs. Supports local SLMs (LM Studio,
Ollama) and frontier cloud (Anthropic Haiku/Sonnet, OpenAI gpt-4o-mini,
Google Gemini) via YAML profiles in config/slm/.

Quick start (LM Studio + qwen3-8b loaded):
    python bin/m3_enrich.py --dry-run        # preview
    python bin/m3_enrich.py                   # enrich both DBs

Pick a different profile:
    python bin/m3_enrich.py --profile enrich_anthropic_haiku
    python bin/m3_enrich.py --profile-path /path/to/my_profile.yaml

Scope:
    --core              # only enrich agent_memory.db
    --chatlog           # only enrich agent_chatlog.db
    --include-summaries # add type='summary' rows to allowlist
    --include-notes     # add type='note' rows
    --include-types t,t # extend allowlist with custom types (additive)
    --only-use-types t,t # replace allowlist entirely (no defaults merged in)

Output:
    Observations are written as type='observation' rows under variant
    --target-variant (default: m3-observations-YYYYMMDD). Read them back
    with mcp__memory__memory_search or any retrieval call that opts into
    M3_PREFER_OBSERVATIONS=1.

Status: Phase D user-facing CLI. Pairs with bin/run_observer.py + bin/run_reflector.py.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
_MAIN_BIN = REPO_ROOT / "bin"
if str(_MAIN_BIN) not in sys.path:
    sys.path.insert(0, str(_MAIN_BIN))

import httpx  # noqa: E402

import memory_core as mc  # noqa: E402
from auth_utils import get_api_key  # noqa: E402
from slm_intent import (  # noqa: E402
    Profile,
    invalidate_cache as invalidate_profile_cache,
    load_profile,
    _parse_profile,
)

# Reuse Observer + Reflector implementations.
import run_observer as observer  # noqa: E402
import run_reflector as reflector  # noqa: E402

# Optional durable state machine (migration 028). Imported lazily-friendly:
# m3_enrich works without the tables present unless --track-state / --resume.
import enrichment_state as estate  # noqa: E402


# ── Defaults ────────────────────────────────────────────────────────────────
DEFAULT_PROFILE = os.environ.get("M3_ENRICH_PROFILE", "enrich_local_qwen")
# Chatlog DBs are message-shaped, so the message/conversation/chat_log
# default catches everything substantive. Core memory mostly lives under
# summary/note/decision/plan/knowledge/fact/preference — filtering it
# through the chatlog default leaves ~90% of rows unenriched. Resolve
# the right default per-DB via _resolve_default_types().
DEFAULT_TYPES = ("message", "conversation", "chat_log")  # chatlog default; also no-flag back-compat
DEFAULT_CHATLOG_TYPES = DEFAULT_TYPES
DEFAULT_CORE_TYPES = (
    "summary", "note", "decision", "plan",
    "knowledge", "fact", "preference",
    "message", "conversation",
)
ALWAYS_SKIP_TYPES = ("observation",)  # already enriched; idempotency
BACKUP_DIR = Path.home() / ".m3-memory" / "backups"


def _today() -> str:
    return datetime.utcnow().strftime("%Y%m%d")


def _resolve_db(arg_path: Optional[str], env_var: str, default_name: str) -> Optional[Path]:
    """Pick the DB to use for one role (core or chatlog).

    Order: explicit --core-db / --chatlog-db arg > env > default sibling.
    Returns None if no file exists at the resolved path (caller skips).
    """
    if arg_path:
        p = Path(arg_path).expanduser().resolve()
        return p if p.exists() else None
    env_val = os.environ.get(env_var)
    if env_val:
        p = Path(env_val).expanduser().resolve()
        return p if p.exists() else None
    p = REPO_ROOT / "memory" / default_name
    return p if p.exists() else None


def _load_profile_with_path(name: Optional[str], path: Optional[str]) -> Profile:
    """Load a profile by name (config/slm/<name>.yaml) OR explicit path.

    --profile-path wins if both are set.
    """
    if path:
        pth = Path(path).expanduser().resolve()
        if not pth.exists():
            sys.exit(f"ERROR: profile path not found: {pth}")
        # Reuse slm_intent's parser, which validates required keys.
        return _parse_profile(pth.stem, pth)
    invalidate_profile_cache()
    prof = load_profile(name or DEFAULT_PROFILE)
    if not prof:
        sys.exit(
            f"ERROR: profile {name!r} not found in config/slm/. "
            f"Use --profile-path /full/path.yaml, or copy "
            f"config/slm/enrich_custom_stub.yaml to make your own."
        )
    return prof


def _ensure_migration_025(db_path: Path) -> None:
    """Apply migration 025 (observation_queue, reflector_queue, obs index)
    if not already present. Best-effort — existing migrate_memory.py path
    may fail on chatlog DBs that have a different schema chain; we fall
    back to direct DDL in that case.

    Also lazy-creates `chroma_sync_queue` if it's missing — required for
    the embed=True write path. Chatlog DBs do not always carry this
    table because they were initialized via the chatlog migration chain
    (separate from main). Without it, every observation write fails."""
    import sqlite3
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        # Check what's already present.
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('observation_queue','reflector_queue','chroma_sync_queue')"
        ).fetchall()
        existing = {r[0] for r in rows}

        # Apply migration 025 if either queue is missing.
        if not {'observation_queue', 'reflector_queue'}.issubset(existing):
            up_path = REPO_ROOT / "memory" / "migrations" / "025_observation_queue.up.sql"
            if up_path.exists():
                conn.executescript(up_path.read_text(encoding="utf-8"))
                conn.commit()
                print(f"[m3-enrich] applied migration 025 to {db_path.name}", flush=True)

        # Create chroma_sync_queue if missing — required by memory_write_impl
        # when embed=True. The chatlog DB does not carry it by default.
        # Schema must match the canonical main-DB shape from
        # memory/migrations/001_initial_schema.sql so memory_sync.py can
        # read `attempts` for queue-health checks. Chatlog migration 003
        # aligns existing chatlog DBs that were created with the older
        # narrow shape.
        if 'chroma_sync_queue' not in existing:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS chroma_sync_queue (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id     TEXT NOT NULL,
                    operation     TEXT NOT NULL,
                    attempts      INTEGER DEFAULT 0,
                    stalled_since TEXT,
                    queued_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                );
                CREATE INDEX IF NOT EXISTS idx_chroma_sync_queue_memory_id
                  ON chroma_sync_queue(memory_id);
                CREATE INDEX IF NOT EXISTS idx_csq_attempts
                  ON chroma_sync_queue(attempts);
                CREATE INDEX IF NOT EXISTS idx_csq_queued_at
                  ON chroma_sync_queue(queued_at);
            """)
            conn.commit()
            print(f"[m3-enrich] added chroma_sync_queue to {db_path.name}", flush=True)
    finally:
        conn.close()


def _backup_db(db_path: Path) -> Path:
    """Copy db_path into BACKUP_DIR with a timestamp suffix. Returns
    the backup file path. Idempotent within the same minute (silently
    overwrites if the same minute-stamp already exists)."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M")
    dst = BACKUP_DIR / f"{db_path.stem}.pre-enrich.{stamp}.db"
    shutil.copy2(db_path, dst)
    return dst


async def _smoke_profile(profile: Profile) -> None:
    """Send one trivial request to verify the profile's endpoint + auth.
    Raises on failure with a clear message."""
    token = get_api_key(profile.api_key_service) or ""
    if not token and profile.api_key_service:
        sys.exit(
            f"ERROR: env var {profile.api_key_service!r} is empty. "
            f"Set it before running (e.g. `export {profile.api_key_service}=...`)."
        )
    # Use a trivially-empty turns block; expect {observations:[]} back.
    test_block = {"session_date": "2025-01-01", "turns": []}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            obs = await observer.call_observer(test_block, profile, client, token)
        # Empty list is a clean pass; non-empty also fine (model just made stuff up).
        print(f"[m3-enrich] profile smoke OK: model={profile.model} backend={profile.backend} "
              f"({len(obs)} obs returned on empty input)", flush=True)
    except Exception as e:
        sys.exit(
            f"ERROR: profile smoke failed.\n"
            f"  url: {profile.url}\n"
            f"  model: {profile.model}\n"
            f"  backend: {profile.backend}\n"
            f"  error: {e}\n"
            f"Check that your endpoint is reachable + auth is correct."
        )


def _load_conv_list(path: Path) -> set[str]:
    """Read a list of group_keys from FILE. Accepts either:
      • newline-delimited text (one group_key per line, blank lines + #-comments ignored)
      • a JSON array of strings

    Returns a deduplicated set; raises SystemExit on malformed input.
    """
    if not path.exists():
        sys.exit(f"ERROR: --source-conv-list path not found: {path}")
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        sys.exit(f"ERROR: --source-conv-list is empty: {path}")
    if raw.lstrip().startswith("["):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            sys.exit(f"ERROR: --source-conv-list JSON parse failed: {e}")
        if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
            sys.exit("ERROR: --source-conv-list JSON must be an array of strings.")
        return {x for x in data if x}
    out: set[str] = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line)
    if not out:
        sys.exit(f"ERROR: --source-conv-list contained no usable entries: {path}")
    return out


def _query_eligible_groups(
    db_path: Path,
    type_allowlist: tuple[str, ...],
    limit: Optional[int],
    source_variant: Optional[str] = None,
    conv_filter: Optional[set[str]] = None,
) -> list[tuple[str, str, list[tuple]]]:
    """Group eligible memory_items rows into (user_id, conversation_id, [turns]).

    Conversation grouping rule:
      1. row.conversation_id column if non-NULL
      2. else metadata_json.session_id
      3. else row.id (one-row group — Observer will treat as single turn)

    source_variant filter:
      None         → no filter (original behavior; pulls every variant)
      "__none__"   → variant IS NULL (true core memory only)
      "<name>"     → variant = '<name>' (single bench/test variant)

    Returns a list of (user_id, conv_id, turns_list) where each turns_list
    contains (id, content, role, turn_index, created_at, metadata_json) tuples
    sorted by turn_index ASC. Same shape run_observer.process_conversation expects.
    """
    import sqlite3
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    placeholders = ",".join("?" * len(type_allowlist))
    excl_placeholders = ",".join("?" * len(ALWAYS_SKIP_TYPES))
    variant_clause = ""
    variant_params: list = []
    if source_variant == "__none__":
        variant_clause = " AND variant IS NULL"
    elif source_variant:
        variant_clause = " AND variant = ?"
        variant_params = [source_variant]
    sql = f"""
        SELECT id,
               content,
               COALESCE(json_extract(metadata_json,'$.role'),
                        title,
                        'user') AS role,
               COALESCE(json_extract(metadata_json,'$.turn_index'), 0) AS turn_index,
               created_at,
               metadata_json,
               COALESCE(conversation_id,
                        json_extract(metadata_json,'$.session_id'),
                        id) AS group_key,
               COALESCE(user_id, '') AS user_id
        FROM memory_items
        WHERE COALESCE(is_deleted,0)=0
          AND type IN ({placeholders})
          AND type NOT IN ({excl_placeholders})
          {variant_clause}
        ORDER BY user_id, group_key, turn_index ASC, created_at ASC
    """
    params = list(type_allowlist) + list(ALWAYS_SKIP_TYPES) + variant_params
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    # Group ALL rows first, THEN apply --limit at the conversation-group
    # level (not the row level). This ensures --limit N gives N full
    # conversations, not N orphan turns scattered across N conversations.
    groups: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for r in rows:
        # row layout: id, content, role, turn_index, created_at, metadata_json, group_key, user_id
        groups[(r[7], r[6])].append((r[0], r[1], r[2], r[3], r[4], r[5]))

    out = [(uid, cid, turns) for (uid, cid), turns in groups.items()]
    if conv_filter is not None:
        out = [g for g in out if g[1] in conv_filter]
    # Sort by group size descending so --limit picks the BIGGEST conversations
    # first — most likely to contain extractable facts. Single-turn groups
    # (acks, status checks) sort to the bottom and only hit the cap last.
    out.sort(key=lambda g: -len(g[2]))
    if limit:
        out = out[:limit]
    return out


def _print_plan_body(plan: dict) -> None:
    """Shared plan summary used by both dry-run and real-run banners."""
    print(f"  Profile:             {plan['profile_name']}")
    print(f"  Model:               {plan['model']}")
    print(f"  Endpoint:            {plan['url']}")
    print(f"  Backend:             {plan['backend']}")
    print(f"  Target variant:      {plan['target_variant']}")
    src_label = plan.get('source_variant') or "(all)"
    if src_label == "__none__":
        src_label = "__none__ (variant IS NULL)"
    print(f"  Source variant:      {src_label}")
    print(f"  Type allowlist:      {plan['types']}")
    print()
    for db_label, db_info in plan["dbs"].items():
        print(f"  -- {db_label} " + "-" * 13)
        print(f"     path:          {db_info['path']}")
        print(f"     conversations: {db_info['n_groups']}")
        print(f"     turns total:   {db_info['n_turns']}")
        if db_info.get('cost_estimate'):
            print(f"     est cost:      {db_info['cost_estimate']}")
        if db_info.get('wall_estimate'):
            print(f"     est wall:      {db_info['wall_estimate']}")
        print()


def _print_dry_run(plan: dict) -> None:
    """Friendly summary of what would happen, without doing it."""
    bar = "=" * 62
    print()
    print(bar)
    print("  m3-enrich DRY RUN -- no writes will happen")
    print(bar)
    print()
    _print_plan_body(plan)
    print("To run for real, drop --dry-run.")
    print(bar)


def _print_run_summary(plan: dict) -> None:
    """Banner for an actual enrichment run (writes will happen)."""
    bar = "=" * 62
    print()
    print(bar)
    print("  m3-enrich RUN -- writing observations")
    print(bar)
    print()
    _print_plan_body(plan)
    print(bar)


def _estimate_cost_wall(profile: Profile, n_groups: int) -> tuple[Optional[str], Optional[str]]:
    """Rough cost + wall estimate based on the profile's known rate card."""
    # Per-call assumption: ~700 input + 400 output tokens.
    rates = {
        "claude-haiku-4-5":   (1.0,  5.0,  1.5),    # ($/M_in, $/M_out, sec/call)
        "claude-sonnet-4-6":  (3.0, 15.0,  2.0),
        "gpt-4o-mini":        (0.15, 0.60, 1.5),
        "gpt-4o":             (2.5, 10.0,  2.0),
        "gemini-2.5-flash":   (0.075, 0.30, 2.0),
        "gemini-2.5-pro":     (1.25, 5.0,  3.0),
    }
    rate = rates.get(profile.model)
    if rate is None:
        # Local — assume free, ~3s per call.
        return ("$0 (local)", f"~{n_groups * 3 / 60:.1f} min")
    in_rate, out_rate, sec = rate
    cost = n_groups * (700 * in_rate / 1_000_000 + 400 * out_rate / 1_000_000)
    wall = n_groups * sec / 60
    return (f"~${cost:.2f}", f"~{wall:.1f} min")


def _classify_observer_error(exc: BaseException) -> str:
    """Map an Observer exception to a stable error_class for the state table.

    Deterministic classes (json_decode/tokenizer/oversize/schema) skip retries;
    everything else gets exponential backoff. See estate.DETERMINISTIC_ERROR_CLASSES.
    """
    name = type(exc).__name__
    msg = str(exc)
    if "JSONDecode" in name or "json" in msg.lower() and "decode" in msg.lower():
        return "json_decode"
    if "tokenizer" in msg.lower() or "tokeniz" in msg.lower():
        return "tokenizer_error"
    if "too large" in msg.lower() or "context length" in msg.lower() or "max_tokens" in msg.lower():
        return "content_too_large"
    if "TimeoutException" in name or "ReadTimeout" in name or "timeout" in msg.lower():
        return "http_timeout"
    if "ConnectError" in name or "ConnectTimeout" in name:
        return "http_connect"
    if "HTTPStatusError" in name or "status_code" in msg.lower():
        return "http_status"
    return "other"


async def _run_db(
    db_path: Path,
    profile: Profile,
    target_variant: str,
    type_allowlist: tuple[str, ...],
    concurrency: int,
    limit: Optional[int],
    counters: dict,
    source_variant: Optional[str] = None,
    conv_filter: Optional[set[str]] = None,
    track_state: bool = False,
    resume: bool = False,
    enrich_run_id: Optional[str] = None,
    max_attempts: int = estate.DEFAULT_MAX_ATTEMPTS,
    include_dead_letter: bool = False,
    budget_usd: Optional[float] = None,
    sample: Optional[int] = None,
    sample_strategy: str = "first",
) -> Optional[str]:
    """Drive Observer over one DB. Apply migration, set M3_DATABASE so
    memory_core writes land here, then call run_observer.process_conversation
    for each grouped conversation.

    Returns an abort_reason string if budget tripped, else None.
    """
    import sqlite3
    import random as _random
    os.environ["M3_DATABASE"] = str(db_path)
    _ensure_migration_025(db_path)
    groups = _query_eligible_groups(
        db_path, type_allowlist, limit, source_variant, conv_filter,
    )

    # ── Sample (post-query, pre-state) ───────────────────────────────────
    # `--sample` is independent of `--limit`. limit caps the SQL pull
    # (cheap, deterministic); sample picks N from those groups via the
    # chosen strategy. Both can be combined.
    if sample and sample > 0 and len(groups) > sample:
        if sample_strategy == "random":
            groups = _random.sample(groups, sample)
        elif sample_strategy == "stratified":
            # Bucket by turn-count quartile; pull ~equal share from each.
            sorted_by_size = sorted(groups, key=lambda g: len(g[2]))
            n = len(sorted_by_size)
            buckets = [
                sorted_by_size[0 : n // 4],
                sorted_by_size[n // 4 : n // 2],
                sorted_by_size[n // 2 : 3 * n // 4],
                sorted_by_size[3 * n // 4 :],
            ]
            per = max(1, sample // 4)
            picked: list = []
            for b in buckets:
                if b:
                    picked.extend(_random.sample(b, min(per, len(b))))
            # Top up to exactly `sample` if integer division left us short.
            if len(picked) < sample:
                rest = [g for g in groups if g not in picked]
                short = sample - len(picked)
                if rest:
                    picked.extend(_random.sample(rest, min(short, len(rest))))
            groups = picked[:sample]
        else:  # "first"
            # _query_eligible_groups already sorts by size desc — so 'first'
            # = N largest groups.
            groups = groups[:sample]
        print(f"[m3-enrich] --sample {sample} ({sample_strategy}): "
              f"{len(groups)} groups selected", flush=True)

    # ── State-tracking wiring (opt-in) ──────────────────────────────────
    # When --track-state is set we open a writer connection on db_path,
    # verify migration 028 has been applied (caller's job to migrate up),
    # recover stale claims, enroll all eligible groups, and (if --resume)
    # narrow `groups` to only those still pending/failed-with-retries.
    state_conn: Optional[sqlite3.Connection] = None
    group_id_by_key: dict[tuple[str, str], int] = {}
    abort_reason: Optional[str] = None

    if track_state:
        if not source_variant:
            print("[m3-enrich] --track-state requires --source-variant; skipping state.", flush=True)
            track_state = False
        else:
            state_conn = sqlite3.connect(str(db_path), timeout=30.0)
            state_conn.execute("PRAGMA journal_mode=WAL")
            if not estate.has_state_tables(state_conn):
                state_conn.close()
                sys.exit(
                    "ERROR: --track-state requires migration 028 applied to "
                    f"{db_path}. Run: python bin/migrate_memory.py --db "
                    f"{db_path} up"
                )
            n_recovered = estate.recover_stale_claims(state_conn)
            if n_recovered:
                print(f"[m3-enrich] recovered {n_recovered} stale claim(s)", flush=True)
            # Enroll every eligible group at status='pending' (idempotent;
            # content-hash drift triggers a supersede).
            enroll_input = []
            for uid, cid, turns in groups:
                enroll_input.append({
                    "group_key": cid,
                    "user_id": uid,
                    "turn_count": len(turns),
                    "source_content_hash": estate.compute_source_content_hash(turns),
                })
            actions = estate.enroll_groups_bulk(
                state_conn, enroll_input,
                source_variant=source_variant,
                target_variant=target_variant,
                db_path=str(db_path),
                profile=profile.name,
                model=profile.model,
                enrich_run_id=enrich_run_id,
            )
            print(f"[m3-enrich] enrolled groups: {actions}", flush=True)
            # Build (uid, cid) → id map for the claim path.
            placeholders = ",".join("?" * len(groups))
            cur = state_conn.execute(
                f"""SELECT id, user_id, group_key FROM enrichment_groups
                    WHERE source_variant=? AND target_variant=?
                      AND group_key IN ({placeholders})""",
                [source_variant, target_variant] + [g[1] for g in groups],
            )
            for gid, uid, gkey in cur.fetchall():
                group_id_by_key[(uid, gkey)] = gid

            if resume:
                eligible = estate.eligible_for_resume(
                    state_conn,
                    source_variant=source_variant,
                    target_variant=target_variant,
                    max_attempts=max_attempts,
                    include_dead_letter=include_dead_letter,
                )
                eligible_keys = {(uid, gkey) for _gid, gkey, uid in eligible}
                before = len(groups)
                groups = [(u, c, t) for (u, c, t) in groups if (u, c) in eligible_keys]
                print(
                    f"[m3-enrich] --resume: {len(groups)}/{before} groups pending "
                    f"(skipped {before - len(groups)} already-done/dead-letter)",
                    flush=True,
                )

    n_groups = len(groups)
    print(f"[m3-enrich] {db_path.name}: {n_groups} conversations to process", flush=True)
    if n_groups == 0:
        if state_conn is not None:
            state_conn.close()
        return None

    token = get_api_key(profile.api_key_service) or ""
    sem = asyncio.Semaphore(concurrency)
    started = time.monotonic()

    # Budget watchdog: every K groups we re-sum cost on the run; abort cleanly.
    budget_check_interval = 25

    async with httpx.AsyncClient() as client:
        async def gated(uid: str, cid: str, turns: list[tuple]) -> None:
            nonlocal abort_reason
            if abort_reason is not None:
                return
            async with sem:
                if abort_reason is not None:
                    return
                pre_processed = counters["processed"]
                pre_written = counters["written"]
                pre_empty = counters["empty_groups"]
                pre_failed = counters["failed"]

                claim_token: Optional[str] = None
                gid: Optional[int] = None
                if track_state and state_conn is not None:
                    gid = group_id_by_key.get((uid, cid))
                    if gid is not None:
                        claim_token = estate.claim_group(
                            state_conn, gid, enrich_run_id=enrich_run_id or "",
                        )
                        if claim_token is None:
                            # Raced or already terminal — skip silently.
                            return

                t0 = time.monotonic()
                try:
                    await observer.process_conversation(
                        cid, uid, turns, target_variant,
                        profile, client, token, counters,
                        source_group_id=gid,
                    )
                except BaseException as exc:  # noqa: BLE001
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    if track_state and gid is not None and state_conn is not None:
                        ec = _classify_observer_error(exc)
                        new_status = estate.mark_failed(
                            state_conn, gid,
                            error_class=ec, last_error=repr(exc),
                            max_attempts=max_attempts, enrichment_ms=elapsed_ms,
                        )
                        if new_status == "dead_letter":
                            print(f"[m3-enrich] DEAD_LETTER conv={cid[:8]} class={ec}", flush=True)
                    raise

                elapsed_ms = int((time.monotonic() - t0) * 1000)
                if track_state and gid is not None and state_conn is not None:
                    written_delta = counters["written"] - pre_written
                    failed_delta = counters["failed"] - pre_failed
                    empty_delta = counters["empty_groups"] - pre_empty
                    if failed_delta > 0 and written_delta == 0:
                        # Some chunks failed and nothing was written — count as failed.
                        estate.mark_failed(
                            state_conn, gid,
                            error_class="other", last_error="chunk(s) failed",
                            max_attempts=max_attempts, enrichment_ms=elapsed_ms,
                        )
                    elif written_delta > 0:
                        estate.mark_success(
                            state_conn, gid,
                            obs_emitted=written_delta, enrichment_ms=elapsed_ms,
                        )
                    elif empty_delta > 0 or counters["processed"] - pre_processed > 0:
                        estate.mark_empty(state_conn, gid, enrichment_ms=elapsed_ms)
                    # else: process_conversation early-returned (empty turns)
                    #       — leave row as-is; claim already updated attempts.

                done = counters["processed"] + counters["empty_groups"] + counters["failed"]
                if done > 0 and done % 25 == 0:
                    elapsed = time.monotonic() - started
                    rate = done / max(elapsed, 1e-3)
                    eta = (n_groups - done) / max(rate, 1e-3)
                    print(
                        f"[m3-enrich] {db_path.name}: {done}/{n_groups}  "
                        f"obs_written={counters['written']} "
                        f"empty={counters['empty_groups']} "
                        f"failed={counters['failed']}  "
                        f"rate={rate:.2f}/s eta={eta/60:.1f}m",
                        flush=True,
                    )

                # Budget watchdog (post-call). Cheap query — runs every N groups.
                if (
                    budget_usd is not None
                    and track_state and state_conn is not None
                    and enrich_run_id
                    and done > 0 and done % budget_check_interval == 0
                ):
                    spent = estate.run_total_cost_usd(state_conn, enrich_run_id)
                    if spent >= budget_usd:
                        abort_reason = "budget_exceeded"
                        print(
                            f"[m3-enrich] BUDGET TRIPPED: spent ${spent:.2f} "
                            f">= ${budget_usd:.2f}; draining inflight then stopping.",
                            flush=True,
                        )

        await asyncio.gather(*(
            gated(uid, cid, turns) for uid, cid, turns in groups
        ), return_exceptions=True)

    if state_conn is not None:
        state_conn.close()
    return abort_reason


async def _run_reflector_pass(
    db_path: Path,
    profile: Profile,
    threshold: int,
    concurrency: int,
) -> dict:
    """Run Reflector on every (user_id, conversation_id) pair whose
    observation count meets `threshold`. Returns a counters dict."""
    import sqlite3
    os.environ["M3_DATABASE"] = str(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = conn.execute("""
        SELECT COALESCE(user_id,'') AS uid,
               json_extract(metadata_json,'$.conversation_id') AS cid,
               COUNT(*) AS n
        FROM memory_items
        WHERE type='observation' AND COALESCE(is_deleted,0)=0
        GROUP BY uid, cid
        HAVING n >= ?
    """, (threshold,)).fetchall()
    conn.close()
    counters = {
        "processed": 0, "sup_emitted": 0, "sup_written": 0,
        "failed": 0, "empty_groups": 0,
    }
    if not rows:
        print(f"[m3-enrich] reflector: no groups exceed threshold={threshold}", flush=True)
        return counters

    print(f"[m3-enrich] reflector: {len(rows)} groups eligible (threshold={threshold})",
          flush=True)
    token = get_api_key(profile.api_key_service) or ""
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient() as client:
        async def gated(uid: str, cid: str) -> None:
            async with sem:
                await reflector.reflect_conversation(uid, cid, profile, client, token, counters)
        await asyncio.gather(*(gated(r[0], r[1]) for r in rows if r[1]))
    return counters


def _resolve_default_types(args) -> tuple[str, ...]:
    """Pick the default type allowlist based on which DB(s) are scoped.

    --core (core_only) → DEFAULT_CORE_TYPES (broad: summary/note/decision/...).
    --chatlog (chatlog_only) → DEFAULT_CHATLOG_TYPES (message/conversation/chat_log).
    Both flags set is rejected by argparse-level guard, so not handled here.
    Neither flag → DEFAULT_TYPES (back-compat with pre-refactor invocations
    that ran both DBs through the chatlog-shaped default).
    """
    if getattr(args, "core_only", False):
        return DEFAULT_CORE_TYPES
    if getattr(args, "chatlog_only", False):
        return DEFAULT_CHATLOG_TYPES
    return DEFAULT_TYPES


def _build_type_allowlist(args) -> tuple[str, ...]:
    """Build the final type allowlist from defaults + opt-in flags.

    Resolution order:
      1. If --only-use-types is set, that CSV REPLACES the default entirely
         (the escape hatch for power users who want a precise, narrow list).
      2. Otherwise start from the per-DB default (--core vs --chatlog).
      3. --include-summaries, --include-notes, and --include-types all
         EXTEND the active list (additive). Names match their semantics.
    """
    if getattr(args, "only_use_types", None):
        types: list[str] = []
        for t in args.only_use_types.split(","):
            t = t.strip()
            if t and t not in types and t not in ALWAYS_SKIP_TYPES:
                types.append(t)
    else:
        types = list(_resolve_default_types(args))
    if args.include_summaries and "summary" not in types:
        types.append("summary")
    if args.include_notes and "note" not in types:
        types.append("note")
    if args.include_types:
        for t in args.include_types.split(","):
            t = t.strip()
            if t and t not in types and t not in ALWAYS_SKIP_TYPES:
                types.append(t)
    return tuple(types)


async def _drain_queue_mode(args, profile, token: str) -> int:
    """Phase E2: drain mode — pop pending observation_queue rows, enrich them.

    Iterates through both core + chatlog DBs (or whichever the user scoped
    via --core / --chatlog), running the existing run_observer.drain_queue_mode
    against each. Returns 0 on clean drain.
    """
    db_targets: list[tuple[str, Path]] = []
    if not args.chatlog_only:
        core_db = _resolve_db(args.core_db, "M3_DATABASE", "agent_memory.db")
        if core_db:
            db_targets.append(("core", core_db))
    if not args.core_only:
        chatlog_db = _resolve_db(args.chatlog_db, "M3_CHATLOG_DATABASE", "agent_chatlog.db")
        if chatlog_db:
            db_targets.append(("chatlog", chatlog_db))
    if not db_targets:
        sys.exit("ERROR: no DBs found to drain.")

    print(f"[m3-enrich] drain-queue mode  profile={profile.name}  "
          f"batch={args.drain_batch}", flush=True)

    # Build a tiny argparse.Namespace for run_observer.drain_queue_mode —
    # it expects target_variant + concurrency + batch on the args object.
    import argparse as _argparse
    drainer_args = _argparse.Namespace(
        target_variant=args.target_variant,
        concurrency=args.concurrency,
        batch=args.drain_batch,
    )

    grand = {"processed": 0, "written": 0, "failed": 0, "empty_groups": 0}
    for label, db_path in db_targets:
        os.environ["M3_DATABASE"] = str(db_path)
        # Ensure migration 025 + chroma_sync_queue exist (cheap idempotent check).
        _ensure_migration_025(db_path)
        # Count pending rows up-front so we can show what we're about to do.
        import sqlite3
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            try:
                pending = conn.execute(
                    "SELECT COUNT(*) FROM observation_queue WHERE attempts < 5"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                pending = 0
        if pending == 0:
            print(f"[m3-enrich] {db_path.name}: queue empty -- skipping", flush=True)
            continue
        print(f"[m3-enrich] {db_path.name}: {pending} pending rows", flush=True)
        await observer.drain_queue_mode(drainer_args, profile, token)
        # observer.drain_queue_mode prints its own summary; we don't re-aggregate
        # here because it doesn't return counters — Phase E3 future work if
        # needed for tests.

    print()
    print("=" * 62)
    print("  m3-enrich --drain-queue COMPLETE")
    print("=" * 62)
    return 0


async def _main_async(args) -> int:
    profile = _load_profile_with_path(args.profile, args.profile_path)
    token = get_api_key(profile.api_key_service) or ""
    if not token and profile.api_key_service:
        sys.exit(
            f"ERROR: env var {profile.api_key_service!r} is empty. "
            f"Set it before running."
        )

    # Phase E2: drain-queue mode dispatches early — doesn't need profile smoke
    # or backups since it only enriches queue rows that were validated at
    # enqueue time.
    if args.drain_queue:
        return await _drain_queue_mode(args, profile, token)

    reflector_profile = (
        _load_profile_with_path(args.reflector_profile, None)
        if args.reflector_profile and args.reflector_profile != args.profile
        else profile
    )
    type_allowlist = _build_type_allowlist(args)

    # Optional --source-conv-list narrowing: load once, apply to every DB.
    # Keeps existing default behavior (no filter) when the flag is unset.
    conv_filter: Optional[set[str]] = None
    if getattr(args, "source_conv_list", None):
        conv_filter = _load_conv_list(Path(args.source_conv_list).expanduser().resolve())
        print(f"[m3-enrich] --source-conv-list: {len(conv_filter)} group_keys "
              f"loaded from {args.source_conv_list}", flush=True)

    # Implication: --resume / --include-dead-letter / --budget-usd all require
    # the state machine. Auto-enable --track-state rather than error out, so
    # users don't have to remember the dependency.
    if (args.resume or args.include_dead_letter or args.budget_usd is not None) \
            and not args.track_state:
        args.track_state = True
    if args.include_dead_letter:
        args.resume = True
    if args.track_state and not args.source_variant:
        sys.exit(
            "ERROR: --track-state / --resume / --budget-usd require --source-variant "
            "(state rows are keyed by (source_variant, target_variant, group_key)).")

    # Pick which DBs to enrich.
    db_targets: list[tuple[str, Path]] = []
    if not args.chatlog_only:
        core_db = _resolve_db(args.core_db, "M3_DATABASE",
                              "agent_memory.db")
        if core_db:
            db_targets.append(("core", core_db))
    if not args.core_only:
        chatlog_db = _resolve_db(args.chatlog_db, "M3_CHATLOG_DATABASE",
                                 "agent_chatlog.db")
        if chatlog_db:
            db_targets.append(("chatlog", chatlog_db))
    if not db_targets:
        sys.exit("ERROR: no DBs found to enrich. "
                 "Set M3_DATABASE / M3_CHATLOG_DATABASE or use --core-db/--chatlog-db.")

    # Build dry-run plan first (cheap; useful as both preview AND the
    # source of truth for cost/wall estimates we print at the end).
    plan = {
        "profile_name": profile.name,
        "model": profile.model,
        "url": profile.url,
        "backend": profile.backend,
        "target_variant": args.target_variant,
        "source_variant": args.source_variant,
        "types": list(type_allowlist),
        "dbs": {},
    }
    for label, db_path in db_targets:
        groups = _query_eligible_groups(
            db_path, type_allowlist, args.limit, args.source_variant, conv_filter,
        )
        n_turns = sum(len(g[2]) for g in groups)
        cost, wall = _estimate_cost_wall(profile, len(groups))
        plan["dbs"][label] = {
            "path": str(db_path),
            "n_groups": len(groups),
            "n_turns": n_turns,
            "cost_estimate": cost,
            "wall_estimate": wall,
        }

    if args.dry_run:
        _print_dry_run(plan)
        return 0

    _print_run_summary(plan)
    print()
    if not args.yes:
        try:
            ans = input("Proceed? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("aborted (no changes made)")
            return 0

    # Pre-flight: smoke profile, backup each DB.
    if not args.skip_preflight:
        await _smoke_profile(profile)
        for label, db_path in db_targets:
            backup = _backup_db(db_path)
            print(f"[m3-enrich] backup: {db_path.name} -> {backup}", flush=True)

    # Observer pass per DB.
    counters_total = {"processed": 0, "written": 0, "failed": 0, "empty_groups": 0}
    abort_reasons: dict[str, str] = {}
    for label, db_path in db_targets:
        counters = {"processed": 0, "written": 0, "failed": 0, "empty_groups": 0}

        # Open a state run record per DB if state-tracking is on. Distinct
        # run_id per DB simplifies aggregation and budget reporting.
        per_db_run_id: Optional[str] = None
        if args.track_state:
            import sqlite3 as _sqlite3
            _sc = _sqlite3.connect(str(db_path), timeout=30.0)
            _sc.execute("PRAGMA journal_mode=WAL")
            if not estate.has_state_tables(_sc):
                _sc.close()
                sys.exit(
                    f"ERROR: --track-state requires migration 028 applied to "
                    f"{db_path}. Run: python bin/migrate_memory.py --db "
                    f"{db_path} up"
                )
            per_db_run_id = estate.start_run(
                _sc,
                profile=profile.name, model=profile.model,
                source_variant=args.source_variant,
                target_variant=args.target_variant,
                db_path=str(db_path),
                concurrency=args.concurrency,
                launch_argv=sys.argv,
            )
            _sc.close()
            print(f"[m3-enrich] {label} run_id={per_db_run_id}", flush=True)

        abort_reason = await _run_db(
            db_path, profile, args.target_variant, type_allowlist,
            args.concurrency, args.limit, counters,
            source_variant=args.source_variant,
            conv_filter=conv_filter,
            track_state=args.track_state,
            resume=args.resume,
            enrich_run_id=per_db_run_id,
            max_attempts=args.max_attempts,
            include_dead_letter=args.include_dead_letter,
            budget_usd=args.budget_usd,
            sample=args.sample,
            sample_strategy=args.sample_strategy,
        )
        if abort_reason:
            abort_reasons[label] = abort_reason

        # Close out the run record with final counts.
        if args.track_state and per_db_run_id:
            import sqlite3 as _sqlite3
            _sc = _sqlite3.connect(str(db_path), timeout=30.0)
            run_status = "aborted" if abort_reason else "completed"
            estate.end_run(
                _sc, per_db_run_id,
                status=run_status, abort_reason=abort_reason,
            )
            _sc.close()

        for k in counters_total:
            counters_total[k] += counters[k]
        print(f"[m3-enrich] {label} done: "
              f"{counters['processed']} groups processed, "
              f"{counters['written']} observations written, "
              f"{counters['empty_groups']} empty, "
              f"{counters['failed']} failed"
              + (f" [ABORTED: {abort_reason}]" if abort_reason else ""),
              flush=True)
        if abort_reason:
            # Skip the reflector pass + remaining DBs when budget tripped.
            break

    # Optional Reflector pass.
    if not args.no_reflect:
        for label, db_path in db_targets:
            r_counters = await _run_reflector_pass(
                db_path, reflector_profile, args.reflector_threshold, args.concurrency,
            )
            print(f"[m3-enrich] reflector {label}: "
                  f"{r_counters['processed']} groups, "
                  f"{r_counters['sup_written']} supersedes edges written", flush=True)

    print()
    print("=" * 62)
    print("  m3-enrich COMPLETE")
    print("=" * 62)
    print(f"  observations written: {counters_total['written']}")
    print(f"  conversations processed: {counters_total['processed']}")
    print(f"  empty (no extractable user-facts): {counters_total['empty_groups']}")
    print(f"  failed: {counters_total['failed']}")
    print()
    print(f"  retrieve later via:")
    print(f"    M3_PREFER_OBSERVATIONS=1 mcp__memory__memory_search ...")
    print(f"  (or pass --observer-variant {args.target_variant} to the bench harness)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="m3_enrich -- build observation memories from your core/chatlog DBs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python bin/m3_enrich.py --dry-run
  python bin/m3_enrich.py
  python bin/m3_enrich.py --profile enrich_anthropic_haiku
  python bin/m3_enrich.py --profile-path ~/my-profile.yaml --core
  python bin/m3_enrich.py --include-summaries --include-types decision,plan

Profile picker:
  enrich_local_qwen        $0,    LM Studio + qwen3-8b   (default)
  enrich_local_gemma       $0,    LM Studio + gemma-4-coder (faster, less synthesis)
  enrich_anthropic_haiku   $$,    Anthropic Claude Haiku 4.5
  enrich_google_gemini     $,     Google Gemini 2.5 Flash (cheapest cloud)
  enrich_openai_gpt        $,     OpenAI gpt-4o-mini
  enrich_custom_stub       —      Template; copy + edit, use --profile-path
""",
    )
    ap.add_argument("--profile", default=DEFAULT_PROFILE,
                    help=f"Profile name in config/slm/. Default: {DEFAULT_PROFILE}.")
    ap.add_argument("--profile-path", default=None,
                    help="Explicit YAML path. Overrides --profile when set.")
    ap.add_argument("--reflector-profile", default=None,
                    help="Override the Reflector stage with a different profile. "
                         "Defaults to --profile (same model for both stages).")
    ap.add_argument("--core", action="store_true", dest="core_only",
                    help="Only enrich the core memory DB (skip chatlog). "
                         "Auto-broadens default type allowlist to: "
                         f"{','.join(DEFAULT_CORE_TYPES)}.")
    ap.add_argument("--chatlog", action="store_true", dest="chatlog_only",
                    help="Only enrich the chatlog DB (skip core). "
                         "Default type allowlist stays message-shaped: "
                         f"{','.join(DEFAULT_CHATLOG_TYPES)}.")
    ap.add_argument("--core-db", default=None,
                    help="Explicit path to the core memory DB.")
    ap.add_argument("--chatlog-db", default=None,
                    help="Explicit path to the chatlog DB.")
    ap.add_argument("--target-variant", default=f"m3-observations-{_today()}",
                    help="Variant tag for emitted observations. Default: m3-observations-YYYYMMDD.")
    ap.add_argument("--source-variant", default=None,
                    help="Filter source rows by variant. '__none__' = true core memory only "
                         "(variant IS NULL). A name string = single-variant scope. "
                         "Default: no filter (all rows).")
    ap.add_argument("--source-conv-list",
                    default=os.environ.get("M3_ENRICH_CONV_LIST"),
                    help="Path to a file listing group_keys (conversation_ids) to "
                         "process. Format: newline-delimited text (with optional "
                         "# comments) OR a JSON array of strings. Narrows the "
                         "eligible-groups set AFTER --source-variant + type "
                         "filtering — opt-in lever, no effect on default "
                         "behavior. Env: M3_ENRICH_CONV_LIST.")
    ap.add_argument("--track-state", action="store_true",
                    default=os.environ.get("M3_ENRICH_TRACK_STATE", "0").lower() in ("1", "true", "yes"),
                    help="Record per-group enrichment state in the enrichment_groups "
                         "table (migration 028). Required for --resume / --budget-usd. "
                         "Requires --source-variant. Env: M3_ENRICH_TRACK_STATE.")
    ap.add_argument("--resume", action="store_true",
                    help="Skip groups already at status='success' or 'empty' for the "
                         "current (source_variant, target_variant) pair. Implies "
                         "--track-state. Picks up pending + failed-with-retries-left.")
    ap.add_argument("--include-dead-letter", action="store_true",
                    help="Also retry groups currently at status='dead_letter'. Manual "
                         "override; implies --resume. Use after fixing the underlying "
                         "issue (prompt change, model upgrade, etc.).")
    ap.add_argument("--max-attempts", type=int,
                    default=int(os.environ.get("M3_ENRICH_MAX_ATTEMPTS",
                                               estate.DEFAULT_MAX_ATTEMPTS)),
                    help=f"Per-group retry cap before promotion to dead_letter. "
                         f"Default {estate.DEFAULT_MAX_ATTEMPTS}. "
                         f"Env: M3_ENRICH_MAX_ATTEMPTS.")
    ap.add_argument("--budget-usd", type=float,
                    default=(float(os.environ["M3_ENRICH_BUDGET_USD"])
                             if os.environ.get("M3_ENRICH_BUDGET_USD") else None),
                    help="Hard ceiling on cumulative cost_usd across this run. When "
                         "tripped, drains inflight calls and exits cleanly with "
                         "status='aborted'. Implies --track-state. "
                         "Env: M3_ENRICH_BUDGET_USD.")
    ap.add_argument("--sample", type=int, default=None,
                    help="Process at most N groups, selected via --sample-strategy. "
                         "Independent of --limit (which caps the SQL pull).")
    ap.add_argument("--sample-strategy", default="first",
                    choices=("first", "random", "stratified"),
                    help="How --sample picks groups. 'first' = top-N by turn-count "
                         "desc (cheapest). 'random' = uniform random. 'stratified' = "
                         "balanced by turn-count quartile. Default 'first'.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap conversations enriched per DB (smoke testing).")
    ap.add_argument("--concurrency", type=int, default=4,
                    help="Concurrent SLM calls. Default 4.")
    ap.add_argument("--include-summaries", action="store_true",
                    help="Add type='summary' rows to the active allowlist "
                         "(extends whichever default applies; redundant under --core).")
    ap.add_argument("--include-notes", action="store_true",
                    help="Add type='note' rows to the active allowlist "
                         "(extends whichever default applies; redundant under --core).")
    ap.add_argument("--include-types", default=None,
                    help="Comma-separated types to ADD to the active allowlist "
                         "(extends whichever default applies). E.g. "
                         "'--include-types reference,project' adds those alongside "
                         "the per-DB default.")
    ap.add_argument("--only-use-types", default=None,
                    help="Comma-separated types -- REPLACES the default allowlist "
                         "entirely (e.g. '--only-use-types decision,plan' selects "
                         "ONLY those, no defaults merged in). Use this when you "
                         "want a precise narrow list. --include-summaries / "
                         "--include-notes / --include-types still extend after "
                         "replacement.")
    ap.add_argument("--drain-queue", action="store_true",
                    help="Phase E2: drain pending observation_queue rows that "
                         "were enqueued by the chatlog auto-enrich hook "
                         "(M3_AUTO_ENRICH=1). Single-shot, returns when the "
                         "queue is empty. Use in cron / scheduled task for "
                         "continuous enrichment.")
    ap.add_argument("--drain-batch", type=int, default=100,
                    help="Max queue rows to process per --drain-queue invocation. "
                         "Default 100 (a few minutes of work for typical convs).")
    ap.add_argument("--no-reflect", action="store_true",
                    help="Skip the Reflector merge/supersede pass.")
    ap.add_argument("--reflector-threshold", type=int, default=50,
                    help="Min observations per (user,conv) before Reflector fires. Default 50.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Preview what would happen without writing.")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="Skip endpoint-smoke and DB backup. Power-user only.")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="Skip the interactive confirm prompt.")
    args = ap.parse_args()
    if args.core_only and args.chatlog_only:
        sys.exit("ERROR: --core and --chatlog are mutually exclusive (omit both for default behavior).")
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
