#!/usr/bin/env python3
"""
m3_cognitive_loop — The autonomous heartbeat of m3-memory.

This script unifies the Observer, Reflector, and Entity Extractor into a
single continuous "live" pipeline. It monitors the core memory and chatlog
DBs for new content and automatically performs:
  1. Entity Extraction (Linking facts into the knowledge graph)
  2. Observation Extraction (Extracting high-signal user-facts/preferences)
  3. Reflection (Merging/superseding facts, resolving contradictions)
  4. Temporal Resolution (Normalizing relative dates like 'yesterday')

Usage:
  python bin/m3_cognitive_loop.py --interval 60  # Run every 60 seconds

When M3_AUTO_ENRICH is ON, this replaces the need for separate cron jobs
for m3_enrich and m3_entities.
"""

import argparse
import asyncio
import atexit
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
_BIN = REPO_ROOT / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import chatlog_config
import m3_enrich
import m3_entities
from m3_sdk import M3Context, ensure_governor_config, get_governor_pacing, resolve_db_path

# PID file path for single-instance locking
PID_FILE = REPO_ROOT / "memory" / "cognitive_loop.pid"

def daemonize_windows(args):
    """Restart this process using pythonw.exe to detach from console."""
    # Build the same argument list but remove --background
    argv = [sys.executable.replace("python.exe", "pythonw.exe")]
    argv.append(os.path.abspath(__file__))
    for arg in sys.argv[1:]:
        if arg != "--background":
            argv.append(arg)

    # Spawn the detached child, then HARD-exit the parent. os._exit(0) — not
    # sys.exit(0) — because sys.exit raises SystemExit, which an outer try/except
    # (or the asyncio runner if we got here late) can swallow, leaving the parent
    # ALIVE alongside the child. Two live loops each dispatch their own
    # Semaphore(2) of SLM calls = over-dispatch that storms the local LLM.
    subprocess.Popen(argv, creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS)
    print("Cognitive Loop started in background (Windows pythonw).")
    sys.stdout.flush()
    os._exit(0)

def daemonize_unix():
    """Double-fork to detach from the terminal."""
    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0) # Exit first parent
    except OSError as e:
        sys.exit(f"fork #1 failed: {e}")

    os.setsid()
    os.umask(0)

    try:
        pid = os.fork()
        if pid > 0:
            print(f"Cognitive Loop started in background (PID {pid}).")
            sys.exit(0) # Exit second parent
    except OSError as e:
        sys.exit(f"fork #2 failed: {e}")

    # Redirect standard file descriptors
    sys.stdout.flush()
    sys.stderr.flush()
    with open(os.devnull, "r") as f:
        os.dup2(f.fileno(), sys.stdin.fileno())
    with open(os.devnull, "a") as f:
        os.dup2(f.fileno(), sys.stdout.fileno())
        os.dup2(f.fileno(), sys.stderr.fileno())

def acquire_lock():
    """Ensure only one instance of the loop is running."""
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            # Check if process is still alive
            if sys.platform == "win32":
                import ctypes
                PROCESS_QUERY_INFORMATION = 0x0400
                handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, old_pid)
                if handle:
                    ctypes.windll.kernel32.CloseHandle(handle)
                    # Use print here as logger might not be fully ready
                    print(f"ERROR: Cognitive Loop is already running (PID {old_pid}). Exiting.")
                    sys.exit(0)
            else:
                os.kill(old_pid, 0)
                print(f"ERROR: Cognitive Loop is already running (PID {old_pid}). Exiting.")
                sys.exit(0)
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            # Stale PID file or can't check
            pass

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    atexit.register(release_lock)

def release_lock():
    """Remove the PID file on exit."""
    if PID_FILE.exists():
        try:
            current_pid = int(PID_FILE.read_text().strip())
            if current_pid == os.getpid():
                PID_FILE.unlink()
        except Exception:
            pass

# Configure logging for structured and greppable output
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("m3_cognitive_loop")

# When the governor reports THROTTLED (host/GPU load high but below the HALT
# line), each pass processes only this many items so the loop returns to the
# top and re-probes load after each LLM call instead of charging through a full
# batch. Default 1 = send a single item, then re-check load before the next —
# the most conservative, interactive-first cadence. Tune via
# M3_GOVERNOR_THROTTLED_LIMIT.
_THROTTLED_LIMIT = max(1, int(os.environ.get("M3_GOVERNOR_THROTTLED_LIMIT", "1")))


def _is_local_llm_url(url: Optional[str]) -> bool:
    """True if an SLM/LLM endpoint runs on THIS machine (loopback) — i.e. its
    work competes for the local GPU/CPU. Cloud/frontier endpoints (api.anthropic.
    com, googleapis, a remote box) return False: GPU load here is irrelevant to
    them, so GPU pressure must not throttle a cloud-backed pass. A LAN host is
    treated as remote (not on THIS GPU). Unknown/empty -> assume local (safe:
    we'd rather over-throttle than saturate the local GPU)."""
    if not url:
        return True
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return True
    # Loopback only. (0.0.0.0 is a bind/listen address, not a destination you'd
    # POST to, so it's intentionally excluded — keeps this a clean loopback check.)
    return host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")


