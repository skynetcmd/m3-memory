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
import json
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
import m3_halt
from m3_sdk import (
    M3Context,
    ensure_governor_config,
    get_governor_pacing,
    get_m3_config_root,
    resolve_db_path,
)

# PID file path for single-instance locking (distinct from m3_halt's PID/
# registry: this enforces one-loop-at-a-time; the registry tracks DB-holders so
# an exclusive op can quiesce them — see docs/design/HALT_PROTOCOL.md).
PID_FILE = REPO_ROOT / "memory" / "cognitive_loop.pid"

# Role name this process registers under in the m3_halt PID registry.
_HALT_ROLE = "cognitive-loop"

def daemonize_windows(args):
    """Restart this process using pythonw.exe to detach from console."""
    # Build the same argument list but remove --background
    argv = [sys.executable.replace("python.exe", "pythonw.exe")]
    argv.append(os.path.abspath(__file__))
    for arg in sys.argv[1:]:
        if arg != "--background":
            argv.append(arg)

    # CRITICAL: the detached child runs under pythonw.exe, which has NO console
    # and therefore an INVALID stdout/stderr (sys.stdout is None / a broken
    # handle). The entity/enrich passes shell into m3_entities / m3_enrich, which
    # emit dozens of print() lines (dry-run banner, [m3-entities] progress). The
    # first such print() in the detached child raises OSError writing to the dead
    # handle and — with no console to surface it — the loop dies SILENTLY, right
    # after "Starting Entity Extraction pass...". That is the "loop starts, logs
    # one line, then goes quiet and gets respawned by the scheduler" bug: every
    # background instance died on the first entity/enrich pass, so extraction only
    # ran in the brief window before that pass. The Unix daemonize already dup2's
    # the std streams to /dev/null (see daemonize_unix); Windows never did.
    #
    # Redirect the child's stdout+stderr to the --log-file if given (so those
    # print() lines are captured alongside the logger output), else to devnull.
    # Both must be REAL OS handles Popen can inherit — os.devnull or an opened
    # file, never None.
    if args.log_file:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.log_file)), exist_ok=True)
            child_out = open(args.log_file, "a", encoding="utf-8")
        except OSError:
            child_out = open(os.devnull, "a")
    else:
        child_out = open(os.devnull, "a")

    # Spawn the detached child, then HARD-exit the parent. os._exit(0) — not
    # sys.exit(0) — because sys.exit raises SystemExit, which an outer try/except
    # (or the asyncio runner if we got here late) can swallow, leaving the parent
    # ALIVE alongside the child. Two live loops each dispatch their own
    # Semaphore(2) of SLM calls = over-dispatch that storms the local LLM.
    with child_out:
        subprocess.Popen(
            argv,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            stdin=subprocess.DEVNULL,
            stdout=child_out,
            stderr=subprocess.STDOUT,
        )
    # CRITICAL: the parent MUST hard-exit now, or we get TWO live loops (the
    # detached child worker + this parent). The status print() is best-effort and
    # MUST NOT be able to prevent the exit: under pythonw.exe there is NO console,
    # so sys.stdout is None / a dead handle and BOTH print() and flush() raise
    # (AttributeError/OSError/ValueError). If that exception propagates before
    # os._exit(0), the parent survives, falls through to acquire_lock()+main_loop,
    # and double-dispatches to the local LLM — the observed "parent+child both
    # looping" bug that survived the scheduled-task single-instance fix. Wrap the
    # I/O so nothing stands between the Popen and the exit.
    try:
        print("Cognitive Loop started in background (Windows pythonw).")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 — no console under pythonw; never block the exit
        pass
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

