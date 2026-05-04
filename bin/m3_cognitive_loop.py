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
import sys
import time
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
_BIN = REPO_ROOT / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import m3_enrich
import m3_entities
import memory_core as mc
import chatlog_config
from m3_sdk import M3Context, resolve_db_path

# PID file path for single-instance locking
PID_FILE = REPO_ROOT / "memory" / "cognitive_loop.pid"

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

# Global stop signal for graceful shutdown
_STOP_EVENT = asyncio.Event()

def _signal_handler():
    logger.info("Shutdown signal received. Gracefully stopping...")
    _STOP_EVENT.set()

def has_entity_work(core_db: Optional[str], chatlog_db: Optional[str]) -> bool:
    """SQL check: Are there rows in memory_items that don't have entities yet?"""
    try:
        # 1. Check Core DB
        ctx_core = M3Context(core_db)
        sql = """
            SELECT 1 FROM memory_items mi
            LEFT JOIN memory_item_entities mie ON mi.id = mie.memory_id
            WHERE mi.is_deleted = 0 
              AND mi.type IN ('message', 'chat_log', 'note', 'observation')
              AND mie.memory_id IS NULL
            LIMIT 1
        """
        if len(ctx_core.query_memory(sql)) > 0:
            return True
            
        # 2. Check Chatlog DB (if separate)
        if chatlog_db and os.path.abspath(chatlog_db) != os.path.abspath(ctx_core.db_path):
            ctx_chat = M3Context(chatlog_db)
            if len(ctx_chat.query_memory(sql)) > 0:
                return True
                
        return False
    except Exception as e:
        logger.debug(f"Entity work check failed (non-fatal): {e}")
        return True # Default to True to be safe

def has_enrich_work(core_db: Optional[str]) -> bool:
    """SQL check: Is the observation_queue or reflector_queue non-empty?"""
    try:
        # Queues always live in the CORE memory DB
        ctx = M3Context(core_db)
        res_obs = ctx.query_memory("SELECT 1 FROM observation_queue LIMIT 1")
        res_ref = ctx.query_memory("SELECT 1 FROM reflector_queue LIMIT 1")
        return len(res_obs) > 0 or len(res_ref) > 0
    except Exception as e:
        logger.debug(f"Enrich work check failed (non-fatal): {e}")
        return True

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
            yes=True
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

async def main_loop(args):
    """Main execution loop with adaptive backoff and signal awareness."""
    logger.info(f"Cognitive Loop heartbeat started. Interval: {args.interval}s")
    
    # Register signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    while not _STOP_EVENT.is_set():
        start_time = time.monotonic()
        
        if not args.skip_entities:
            await run_entity_pass(args)
            if _STOP_EVENT.is_set(): break
            
        if not args.skip_enrich:
            await run_enrich_pass(args)
            if _STOP_EVENT.is_set(): break
            
        elapsed = time.monotonic() - start_time
        wait_time = max(0, args.interval - elapsed)
        
        if _STOP_EVENT.is_set():
            break
            
        try:
            await asyncio.wait_for(_STOP_EVENT.wait(), timeout=wait_time)
        except asyncio.TimeoutError:
            pass

    logger.info("Cognitive Loop stopped.")

def main():
    acquire_lock()
    
    env_interval = os.environ.get("M3_COGNITIVE_LOOP_INTERVAL")
    default_interval = int(env_interval) if env_interval and env_interval.isdigit() else 300

    parser = argparse.ArgumentParser(description="m3-memory Cognitive Loop")
    parser.add_argument("--interval", type=int, default=default_interval, 
                        help=f"Seconds between passes (default: {default_interval})")
    parser.add_argument("--concurrency", type=int, default=2, help="SLM concurrency (default: 2)")
    parser.add_argument("--limit-per-pass", type=int, default=50, help="Max groups/rows per pass (default: 50)")
    
    # Database knobs
    parser.add_argument("--database", default=None, help="Core Memory DB path (Env: M3_DATABASE)")
    parser.add_argument("--chatlog-db", default=None, help="Chatlog DB path (Env: CHATLOG_DB_PATH)")
    
    parser.add_argument("--profile-entities", default="entities_local_qwen", help="Profile for entities")
    parser.add_argument("--profile-enrich", default="enrich_local_qwen", help="Profile for enrichment")
    parser.add_argument("--reflector-threshold", type=int, default=5, help="Min observations before Reflector (default: 5)")
    parser.add_argument("--skip-entities", action="store_true", help="Skip entity extraction")
    parser.add_argument("--skip-enrich", action="store_true", help="Skip enrichment pass")
    parser.add_argument("--no-reflect", action="store_true", help="Skip reflection pass")

    args = parser.parse_args()

    # Resolve paths once to normalize env vs flag
    args.database = resolve_db_path(args.database)
    args.chatlog_db = chatlog_config.chatlog_db_path(args.chatlog_db)

    try:
        asyncio.run(main_loop(args))
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