def _pace_for_pass(pacing_cpu_ram: dict, pacing_full: dict, uses_local_gpu: bool) -> dict:
    """Pick the pacing a pass must obey. GPU pressure only gates passes that run
    on the local GPU (local LLM/SLM); CPU/RAM pressure gates every pass. So a
    pass that uses the local GPU obeys the stricter of the two ladders; a cloud
    or pure-SQL pass obeys the CPU/RAM-only ladder."""
    if uses_local_gpu:
        # pacing_full already folds in GPU; it's >= the cpu/ram-only verdict.
        return pacing_full
    return pacing_cpu_ram


def _select_pass_order(passes: list, cycle: int, active: bool) -> list:
    """Round-robin + idle-aware pass selection for one cycle. Pure function so the
    fairness policy is unit-testable in isolation from the async loop.

    - ROUND-ROBIN (fairness): rotate the list by `cycle` so a different pass leads
      each cycle. No pass is permanently first, so an always-backlogged upstream
      pass (entity extraction) can't perpetually grab the local GPU before the
      downstream passes get a turn.
    - IDLE-AWARE INTENSITY (interactive UX): when `active` (governor THROTTLED — a
      proxy for the user being busy, since this separate process can't read the
      MCP interaction stamp), return only the rotated LEADER so exactly one pass
      runs this cycle, minimising GPU contention. When idle, return every pass to
      drain the backlog.

    `cycle` may grow unbounded; the modulo keeps rotation stable. Empty `passes`
    returns empty (no crash)."""
    n = len(passes)
    if n == 0:
        return []
    order = [passes[(i + cycle) % n] for i in range(n)]
    return order[:1] if active else order


_PROFILE_LOCAL_CACHE: dict[str, bool] = {}


def _profile_is_local(profile_name: Optional[str]) -> bool:
    """Resolve whether an SLM profile's endpoint is local (on this GPU). Cached;
    a missing/unloadable profile is treated as local (safe default)."""
    key = profile_name or "__default__"
    if key in _PROFILE_LOCAL_CACHE:
        return _PROFILE_LOCAL_CACHE[key]
    is_local = True
    try:
        from slm_intent import load_profile
        prof = load_profile(profile_name) if profile_name else None
        if prof is not None:
            is_local = _is_local_llm_url(getattr(prof, "url", None))
    except Exception:
        is_local = True
    _PROFILE_LOCAL_CACHE[key] = is_local
    return is_local


# Global stop signal for graceful shutdown
_STOP_EVENT = asyncio.Event()

def _signal_handler():
    logger.info("Shutdown signal received. Gracefully stopping...")
    _STOP_EVENT.set()

def _probe_core(db_path: Optional[str], sql: str, params: tuple = ()) -> list:
    """Run a lightweight read-probe against the CORE store, backend-appropriately.

    These cognitive-loop work-gates are cheap existence probes that (a) honor an
    EXPLICIT db_path and (b) must NOT trigger lazy-init/migrations. On SQLite that
    means a direct M3Context(db_path).query_memory (opens exactly db_path, read-
    only-ish, no migration spawn). On PostgreSQL there is ONE pooled store and
    db_path is meaningless, so route through the backend-aware mc._db(). Returns
    the fetched rows (len()>0 == "there is work")."""
    from memory.backends import active_backend
    if active_backend().name == "sqlite":
        return M3Context(db_path).query_memory(sql, params)
    import memory_core as mc
    with mc._db() as db:
        return db.execute(sql, params).fetchall()


def _conn_for_pass(db_path: Optional[str]):
    """A WRITABLE connection context manager for a pass, backend-appropriately.

    Like _probe_core but for read+write passes (classify): SQLite honors the
    explicit db_path via M3Context(db_path).get_sqlite_conn() (no migration spawn,
    write-capable); PG routes through mc._db() (pooled, db_path meaningless). Use
    with a `with ... as conn:` block; conn.execute/commit work on both."""
    from memory.backends import active_backend
    if active_backend().name == "sqlite":
        return M3Context(db_path).get_sqlite_conn()
    import memory_core as mc
    return mc._db()


def _entity_work_sql(items_tbl: str, link_tbl: str) -> str:
    """The un-entitized-rows probe over a given items + item-entities table pair.
    Same SQL shape for core (memory_items/memory_item_entities) and chatlog
    (chat_log_items/chat_log_item_entities on PG)."""
    return f"""
        SELECT 1 FROM {items_tbl} mi
        LEFT JOIN {link_tbl} mie ON mi.id = mie.memory_id
        WHERE mi.is_deleted = 0
          AND mi.type IN ('message', 'chat_log', 'note', 'observation')
          AND mie.memory_id IS NULL
        LIMIT 1
    """


def has_entity_work(core_db: Optional[str], chatlog_db: Optional[str]) -> bool:
    """SQL check: Are there rows without entities yet? Backend-aware: on PG the
    core + chatlog stores are memory_items / chat_log_items in one DB; on SQLite
    they may be separate files."""
    try:
        from memory.backends import active_backend, chatlog_table
        _backend = active_backend()

        # 1. Core store.
        core_sql = _entity_work_sql("memory_items", "memory_item_entities")
        if len(_probe_core(core_db, core_sql)) > 0:
            return True

        # 2. Chatlog store.
        if _backend.name != "sqlite":
            # PG: chat_log_* tables in the same DB — probe them via the pool.
            import memory_core as mc
            cl_sql = _entity_work_sql(
                chatlog_table("items"), chatlog_table("item_entities")
            )
            with mc._db() as db:
                if len(db.execute(cl_sql).fetchall()) > 0:
                    return True
        elif chatlog_db and os.path.abspath(chatlog_db) != os.path.abspath(M3Context(core_db).db_path):
            # SQLite: a SEPARATE chatlog file with the same table names.
            ctx_chat = M3Context(chatlog_db)
            sql = _entity_work_sql("memory_items", "memory_item_entities")
            if len(ctx_chat.query_memory(sql)) > 0:
                return True

        return False
    except Exception as e:
        logger.debug(f"Entity work check failed (non-fatal): {e}")
        return True # Default to True to be safe

