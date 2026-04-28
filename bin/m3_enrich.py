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


def _query_eligible_groups(
    db_path: Path,
    type_allowlist: tuple[str, ...],
    limit: Optional[int],
    source_variant: Optional[str] = None,
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
    # Sort by group size descending so --limit picks the BIGGEST conversations
    # first — most likely to contain extractable facts. Single-turn groups
    # (acks, status checks) sort to the bottom and only hit the cap last.
    out.sort(key=lambda g: -len(g[2]))
    if limit:
        out = out[:limit]
    return out


def _print_dry_run(plan: dict) -> None:
    """Print a friendly summary of what would happen, without doing it."""
    bar = "=" * 62
    print()
    print(bar)
    print("  m3-enrich DRY RUN -- no writes will happen")
    print(bar)
    print()
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
    print("To run for real, drop --dry-run.")
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


async def _run_db(
    db_path: Path,
    profile: Profile,
    target_variant: str,
    type_allowlist: tuple[str, ...],
    concurrency: int,
    limit: Optional[int],
    counters: dict,
    source_variant: Optional[str] = None,
) -> None:
    """Drive Observer over one DB. Apply migration, set M3_DATABASE so
    memory_core writes land here, then call run_observer.process_conversation
    for each grouped conversation."""
    os.environ["M3_DATABASE"] = str(db_path)
    _ensure_migration_025(db_path)
    groups = _query_eligible_groups(db_path, type_allowlist, limit, source_variant)
    n_groups = len(groups)
    print(f"[m3-enrich] {db_path.name}: {n_groups} eligible conversations", flush=True)
    if n_groups == 0:
        return

    token = get_api_key(profile.api_key_service) or ""
    sem = asyncio.Semaphore(concurrency)
    started = time.monotonic()

    async with httpx.AsyncClient() as client:
        async def gated(uid: str, cid: str, turns: list[tuple]) -> None:
            async with sem:
                # process_conversation expects the same tuple shape as our
                # _query_eligible_groups returns.
                await observer.process_conversation(
                    cid, uid, turns, target_variant,
                    profile, client, token, counters,
                )
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

        await asyncio.gather(*(
            gated(uid, cid, turns) for uid, cid, turns in groups
        ))


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
        groups = _query_eligible_groups(db_path, type_allowlist, args.limit, args.source_variant)
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

    _print_dry_run(plan)
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
    for label, db_path in db_targets:
        counters = {"processed": 0, "written": 0, "failed": 0, "empty_groups": 0}
        await _run_db(
            db_path, profile, args.target_variant, type_allowlist,
            args.concurrency, args.limit, counters,
            source_variant=args.source_variant,
        )
        for k in counters_total:
            counters_total[k] += counters[k]
        print(f"[m3-enrich] {label} done: "
              f"{counters['processed']} groups processed, "
              f"{counters['written']} observations written, "
              f"{counters['empty_groups']} empty, "
              f"{counters['failed']} failed", flush=True)

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
                         "(variant IS NULL). A name string = single-variant scope (e.g. "
                         "lme-strat60b-v3). Default: no filter (all rows).")
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