def _pid_is_live_loop(pid: int) -> bool:
    """True iff `pid` is alive AND is actually a cognitive-loop process (not a
    reused PID now owned by something unrelated). Liveness alone is not enough: a
    reused PID would make acquire_lock refuse to start forever (false positive).
    On error, err toward "live" (conservative — refuse to start a possible second
    instance rather than risk two loops double-dispatching to the local LLM)."""
    if pid <= 0:
        return False
    # 1. Liveness.
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False  # no such process (or truly inaccessible) -> stale
        ctypes.windll.kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False  # definitely gone -> stale
        except PermissionError:
            return True   # exists but not ours to signal -> treat as live
        except OSError:
            return True   # unknown -> conservative
    # 2. Identity: is the live PID actually a cognitive_loop? Best-effort via
    # psutil; if psutil is absent or the check fails, fall back to "live == ours"
    # (the old behaviour) so we never START a duplicate on an inconclusive probe.
    try:
        import psutil
        cmdline = " ".join(psutil.Process(pid).cmdline())
        return "cognitive_loop" in cmdline
    except Exception:
        return True  # can't confirm identity -> assume it IS the loop (safe)


def acquire_lock():
    """Ensure only one instance of the loop is running.

    Uses an ATOMIC exclusive-create (O_CREAT|O_EXCL) as the actual mutex so two
    near-simultaneous launches can't both pass a check-then-write window — exactly
    the race that produced two live loops (double LLM dispatch). If the create
    fails because the file already exists, we inspect the recorded PID: a genuinely
    stale lock (dead PID, or a reused PID now owned by a non-loop process) is
    reclaimed; a live loop makes us exit."""
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    my_pid = os.getpid()

    def _write_pid_atomic() -> bool:
        """Try to create the lock file exclusively. True if we won it."""
        try:
            fd = os.open(str(PID_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        try:
            os.write(fd, str(my_pid).encode())
        finally:
            os.close(fd)
        return True

    if not _write_pid_atomic():
        # Lock file exists — decide whether the holder is live or stale.
        try:
            old_pid = int(PID_FILE.read_text().strip())
        except (ValueError, OSError):
            old_pid = -1  # unreadable/garbage -> treat as stale, reclaim
        if old_pid != my_pid and _pid_is_live_loop(old_pid):
            print(f"ERROR: Cognitive Loop is already running (PID {old_pid}). Exiting.")
            sys.exit(0)
        # Stale (dead PID / reused-by-other / garbage): reclaim by overwriting.
        # There is no second racer here — a real concurrent launch would have been
        # caught by the O_EXCL create above — so a plain overwrite is safe.
        try:
            PID_FILE.write_text(str(my_pid))
        except OSError as e:
            print(f"WARNING: could not reclaim stale lock {PID_FILE}: {e}")

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


def _select_pass_order(passes: list, cycle: int, active: bool,
                       has_work: "dict[str, bool] | None" = None) -> list:
    """Round-robin + idle-aware + queue-aware pass selection for one cycle. Pure
    function so the fairness policy is unit-testable in isolation from the async
    loop.

    - QUEUE-AWARE (no wasted slot): `has_work` maps a pass name -> whether its
      queue has pending work RIGHT NOW. A pass whose name is present and maps to
      False is dropped from this cycle's rotation. This matters most under
      THROTTLED load, where only ONE pass runs: without this, the rotation could
      hand the single slot to an EMPTY pass (e.g. enrich/reflect with a 0-depth
      queue) while a backlogged pass (entity extraction with thousands of rows)
      waits another full cycle — the "no entity-extraction requests while the
      backlog is huge" symptom. Passes NOT present in `has_work` (the time-driven
      sync/maintenance/audit, which have no queryable queue — their gate is "is it
      due?", checked inside the pass) are always kept: absence means "eligibility
      unknown, let the pass decide", never "no work". `has_work=None` disables the
      filter entirely (back-compat: rotate all passes).
    - ROUND-ROBIN (fairness): rotate the *eligible* list by `cycle` so a different
      pass leads each cycle. No pass is permanently first, so an always-backlogged
      upstream pass (entity extraction) can't perpetually grab the local GPU before
      the downstream passes get a turn.
    - IDLE-AWARE INTENSITY (interactive UX): when `active` (governor THROTTLED — a
      proxy for the user being busy, since this separate process can't read the
      MCP interaction stamp), return only the rotated LEADER so exactly one pass
      runs this cycle, minimising GPU contention. When idle, return every eligible
      pass to drain the backlog.

    Order of operations: filter-empty FIRST, then rotate, then (if active) take the
    leader — so the leader is always drawn from passes that actually have work.

    `cycle` may grow unbounded; the modulo keeps rotation stable. Empty `passes`
    (or all-filtered) returns empty (no crash)."""
    if has_work is not None:
        # Keep a pass unless it is explicitly known to have no work. A name absent
        # from the map is kept (unknown eligibility -> the pass's own gate decides).
        eligible = [p for p in passes if has_work.get(p["name"], True)]
    else:
        eligible = list(passes)
    n = len(eligible)
    if n == 0:
        return []
    order = [eligible[(i + cycle) % n] for i in range(n)]
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


# ── Per-pass min-interval gate ────────────────────────────────────────────────
# Some passes are TIME-driven, not backlog-driven: warehouse sync and the weekly
# audit have no queryable "work queue" — "is there work?" is purely "has enough
# time elapsed?". These run in the same governor-paced round-robin as the other
# passes (so they're deferred under load, never a rigid clock), but a min-interval
# gate keeps them from firing every tick. Run timestamps live in a small JSON file
# beside .governor_config.json (file-based, like the rest of the loop's state — no
# DB migration). Cheap-when-not-due is the whole point: a sync that ran 5 min ago
# is skipped without a network round-trip; an audit that ran yesterday is a no-op.
def _loop_pass_runs_path() -> str:
    return os.path.join(get_m3_config_root(), ".loop_pass_runs.json")


def _read_pass_runs() -> dict:
    """Read the {pass_name: iso_timestamp} map. Never raises — a missing/corrupt
    file means "nothing has run", so every pass reads as due (fail-open, correct
    for a first run)."""
    try:
        with open(_loop_pass_runs_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _record_pass_run(pass_name: str) -> None:
    """Stamp `pass_name` as having run now. Atomic replace; best-effort (a failed
    stamp just means the pass may re-run sooner than intended — harmless)."""
    try:
        runs = _read_pass_runs()
        runs[pass_name] = datetime.now(timezone.utc).isoformat()
        path = _loop_pass_runs_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(runs, f)
        os.replace(tmp, path)
    except OSError as e:
        logger.debug("Could not record pass-run for %s: %s", pass_name, e)


def _due(pass_name: str, min_interval_s: float) -> bool:
    """True if `pass_name` has never run or last ran >= min_interval_s ago.
    Fail-open: an unparseable timestamp reads as due."""
    last = _read_pass_runs().get(pass_name)
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - last_dt).total_seconds() >= min_interval_s


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


def has_distill_work(core_db: Optional[str], threshold: int, stale_days: int) -> bool:
    """SQL check: are there enough completed tasks (with a result, aged past
    stale_days) to bother distilling into procedures? Mirrors
    memory_distill_procedures_impl's selection so the loop only spends a model
    call when there's real work (event-driven, backend-agnostic via dialect)."""
    try:
        from memory.backends import dialect
        _d = dialect()
        _p = _d.param()
        clause = ""
        params: tuple = (threshold,)
        if stale_days > 0:
            clause = f" AND completed_at IS NOT NULL AND completed_at < {_d.now_minus_days(_p)}"
            params = (int(stale_days), threshold)
        sql = (
            "SELECT 1 FROM tasks "
            "WHERE state='completed' AND deleted_at IS NULL "
            "AND result_memory_id IS NOT NULL" + clause + " "
            f"GROUP BY state HAVING COUNT(*) >= {_p} LIMIT 1"
        )
        return len(_probe_core(core_db, sql, params)) > 0
    except Exception as e:
        logger.debug(f"Distill work check failed (non-fatal): {e}")
        return False  # conservative: no model work unless we can confirm some


async def run_distill_pass(args):
    """Distill successful task runs into reusable 'procedure' memories —
    governor-gated, event-driven. Only fires when enough completed tasks exist
    AND M3_DISTILL_AUTO=1 is set (the job itself enforces the dry-run-unless-
    opted-in + activity-yield contract). Delegates to distill_procedures._run so
    the loop and the standalone cron/CLI share ONE implementation."""
    if not has_distill_work(args.database, args.distill_threshold, args.distill_stale_days):
        logger.debug("No distillation work (no completed tasks over threshold). Skipping.")
        return
    logger.info("Starting Procedural Distillation pass...")
    try:
        import distill_procedures
        out = await distill_procedures._run(
            apply=True,  # the job gates real writes on M3_DISTILL_AUTO + idle
            threshold=args.distill_threshold,
            stale_days=args.distill_stale_days,
            max_procedures=args.distill_max_procedures,
        )
        logger.info("Distillation pass: %s", out.strip().replace("\n", " | "))
    except Exception as e:
        logger.error(f"Distillation pass error: {type(e).__name__}: {e}")


def has_chatlog_prune_work(chatlog_db: Optional[str], prune_days: float,
                           min_rows: int) -> bool:
    """SQL check: are there enough aged chat_log turns to bother pruning?
    Event-driven gate (mirrors has_consolidate_work) so the loop only does a
    sweep when a real backlog of prune-eligible noise has accumulated."""
    try:
        from memory.backends import active_backend, chatlog_table, dialect
        _d = dialect()
        _p = _d.param()
        _is_sqlite = active_backend().name == "sqlite"
        _T = chatlog_table("items")  # memory_items (sqlite) | chat_log_items (pg)
        # CONVERGENCE: the sweep decays a noise row ONCE (lowers importance to
        # ~0.06, sets valid_to). A decayed row keeps importance<=0.3, so a gate
        # keyed only on that stays True forever after the backlog drains —
        # re-firing the pass every cycle to do nothing (part of the "same rows
        # over and over" loop). Count only rows the sweep can still ACT on:
        # not-yet-decayed (valid_to open). Cross-backend "open" test — SQLite
        # stores an unset bound as NULL OR '' (loose TEXT typing); Postgres/
        # MariaDB use NULL only — so a bare `valid_to IS NULL` would miss SQLite's
        # '' and undercount. Guard on the column existing (older schema/backend
        # without valid_to falls back to the importance-only gate, matching that
        # schema's sweep behaviour).
        ctx = M3Context.for_db(None)
        with ctx.get_chatlog_conn() as conn:
            _has_valid_to = False
            try:
                _cs, _cp = _d.columns_of(_T)
                _has_valid_to = any(r[0] == "valid_to" for r in conn.execute(_cs, _cp).fetchall())
            except Exception:  # noqa: BLE001 — introspection failure degrades to importance-only
                _has_valid_to = False
            # "not-yet-decayed" = valid_to is open. No extra bind params: the
            # clause is a literal comparison the backend evaluates directly.
            #   SQLite : valid_to open == NULL OR '' (loose TEXT typing)
            #   PG/MDB : valid_to open == NULL only (TIMESTAMPTZ; '' is illegal,
            #            so referencing = '' there would error — hence the branch)
            _open_clause = ""
            if _has_valid_to:
                if _is_sqlite:
                    _open_clause = " AND (valid_to IS NULL OR valid_to = '')"
                else:
                    _open_clause = " AND valid_to IS NULL"
            sql = (
                f"SELECT 1 FROM {_T} "
                f"WHERE type='chat_log' AND is_deleted=0 AND importance <= 0.3 "
                f"AND created_at < {_d.now_minus_days(_p)} "
                f"{_open_clause} "
                f"GROUP BY type HAVING COUNT(*) > {_p} LIMIT 1"
            )
            # now_minus_days binds an INT; only prune_days + min_rows are params.
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


async def run_sync_pass(args):
    """Warehouse sync (SQLite -> CDW PostgreSQL) — governor-paced, time-gated.

    Delegates to sync_all.run_pg_sync so the loop and the standalone
    AgentOS_HourlySync task share ONE implementation. Time-driven (delta sync has
    no queryable backlog), so gated by _due("sync", interval) — a sync that ran
    recently is skipped without a network round-trip. NOTE: the OS HourlySync task
    is DELIBERATELY KEPT as a load-independent floor — when the governor HALTs
    background passes under sustained CPU/RAM pressure this pass is deferred, so a
    rigid hourly task guarantees the warehouse can't drift stale indefinitely
    (same reasoning as SecretRotator staying scheduled)."""
    # sync_all/pg_sync is the SQLite -> CDW warehouse bridge (it opens the local
    # side as sqlite3). On a PostgreSQL-PRIMARY deployment there is no local
    # SQLite store to fan out, so this pass is a no-op — skip cleanly (the PG
    # primary is its own shared store; CDW fan-in doesn't apply). Backend-agnostic
    # via the same active_backend() seam the other passes use.
    try:
        from memory.backends import active_backend
        if active_backend().name != "sqlite":
            logger.debug("Sync pass: primary backend is %s (not sqlite) — no local "
                         "store to sync to CDW. Skipping.", active_backend().name)
            return
    except Exception as e:
        logger.debug("Sync pass: backend probe failed (%s); assuming sqlite.", e)
    if not _due("sync", args.sync_min_interval_s):
        logger.debug("Sync not due (ran < %ss ago). Skipping.", args.sync_min_interval_s)
        return
    try:
        import sync_all
        # Cheap pre-flight: if the warehouse isn't reachable, skip fast (don't
        # stamp — so we retry next cycle rather than waiting a full interval).
        if not sync_all.TARGET_IP:
            logger.debug("Sync pass: no warehouse target configured. Skipping.")
            return
        if not sync_all.is_reachable(sync_all.TARGET_IP):
            logger.info("Sync pass: warehouse %s unreachable — skipping (retry next cycle).",
                        sync_all.TARGET_IP)
            return
        logger.info("Starting warehouse-sync pass...")
        ok = await asyncio.to_thread(sync_all.run_pg_sync, False)
        # Stamp only on a completed attempt (reachable + ran) so the interval
        # gate paces real syncs, not skipped ones.
        _record_pass_run("sync")
        logger.info("Warehouse-sync pass complete (ok=%s).", ok)
    except Exception as e:
        logger.error(f"Sync pass error: {type(e).__name__}: {e}")


async def run_maintenance_pass(args):
    """Memory maintenance (importance/confidence decay, orphan prune, retention)
    — governor-paced, time-gated. Delegates to memory_maintenance_impl so the loop
    and the standalone AgentOS_Maintenance task share ONE implementation. The impl
    is a no-op when nothing is decay/prune-eligible, so the _due gate is a coarse
    cadence limiter (avoid rewriting decay every tick), not the work-probe."""
    if not _due("maintenance", args.maintenance_min_interval_s):
        logger.debug("Maintenance not due (ran < %ss ago). Skipping.",
                     args.maintenance_min_interval_s)
        return
    try:
        import memory_maintenance
        logger.info("Starting memory-maintenance pass...")
        summary = await asyncio.to_thread(memory_maintenance.memory_maintenance_impl)
        _record_pass_run("maintenance")
        logger.info("Memory-maintenance pass complete: %s", summary)
    except Exception as e:
        logger.error(f"Maintenance pass error: {type(e).__name__}: {e}")


async def run_audit_pass(args):
    """Weekly audit report — governor-paced, time-gated (~7d). Delegates to
    weekly_auditor.run_audit so the loop and the standalone AgentOS_WeeklyAuditor
    task share ONE implementation. Purely time-driven (no backlog), so _due("audit",
    ~7d) IS the work-probe: a no-op until a week has elapsed."""
    if not _due("audit", args.audit_min_interval_s):
        logger.debug("Audit not due (ran < %ss ago). Skipping.", args.audit_min_interval_s)
        return
    try:
        import weekly_auditor
        logger.info("Starting weekly-audit pass...")
        result = await asyncio.to_thread(weekly_auditor.run_audit)
        _record_pass_run("audit")
        logger.info("Weekly-audit pass complete: %s", result)
    except Exception as e:
        logger.error(f"Audit pass error: {type(e).__name__}: {e}")


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

    # Register as a live DB-holder so an exclusive op (migration/backup) can
    # discover us and wait for us to quiesce. Deregistered on clean exit; also
    # deregistered/re-registered around a HALT pause below (see HALT_PROTOCOL.md).
    m3_halt.register_process(_HALT_ROLE)
    atexit.register(m3_halt.deregister, _HALT_ROLE)

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
        # ── Cooperative quiesce (HALT_m3) ──────────────────────────────────────
        # An exclusive op (migration/backup/repair) raised the halt: flush our WAL
        # to disk (§10), drop out of the PID registry so we no longer count as a
        # DB-holder, and spin-wait WITHOUT opening any connection until it clears.
        # Because we open DBs per-pass (not held across the wait), checkpointing +
        # not-reopening fully releases the DB for the exclusive op. We pause the
        # process, never exit — nothing needs to restart us. See HALT_PROTOCOL.md.
        # Backend-blind: halt/registry are file+PID ops; _checkpoint_wal self-no-ops
        # on PostgreSQL (PG manages its own WAL), so this runs on both backends.
        if m3_halt.halt_is_active(role=_HALT_ROLE):
            logger.info("HALT_m3 active — checkpointing and pausing cognitive loop "
                        "for a DB-exclusive operation.")
            _checkpoint_wal(args.database)
            m3_halt.deregister(_HALT_ROLE)
            try:
                while m3_halt.halt_is_active(role=_HALT_ROLE) and not _STOP_EVENT.is_set():
                    await asyncio.sleep(2.0)
            finally:
                # Re-register on resume (or on stop, so a clean exit still
                # deregisters via atexit rather than leaking).
                m3_halt.register_process(_HALT_ROLE)
            if _STOP_EVENT.is_set():
                break
            logger.info("HALT_m3 cleared — resuming cognitive loop.")
            continue

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
        # Distillation's default model is local (M3_DISTILL_MODEL=slm/llm); it's
        # only non-local when pointed at a cloud profile, so gate on the GPU by
        # default (mirrors consolidate). A cloud endpoint won't touch the GPU,
        # but treating it as local here only makes the loop slightly more
        # conservative under GPU load, never wrong.
        distill_local = True

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
            {"name": "distill",    "skip": args.skip_distill,       "gpu": distill_local,     "sets_limit": False, "run": run_distill_pass},
            {"name": "prune",      "skip": args.skip_chatlog_prune, "gpu": None,              "sets_limit": False, "run": run_chatlog_prune_pass},
            # Time-driven, non-GPU passes (CPU/RAM-gated like prune). Each is
            # cheap when not due (interval gate + delta-sync / no-op impl), so
            # they ride the round-robin without adding steady load. Sync also has
            # a kept OS-task floor (see run_sync_pass) for when the governor HALTs.
            {"name": "sync",       "skip": args.skip_sync,          "gpu": None,              "sets_limit": False, "run": run_sync_pass},
            {"name": "maintenance","skip": args.skip_maintenance,   "gpu": None,              "sets_limit": False, "run": run_maintenance_pass},
            {"name": "audit",      "skip": args.skip_audit,         "gpu": None,              "sets_limit": False, "run": run_audit_pass},
        ]
        # Round-robin order + idle-aware intensity + queue-awareness (see
        # _select_pass_order). When the governor is THROTTLED we treat the host as
        # user-active and run only the rotated leader this cycle; otherwise every
        # pass runs. Rotation makes sure the leader differs each cycle so no pass is
        # starved.
        active = pacing_full.get("background") == "THROTTLED"
        # QUEUE-AWARE LEADER (throttled only): under throttle exactly one pass runs,
        # so we must not spend that single slot on a pass whose queue is empty while
        # a backlogged pass waits another full cycle. Probe the cheap queue-gates
        # for the queue-backed passes ONLY when throttled (idle mode runs every pass
        # and each pass's own gate skips empties — no extra probing needed there).
        # The time-driven passes (sync/maintenance/audit) are intentionally OMITTED
        # from this map so _select_pass_order keeps them (their gate is "is it
        # due?", checked inside the pass, not a queue depth). A probe error is
        # fail-open (treated as "has work") via the has_* helpers' own except paths.
        work_map: "dict[str, bool] | None" = None
        if active:
            work_map = {
                "entities":    not args.skip_entities and has_entity_work(args.database, args.chatlog_db),
                "enrich":      not args.skip_enrich and has_enrich_work(args.database),
                "embed":       not args.skip_embed and has_embed_work(args.database),
                "classify":    not args.skip_classify and has_classify_work(args.database),
                "consolidate": not args.skip_consolidate and has_consolidate_work(
                    args.database, args.consolidate_source_type,
                    args.consolidate_threshold, args.consolidate_stale_days),
                "distill":     not args.skip_distill and has_distill_work(
                    args.database, args.distill_threshold, args.distill_stale_days),
                "prune":       not args.skip_chatlog_prune and has_chatlog_prune_work(
                    args.chatlog_db, args.chatlog_prune_days, args.chatlog_prune_threshold),
            }
        order = _select_pass_order(passes, _cycle, active, work_map)
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
    parser.add_argument("--limit-per-pass", type=int,
                        default=max(1, int(os.environ.get("M3_LIMIT_PER_PASS", "4"))),
                        help="Max groups/rows per heavy-LLM pass (entity extraction, "
                             "enrichment, observation drain). Default 4 (override with the "
                             "M3_LIMIT_PER_PASS env var — e.g. set it higher to drain a large "
                             "catch-up backlog faster on an idle box, WITHOUT editing the flag "
                             "in a scheduled task). Small enough that one pass is a few-second "
                             "GPU burst (the governor is only re-checked BETWEEN passes, not "
                             "within a batch — a 50-item pass once pinned the GPU for ~17 min, "
                             "so 4 stays ~10x under that). Under THROTTLED load this is shrunk to "
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

    # Procedural distillation pass. Event-driven + governor-gated inside the
    # loop; the job still requires M3_DISTILL_AUTO=1 to actually write (else
    # dry-run). Rolls up completed task runs → reusable 'procedure' memories.
    parser.add_argument("--skip-distill", action="store_true",
                        help="Skip the procedural-distillation pass")
    parser.add_argument("--distill-threshold", type=int, default=1,
                        help="Min completed tasks before distilling (default: 1)")
    parser.add_argument("--distill-stale-days", type=int, default=3,
                        help="Only distill tasks completed > N days ago (default: 3)")
    parser.add_argument("--distill-max-procedures", type=int, default=20,
                        help="Max procedures written per distillation run (default: 20)")

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

    # Time-driven passes (governor-paced, interval-gated). These replace the
    # standalone AgentOS_HourlySync / _Maintenance / _WeeklyAuditor scheduled
    # tasks by running them in the round-robin under governor pacing. The OS
    # HourlySync task is kept as a load-independent floor (see run_sync_pass).
    parser.add_argument("--skip-sync", action="store_true",
                        help="Skip the warehouse-sync pass")
    parser.add_argument("--sync-min-interval-s", type=float, default=3600.0,
                        help="Min seconds between warehouse syncs in-loop (default: 3600)")
    parser.add_argument("--skip-maintenance", action="store_true",
                        help="Skip the memory-maintenance (decay/prune) pass")
    parser.add_argument("--maintenance-min-interval-s", type=float, default=3600.0,
                        help="Min seconds between maintenance passes (default: 3600)")
    parser.add_argument("--skip-audit", action="store_true",
                        help="Skip the weekly-audit pass")
    parser.add_argument("--audit-min-interval-s", type=float, default=7 * 86400.0,
                        help="Min seconds between audit passes (default: 604800 = 7d)")

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