def has_enrich_work(core_db: Optional[str]) -> bool:
    """SQL check: Is the observation_queue or reflector_queue non-empty?

    The queues always live in the CORE store (main DB on SQLite; core tables on
    PG — observation_queue/reflector_queue are NOT chatlog-forked). Route through
    the backend-aware mc._db()."""
    try:
        res_obs = _probe_core(core_db, "SELECT 1 FROM observation_queue LIMIT 1")
        res_ref = _probe_core(core_db, "SELECT 1 FROM reflector_queue LIMIT 1")
        return len(res_obs) > 0 or len(res_ref) > 0
    except Exception as e:
        logger.debug(f"Enrich work check failed (non-fatal): {e}")
        return True


def has_consolidate_work(core_db: Optional[str], source_type: str,
                         threshold: int, stale_days: int) -> bool:
    """SQL check: is there an aged 'source_type' group large enough to consolidate
    into a belief? Mirrors memory_consolidate_impl's group query so the loop only
    spends an LLM call when there's real work (event-driven, not time-driven)."""
    try:
        from memory.backends import dialect
        _d = dialect()
        _p = _d.param()
        clause = ""
        params: tuple = (source_type, threshold)
        if stale_days > 0:
            # Only count rows older than the staleness window. now_minus_days binds
            # an INT number of days (portable), not a SQLite '-N days' modifier.
            clause = f" AND created_at < {_d.now_minus_days(_p)}"
            params = (source_type, int(stale_days), threshold)
        sql = (
            f"SELECT 1 FROM memory_items "
            f"WHERE is_deleted = 0 AND type = {_p}" + clause + " "
            f"GROUP BY type, agent_id, user_id HAVING COUNT(*) > {_p} LIMIT 1"
        )
        return len(_probe_core(core_db, sql, params)) > 0
    except Exception as e:
        logger.debug(f"Consolidate work check failed (non-fatal): {e}")
        return False  # conservative: no LLM work unless we can confirm there is some

async def run_entity_pass(args):
    """Run incremental entity extraction on core and chatlog DBs."""
    if not has_entity_work(args.database, args.chatlog_db):
        logger.debug("No pending entity work. Skipping pass.")
        return

    logger.info("Starting Entity Extraction pass...")
    try:
        ent_args = argparse.Namespace(
            profile=args.profile_entities,
            entity_vocab_yaml=None,
            core_only=False,
            chatlog_only=False,
            core_db=args.database,
            chatlog_db=args.chatlog_db,
            source_variant="__none__",
            types=None,
            limit=args.limit_per_pass,
            concurrency=args.concurrency,
            force=False,
            dry_run=False,
            skip_preflight=True,
            yes=True,
            # m3_entities._main_async reads args.embed_url/embed_model directly
            # (not via getattr). The argparse CLI defaults them to the
            # M3_EMBED_URL/M3_EMBED_MODEL env vars; mirror that here so the
            # loop honors the same embedder override and doesn't AttributeError.
            embed_url=os.environ.get("M3_EMBED_URL"),
            embed_model=os.environ.get("M3_EMBED_MODEL"),
        )
        await m3_entities._main_async(ent_args)
    except Exception as e:
        logger.error(f"Entity pass error: {type(e).__name__}: {e}")

async def run_enrich_pass(args):
    """Run incremental Observation + Reflection pass."""
    if not has_enrich_work(args.database):
        logger.debug("No pending enrichment work. Skipping pass.")
        return

    logger.info("Starting Enrichment (Observer + Reflector) pass...")
    try:
        enrich_args = argparse.Namespace(
            profile=args.profile_enrich,
            profile_path=None,
            reflector_profile=None,
            core_only=False,
            chatlog_only=False,
            core_db=args.database,
            chatlog_db=args.chatlog_db,
            target_variant="m3-observations-auto",
            source_variant="__none__",
            source_conv_list=None,
            track_state=True,
            resume=True,
            include_dead_letter=False,
            max_attempts=3,
            budget_usd=None,
            sample=None,
            sample_strategy="first",
            input_max_k=None,
            min_size_k=None,
            max_size_k=None,
            send_to=None,
            limit=args.limit_per_pass,
            concurrency=args.concurrency,
            cascade_threshold=10,
            cascade_window_s=60,
            report=False,
            no_report=True,
            include_summaries=False,
            include_notes=False,
            include_types=None,
            only_use_types=None,
            drain_queue=True,
            drain_batch=args.limit_per_pass,
            no_reflect=args.no_reflect,
            reflector_threshold=args.reflector_threshold,
            dry_run=False,
            skip_preflight=True,
            yes=True
        )
        await m3_enrich._main_async(enrich_args)
    except Exception as e:
        logger.error(f"Enrichment pass error: {type(e).__name__}: {e}")


def has_embed_work(core_db: Optional[str]) -> bool:
    """SQL check: are there memory_items rows with no embedding yet?

    These accumulate when memory_write deferred embedding (no fast embedder
    available at write time — the zero-lag path). embed_backfill's own
    _count_pending uses the same WHERE-NOT-EXISTS query, so this is an accurate,
    cheap event-gate."""
    try:
        import embed_backfill
        db_path = core_db or os.environ.get("M3_DATABASE")
        if not db_path:
            return False
        args = embed_backfill._parse_args(["--db", str(db_path), "--limit", "1"])
        return embed_backfill._count_pending(args.db, args) > 0
    except Exception as e:
        logger.debug(f"Embed-backfill work check failed (non-fatal): {e}")
        return False  # conservative: don't spin the embedder unless we're sure


def _checkpoint_wal(db_path: Optional[str]) -> None:
    """Explicit WAL checkpoint at the end of a work cycle (§10 WAL discipline).

    The loop is the heavy writer on agent_memory.db; a co-reader (the MCP
    memory server) runs on the same DB. SQLite's passive wal_autocheckpoint
    BUSY-FAILS whenever that reader holds a lock (documented in
    sqlite_pragmas.py), so the WAL grows to its journal_size_limit ceiling
    (64 MiB) and then wedges every writer AND reader — this is what deadlocked
    the MCP server (2026-07-03, 32-min memory_search hang). An explicit
    TRUNCATE checkpoint here is more assertive than PASSIVE and resets the WAL
    file size. It runs only after a cycle that did write work, off the event
    loop, and NEVER crashes the loop on failure (fail-safe): a busy checkpoint
    is retried next cycle, it does not stall the heartbeat.

    No-op on non-SQLite backends: WAL is a SQLite-internal concept. PostgreSQL
    manages its own WAL + checkpointing via the background checkpointer/autovacuum,
    so there is nothing for the loop to do — return immediately.
    """
    try:
        from memory.backends import active_backend
        if active_backend().name != "sqlite":
            return  # PG self-manages WAL; nothing to checkpoint from here
    except Exception:  # noqa: BLE001 — never let a backend probe break the loop
        pass
    try:
        import sqlite3

        from sqlite_pragmas import apply_pragmas, profile_for_db
        path = db_path or os.environ.get("M3_DATABASE")
        if not path:
            return
        conn = sqlite3.connect(path, timeout=10)
        try:
            apply_pragmas(conn, profile_for_db(path))
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            # row = (busy, log_pages, checkpointed_pages). busy==1 => a reader
            # blocked the full truncate; log loudly so a chronic wedge is visible
            # rather than silently starving (fail-loud, §3).
            if row and row[0] == 1:
                logger.warning(
                    "WAL checkpoint(TRUNCATE) was BUSY (a reader held the lock); "
                    "WAL not fully reset this cycle (log=%s ckpt=%s). Will retry next cycle.",
                    row[1] if len(row) > 1 else "?", row[2] if len(row) > 2 else "?")
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — checkpoint is best-effort; never wedge the loop
        logger.warning("WAL checkpoint failed (non-fatal): %s", e)


async def run_embed_pass(args):
    """Drain deferred embeddings via embed_backfill.

    memory_write defers embedding when no fast embedder is available (§3/§8
    zero-lag write), leaving rows with a persisted verbatim body but no vector.
    This pass fills those vectors. Bounded by --limit-per-pass so one cycle
    can't monopolize the GPU; the round-robin scheduler gives other passes a
    turn (see _select_pass_order)."""
    if not has_embed_work(args.database):
        logger.debug("No pending embed-backfill work. Skipping pass.")
        return

    # Active embed-server recovery (§8): there IS pending embed work, which often
    # means writes deferred because the fast embedder was down. If the shared
    # tier-2 server (:8082) has since come back, force the client breakers closed
    # NOW rather than waiting for organic traffic to re-open them — otherwise this
    # backfill sweep would cascade through dead tiers and re-defer. No-op (and no
    # network cost beyond one /health GET) when the server is still down.
    try:
        from memory.embed import recover_if_fallback_healthy
        if recover_if_fallback_healthy():
            logger.info("Embed-backfill: tier-2 server healthy — breakers reset before sweep.")
    except Exception as e:
        logger.debug(f"Active embed recovery check failed (non-fatal): {e}")

    logger.info("Starting Embed-backfill pass...")
    try:
        import embed_backfill
        bf_args = embed_backfill._parse_args(
            ["--db", str(args.database), "--limit", str(args.limit_per_pass)]
        )
        counters = embed_backfill.Counters()
        await embed_backfill._run_sweep(bf_args, counters)
    except Exception as e:
        logger.error(f"Embed-backfill pass error: {type(e).__name__}: {e}")


def has_classify_work(core_db: Optional[str]) -> bool:
    """SQL event-gate: are there rows persisted as type='auto' awaiting
    classification? These accumulate from the zero-lag write path — memory_write
    with auto_classify defers the LLM classify (persist as 'auto', §3/§8 zero-lag)
    and this sweep resolves the real type later. Cheap COUNT gate; skips the pass
    when nothing is pending."""
    try:
        sql = "SELECT 1 FROM memory_items WHERE type='auto' AND COALESCE(is_deleted,0)=0 LIMIT 1"
        return len(_probe_core(core_db, sql)) > 0
    except Exception as e:  # noqa: BLE001 — conservative gate; don't spin on error
        logger.debug(f"Classify work check failed (non-fatal): {e}")
        return False


async def run_classify_pass(args):
    """Classify rows deferred as type='auto' by the zero-lag write path.

    memory_write persists auto_classify rows as type='auto' rather than blocking
    the write on an LLM call (§3/§8). This sweep selects those rows, asks the
    local LLM for the real type with a BOUNDED timeout, and UPDATEs the type.
    Fail-open: on LLM timeout/error a row stays 'auto' and is retried next sweep —
    never lost, never blocking. Bounded by --limit-per-pass. The per-classify
    deadline is M3_CLASSIFY_DEADLINE_S (default 10s)."""
    if not has_classify_work(args.database):
        logger.debug("No pending classification work. Skipping pass.")
        return

    from memory.backends import dialect
    from memory.enrich import _auto_classify

    _p = dialect().param()
    deadline_s = float(os.environ.get("M3_CLASSIFY_DEADLINE_S", "10"))

    logger.info("Starting Classification pass...")
    # Fetch the batch with a short-lived connection; do the (slow) LLM work
    # OUTSIDE any open cursor/transaction so we never hold a lock across an LLM
    # call (§3 cursor/lock discipline, §10 WAL — don't wedge co-readers). Backend-
    # appropriate connection (SQLite honors args.database; PG pools) replaces the
    # raw sqlite3.connect (was a stale-file write on PG).
    with _conn_for_pass(args.database) as db:
        rows = db.execute(
            "SELECT id, content, title FROM memory_items "
            "WHERE type='auto' AND COALESCE(is_deleted,0)=0 "
            f"ORDER BY created_at LIMIT {_p}",
            (int(args.limit_per_pass),),
        ).fetchall()

    resolved = 0
    for item_id, content, title in rows:
        try:
            new_type = await asyncio.wait_for(
                _auto_classify(content or "", title or ""), timeout=deadline_s
            )
        except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001 — fail-open
            logger.warning(
                "Classify deferred: row %s left as 'auto' (LLM unavailable/slow: %s). "
                "Will retry next sweep.", item_id, type(e).__name__)
            continue
        # _auto_classify returns 'note' as its own fail-open default; only UPDATE
        # when we got a REAL classification (not the sentinel and not still 'auto').
        if new_type and new_type != "auto":
            with _conn_for_pass(args.database) as uconn:
                uconn.execute(
                    f"UPDATE memory_items SET type={_p}, updated_at={_p} "
                    f"WHERE id={_p} AND type='auto'",
                    (new_type, datetime.now(timezone.utc).isoformat(), item_id),
                )
                uconn.commit()
                resolved += 1
    logger.info("Classification pass: resolved %d/%d row(s).", resolved, len(rows))


async def run_consolidate_pass(args):
    """Roll up aged 'observation' groups into high-order 'belief' memories
    (knowledge-maintenance Phase 4) — governor-gated, event-driven. Only fires
    when a group exceeds the threshold AND M3_CONSOLIDATION_AUTO=1 is set (the
    job itself enforces the dry-run-unless-opted-in + activity-yield contract).
    Delegates to consolidate_beliefs._run so the loop and the standalone cron/CLI
    share ONE implementation."""
    src = args.consolidate_source_type
    if not has_consolidate_work(args.database, src,
                                args.consolidate_threshold, args.consolidate_stale_days):
        logger.debug("No consolidation work (no aged group over threshold). Skipping.")
        return
    logger.info("Starting Belief Consolidation pass...")
    try:
        import consolidate_beliefs
        out = await consolidate_beliefs._run(
            apply=True,  # the job gates real writes on M3_CONSOLIDATION_AUTO + idle
            threshold=args.consolidate_threshold,
            stale_days=args.consolidate_stale_days,
            source_type=src,
        )
        logger.info("Consolidation pass: %s", out.strip().replace("\n", " | "))
    except Exception as e:
        logger.error(f"Consolidation pass error: {type(e).__name__}: {e}")


def has_chatlog_prune_work(chatlog_db: Optional[str], prune_days: float,
                           min_rows: int) -> bool:
    """SQL check: are there enough aged chat_log turns to bother pruning?
    Event-driven gate (mirrors has_consolidate_work) so the loop only does a
    sweep when a real backlog of prune-eligible noise has accumulated."""
    try:
        from memory.backends import chatlog_table, dialect
        _d = dialect()
        _p = _d.param()
        _T = chatlog_table("items")  # memory_items (sqlite) | chat_log_items (pg)
        sql = (
            f"SELECT 1 FROM {_T} "
            f"WHERE type='chat_log' AND is_deleted=0 AND importance <= 0.3 "
            f"AND created_at < {_d.now_minus_days(_p)} "
            f"GROUP BY type HAVING COUNT(*) > {_p} LIMIT 1"
        )
        # Route through the chatlog connection (PG: core pool + chat_log_* tables;
        # SQLite: the chatlog file). now_minus_days binds an INT (portable).
        ctx = M3Context.for_db(None)
        with ctx.get_chatlog_conn() as conn:
            return len(conn.execute(sql, (int(prune_days), min_rows)).fetchall()) > 0
    except Exception as e:
        logger.debug(f"Chatlog-prune work check failed (non-fatal): {e}")
        return False


async def run_chatlog_prune_pass(args):
    """Aged noise pruning for chatlog turns — governor-gated, event-driven.

    Suppresses 14-45d noise (importance down) and soft-deletes >45d
    high-confidence noise (durable-signal / substantial-structured turns are
    protected to suppress-only). Tombstones propagate fleet-wide via
    is_deleted+updated_at sync and stay recoverable. Real writes require
    M3_CHATLOG_PRUNE_AUTO=1 (else dry-run) — same opt-in contract as belief
    consolidation. Delegates to chatlog_prune.run so the loop and the standalone
    CLI share ONE implementation."""
    if not has_chatlog_prune_work(args.chatlog_db, args.chatlog_prune_days,
                                  args.chatlog_prune_threshold):
        logger.debug("No chatlog-prune work (no aged backlog over threshold). Skipping.")
        return
    apply = os.environ.get("M3_CHATLOG_PRUNE_AUTO", "0").lower() in ("1", "true", "yes")
    logger.info("Starting chatlog noise-prune pass (apply=%s)...", apply)
    try:
        from types import SimpleNamespace

        import chatlog_prune
        opts = SimpleNamespace(
            fresh_days=args.chatlog_prune_fresh_days,
            prune_days=args.chatlog_prune_days,
            status_min_cluster=5, generic_imp_max=0.3, keep_imp_floor=0.4,
            generic_protect_len=300, generic_delete_maxlen=300,
            # Bound writes per cycle (§8): the loop fires every --interval, so a
            # large backlog drains over many cycles instead of one monster pass
            # that blocks the heartbeat. Oldest noise is handled first.
            max_actions=args.chatlog_prune_max_actions,
            no_generic=False, apply=apply)
        summary = chatlog_prune.run(args.chatlog_db, opts)
        logger.info("Chatlog-prune pass: suppress=%s soft-delete=%s capped=%s (apply=%s)",
                    summary.get("writes_decay"), summary.get("writes_prune"),
                    summary.get("capped"), apply)
    except Exception as e:
        logger.error(f"Chatlog-prune pass error: {type(e).__name__}: {e}")


async def main_loop(args):
    """Main execution loop with adaptive backoff and signal awareness."""
    # This daemon is a thin orchestrator: every pass delegates to an in-process
    # module (m3_entities/m3_enrich/embed_backfill/memory.enrich/consolidate_beliefs/
    # chatlog_prune) that is now backend-agnostic, its work-gates route through the
    # backend-aware mc._db(), and the WAL checkpoint is skipped on PG (which manages
    # its own WAL). So the loop runs on both SQLite and PostgreSQL. (Previously
    # gated SQLite-only because the passes + WAL checkpoint hit raw sqlite3; those
    # were ported/guarded and verified on PG, so the gate was removed.)
    logger.info(f"Cognitive Loop heartbeat started. Interval: {args.interval}s")

    # Register signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    # The configured per-pass batch ceiling. Under load we shrink the EFFECTIVE
    # limit below this so each pass is small and the loop re-evaluates the
    # governor (incl. live GPU/LLM load) between tiny batches instead of charging
    # through a full 50-item batch of LLM calls with no re-check. Restored to the
    # full value once load clears.
    _full_limit = args.limit_per_pass
    # Round-robin cursor: which pass leads each cycle. Rotating the start avoids
    # the fixed-priority starvation where entity extraction — always backlogged —
    # would perpetually run first and grab the local GPU before enrich/consolidate
    # ever get a turn under contention. Each pass takes the lead 1-in-N cycles.
    _cycle = 0
    while not _STOP_EVENT.is_set():
        # ── Adaptive Governor Gating (per-resource, per-pass) ──────────────────
        # Each resource gates the work that consumes it: CPU/RAM pressure gates
        # EVERY background pass; GPU pressure gates only passes that run on the
        # local GPU (local LLM/SLM). So we compute TWO verdicts — one from
        # CPU/RAM only, one that also folds in GPU — and apply the right one per
        # pass. Cloud-backed and pure-SQL passes ignore GPU load entirely.
        pacing_full = {"background": "CONTINUOUS", "background_delay": 0.1}
        pacing_cpu_ram = dict(pacing_full)
        telemetry = {}
        any_throttled = False
        try:
            ctx = M3Context.for_db(args.database)
            telemetry = ctx.get_system_telemetry()
            pacing_full = get_governor_pacing(telemetry)
            pacing_cpu_ram = get_governor_pacing({**telemetry, "gpu_total": 0.0})
            if telemetry.get("thermal") in ("Serious", "Critical"):
                logger.info("Thermal load serious. Pausing cognitive loop for 10s...")
                await asyncio.sleep(10.0)
                continue
            # If CPU/RAM alone halts, NOTHING should run — even cloud/SQL contend
            # for local CPU. Short-circuit the whole cycle.
            if pacing_cpu_ram["background"] == "HALTED":
                logger.info("CPU/RAM load critical (cpu=%.0f ram=%.0f). All background "
                            "work HALTED. Sleeping 5s...",
                            telemetry.get("cpu_total", 0.0), telemetry.get("ram_total", 0.0))
                await asyncio.sleep(5.0)
                continue
        except Exception as e:
            logger.debug(f"Governor check error (non-fatal): {e}")

        # Resolve, per LLM pass, whether it runs on the LOCAL GPU (so GPU load
        # applies) or is cloud-backed (GPU load irrelevant). The chatlog-prune
        # pass is pure SQL → never uses the GPU.
        entity_local = _profile_is_local(args.profile_entities)
        enrich_local = _profile_is_local(args.profile_enrich)
        consolidate_local = True  # belief consolidation uses the local LLM

        def _effective_limit(pace: dict) -> int:
            return _THROTTLED_LIMIT if pace["background"] == "THROTTLED" else _full_limit

        def _run_gate(pace: dict) -> bool:
            """True if a pass under this pacing should run at all (HALTED skips)."""
            nonlocal any_throttled
            if pace["background"] == "HALTED":
                return False
            if pace["background"] == "THROTTLED":
                any_throttled = True
            return True

        start_time = time.monotonic()

        # The passes as a rotatable list. `gpu` marks passes that run on the
        # local GPU (so GPU load gates them); chatlog-prune is pure SQL. `sets_limit`
        # marks the LLM passes whose batch size we shrink under throttle.
        passes = [
            {"name": "entities",   "skip": args.skip_entities,      "gpu": entity_local,      "sets_limit": True,  "run": run_entity_pass},
            {"name": "enrich",     "skip": args.skip_enrich,        "gpu": enrich_local,      "sets_limit": True,  "run": run_enrich_pass},
            {"name": "embed",      "skip": args.skip_embed,         "gpu": True,              "sets_limit": True,  "run": run_embed_pass},
            {"name": "classify",   "skip": args.skip_classify,      "gpu": enrich_local,      "sets_limit": True,  "run": run_classify_pass},
            {"name": "consolidate","skip": args.skip_consolidate,   "gpu": consolidate_local, "sets_limit": False, "run": run_consolidate_pass},
            {"name": "prune",      "skip": args.skip_chatlog_prune, "gpu": None,              "sets_limit": False, "run": run_chatlog_prune_pass},
        ]
        # Round-robin order + idle-aware intensity (see _select_pass_order). When
        # the governor is THROTTLED we treat the host as user-active and run only
        # the rotated leader this cycle; otherwise every pass runs. Rotation makes
        # sure the leader differs each cycle so no pass is starved.
        active = pacing_full.get("background") == "THROTTLED"
        order = _select_pass_order(passes, _cycle, active)
        _cycle += 1

        for p in order:
            if p["skip"]:
                continue
            # prune is pure SQL → gated only by CPU/RAM; everything else uses the
            # per-pass verdict (GPU folded in only for local-GPU passes).
            pace = pacing_cpu_ram if p["gpu"] is None else _pace_for_pass(
                pacing_cpu_ram, pacing_full, p["gpu"])
            if _run_gate(pace):
                if p["sets_limit"]:
                    args.limit_per_pass = _effective_limit(pace)
                await p["run"](args)
            if _STOP_EVENT.is_set():
                break

        args.limit_per_pass = _full_limit  # restore for the next cycle's defaults

        # §10 WAL discipline: the loop is the heavy writer; force a checkpoint at
        # the cycle boundary so the WAL can't grow to its 64 MiB ceiling and wedge
        # the co-reading MCP server (the 2026-07-03 32-min-hang root cause). Run
        # off the event loop (may block briefly on a busy reader) and fail-safe.
        await asyncio.to_thread(_checkpoint_wal, args.database)

        elapsed = time.monotonic() - start_time
        wait_time = max(0, args.interval - elapsed)
        # If any pass ran throttled (tiny batch), don't also wait the full
        # interval (that would crawl the backlog) — but DO insert the throttle
        # delay so the shrunk batches don't busy-loop and re-saturate the host.
        if any_throttled:
            delay = float(pacing_full.get("background_delay",
                          pacing_cpu_ram.get("background_delay", 10.0)))
            wait_time = min(wait_time, delay)
        else:
            # IDLE BACKLOG DRAIN (§5/§8): each heavy-LLM pass processes at most
            # --limit-per-pass items, so a real backlog needs many cycles. When
            # the host is idle (not throttled) and work remains, re-tick after a
            # short floor instead of sleeping the full --interval — otherwise an
            # idle machine trickles one small batch per interval (the "1 item /
            # 5 min" symptom) while CPU/GPU/RAM sit unused. The batch ceiling
            # still bounds each burst, so the governor stays responsive; only
            # the between-pass idle wait is shortened while there's a backlog.
            more_work = (
                (not args.skip_entities and has_entity_work(args.database, args.chatlog_db))
                or (not args.skip_enrich and has_enrich_work(args.database))
                or (not args.skip_embed and has_embed_work(args.database))
                or (not args.skip_classify and has_classify_work(args.database))
            )
            if more_work:
                floor = float(pacing_full.get("background_delay", 0.1))
                wait_time = min(wait_time, max(floor, 1.0))

        if _STOP_EVENT.is_set():
            break

        try:
            await asyncio.wait_for(_STOP_EVENT.wait(), timeout=wait_time)
        except asyncio.TimeoutError:
            pass

    logger.info("Cognitive Loop stopped.")

def main():
    parser = argparse.ArgumentParser(description="m3-memory Cognitive Loop")
    parser.add_argument("--interval", type=int, default=300,
                        help="Seconds between passes (default: 300)")
    parser.add_argument("--background", action="store_true", help="Run in background (fire and forget)")
    parser.add_argument("--log-file", default=None, metavar="PATH",
                        help="Append logging to this file (scheduled-task / service mode). "
                             "Survives the Windows pythonw re-exec.")
    parser.add_argument("--concurrency", type=int, default=2, help="SLM concurrency (default: 2)")
    parser.add_argument("--limit-per-pass", type=int, default=2,
                        help="Max groups/rows per heavy-LLM pass (entity extraction, "
                             "enrichment, observation drain). Default 2: small enough that "
                             "one pass is a few-second GPU burst (the governor is only "
                             "re-checked BETWEEN passes, not within a batch — a 50-item pass "
                             "once pinned the GPU for ~17 min), large enough that an idle "
                             "host drains the backlog at a useful rate instead of one item "
                             "per cycle. Under THROTTLED load this is shrunk to "
                             "M3_GOVERNOR_THROTTLED_LIMIT (default 1); when idle the loop "
                             "also re-ticks immediately if a backlog remains (see the "
                             "backlog-aware wait below) rather than sleeping the full "
                             "--interval. Embedding is a separate scheduled task "
                             "(ChatlogEmbedSweep) and is unaffected.")

    # Database knobs
    parser.add_argument("--database", default=None, help="Core Memory DB path (Env: M3_DATABASE)")
    parser.add_argument("--chatlog-db", default=None, help="Chatlog DB path (Env: CHATLOG_DB_PATH)")

    parser.add_argument("--profile-entities", default="entities_local_qwen", help="Profile for entities")
    parser.add_argument("--profile-enrich", default="enrich_local_qwen", help="Profile for enrichment")
    parser.add_argument("--reflector-threshold", type=int, default=5, help="Min observations before Reflector (default: 5)")
    parser.add_argument("--skip-entities", action="store_true", help="Skip entity extraction")
    parser.add_argument("--skip-enrich", action="store_true", help="Skip enrichment pass")
    parser.add_argument("--skip-embed", action="store_true",
                        help="Skip embed-backfill pass (draining deferred zero-lag-write vectors)")
    parser.add_argument("--skip-classify", action="store_true",
                        help="Skip classification pass (resolving type='auto' rows deferred by zero-lag writes)")
    parser.add_argument("--no-reflect", action="store_true", help="Skip reflection pass")
    # Belief consolidation pass (knowledge-maintenance Phase 4). Event-driven +
    # governor-gated inside the loop; the job still requires M3_CONSOLIDATION_AUTO=1
    # to actually write (else dry-run). Replaces the standalone weekly cron.
    parser.add_argument("--skip-consolidate", action="store_true",
                        help="Skip the belief-consolidation pass")
    parser.add_argument("--consolidate-threshold", type=int, default=50,
                        help="Min same-type group size before consolidating (default: 50)")
    parser.add_argument("--consolidate-stale-days", type=int, default=7,
                        help="Only consolidate items older than N days (default: 7)")
    parser.add_argument("--consolidate-source-type", default="observation",
                        help="Episodic source memory type to roll up (default: observation)")

    # Chatlog aged noise-prune pass. Governor-gated + event-driven; real
    # writes require M3_CHATLOG_PRUNE_AUTO=1 (else dry-run). Replaces a fixed cron.
    parser.add_argument("--skip-chatlog-prune", action="store_true",
                        help="Skip the chatlog noise-prune pass")
    parser.add_argument("--chatlog-prune-threshold", type=int, default=2000,
                        help="Min aged prune-eligible chat_log rows before a sweep (default: 2000)")
    parser.add_argument("--chatlog-prune-fresh-days", type=float, default=14.0,
                        help="Keep noise newer than N days untouched (default: 14)")
    parser.add_argument("--chatlog-prune-days", type=float, default=45.0,
                        help="Soft-delete aged noise older than N days (default: 45)")
    parser.add_argument("--chatlog-prune-max-actions", type=int, default=5000,
                        help="Max decay+prune writes per cycle (default: 5000; 0 = "
                             "no cap). Caps one pass so a backlog drains across "
                             "cycles instead of blocking the heartbeat.")

    args = parser.parse_args()

    if args.background:
        if sys.platform == "win32":
            daemonize_windows(args)
        else:
            daemonize_unix()

    # Only acquire lock AFTER daemonizing
    acquire_lock()

    # Attach a file handler AFTER daemonizing so it lands in the real worker
    # process (the pythonw re-exec on Windows / double-fork on Unix). The
    # --log-file arg survives the re-exec because daemonize_windows copies
    # sys.argv[1:]. Under launchd/systemd there is no re-exec (no --background)
    # and this still runs in the managed process.
    if args.log_file:
        os.makedirs(os.path.dirname(os.path.abspath(args.log_file)), exist_ok=True)
        _fh = logging.FileHandler(args.log_file, encoding="utf-8")
        _fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logging.getLogger().addHandler(_fh)

    # Resolve paths once to normalize env vs flag
    if args.database:
        os.environ["M3_DATABASE"] = os.path.abspath(args.database)
    args.database = resolve_db_path()

    if args.chatlog_db:
        os.environ["CHATLOG_DB_PATH"] = os.path.abspath(args.chatlog_db)
    args.chatlog_db = chatlog_config.chatlog_db_path()

    # Seed .governor_config.json with current defaults if absent, so the live
    # tuning knob always exists and is discoverable (idempotent; never clobbers).
    ensure_governor_config()

    try:
        asyncio.run(main_loop(args))
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
