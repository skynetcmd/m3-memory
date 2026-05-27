#!/usr/bin/env python3
import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format='%(name)s: [%(levelname)s] %(message)s')
logger = logging.getLogger("memory_doctor")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "bin"))
from m3_sdk import add_database_arg, resolve_db_path


def fix_missing_timestamps(conn):
    """Ensures all items have at least a created_at timestamp."""
    logger.info("Checking for missing timestamps...")
    now = datetime.now(timezone.utc).isoformat() + "Z"

    # Fix items with NULL created_at
    res = conn.execute(
        "UPDATE memory_items SET created_at = ? WHERE created_at IS NULL",
        (now,)
    )
    if res.rowcount:
        logger.info(f"Fixed {res.rowcount} items with missing created_at.")

def validate_relationships(conn):
    """Prunes relationships pointing to non-existent items."""
    logger.info("Validating relationship integrity...")
    res = conn.execute("""
        DELETE FROM memory_relationships
        WHERE from_id NOT IN (SELECT id FROM memory_items)
           OR to_id NOT IN (SELECT id FROM memory_items)
    """)
    if res.rowcount:
        logger.info(f"Pruned {res.rowcount} orphaned relationships.")

def fix_metadata_json(conn):
    """Ensures metadata_json is valid JSON."""
    logger.info("Validating metadata JSON strings...")
    cursor = conn.execute("SELECT id, metadata_json FROM memory_items WHERE metadata_json IS NOT NULL")
    rows = cursor.fetchall()
    fixed = 0
    for rid, meta in rows:
        try:
            if meta:
                json.loads(meta)
        except json.JSONDecodeError:
            logger.warning(f"Repairing invalid JSON for item {rid}")
            conn.execute("UPDATE memory_items SET metadata_json = '{}' WHERE id = ?", (rid,))
            fixed += 1
    if fixed:
        logger.info(f"Repaired {fixed} items with invalid metadata JSON.")

def check_sovereign_embedder():
    """Checks the status of the integrated sovereign embedder."""
    logger.info("Checking Sovereign Embedder status...")
    import asyncio

    # Import locally to avoid circular dependencies if any
    import memory_core

    status = asyncio.run(memory_core.embedder_status_impl())

    if status["binary_found"]:
        logger.info("✓ Sovereign binary found.")
    else:
        logger.info("- Sovereign binary not installed (optional).")

    if status["status"] == "online":
        logger.info(f"✓ Sovereign server is ONLINE on port {status['port']}.")
        if status["models"]:
            model_ids = [m["id"] for m in status["models"]]
            logger.info(f"  Loaded models: {', '.join(model_ids)}")
    elif status["binary_found"]:
        stat_str = status["status"].upper()
        logger.warning(f"⚠ Sovereign binary exists but server is {stat_str} on port {status['port']}.")
        logger.info("  Tip: Run `mcp-memory embedder start` to boot it.")


def run_cascade_doctor() -> int:
    """B16: delegate to the canonical embedding-cascade diagnostic in
    memory.doctor.memory_doctor_impl. Probes tier-1 GGUF + tier-2 :8082 +
    DB + roundtrip, returns structured dict — same impl the MCP
    `memory_doctor` tool calls.

    Returns 0 on summary in {healthy, degraded}, 1 on broken.
    """
    try:
        import asyncio
        # bin/ is the sys.path entry; memory.doctor is the submodule.
        from memory.doctor import memory_doctor_impl
    except Exception as e:
        logger.warning(
            f"cascade doctor unavailable (memory.doctor not importable): "
            f"{type(e).__name__}: {e}"
        )
        return 0  # not fatal — legacy DB-repair path still ran
    try:
        out = asyncio.run(memory_doctor_impl())
    except Exception as e:
        logger.error(f"cascade doctor crashed: {type(e).__name__}: {e}")
        return 1

    print()
    print("=== embedding-cascade health (memory_doctor) ===")
    print(f"  summary  : {out.get('summary')}")
    print(f"  tier_1   : {out.get('tier_1', {}).get('status')}")
    print(f"  tier_2   : {out.get('tier_2', {}).get('status')}"
          f"  ({out.get('tier_2', {}).get('url')})")
    print(f"  db       : {out.get('db', {}).get('status')}")
    print(f"  roundtrip: {out.get('roundtrip', {}).get('status')}"
          f"  latency={out.get('roundtrip', {}).get('latency_ms')}ms")
    for issue in out.get("issues", []):
        print(f"  ISSUE: {issue}")
    for rec in out.get("recommendations", []):
        print(f"  TIP:   {rec}")
    return 0 if out.get("summary") != "broken" else 1


def run_embed_server_doctor() -> int:
    """B16: invoke `m3-embed-server doctor` for the Rust-side full-stack
    health check (config, service status, GGUF discovery, log tail).

    Best-effort: if the binary isn't on PATH, skip silently. Returns the
    subprocess exit code (0 = pass, 1 = some critical probe failed).
    """
    import shutil
    import subprocess as _sp
    bin_name = "m3-embed-server.exe" if sys.platform == "win32" else "m3-embed-server"
    exe = shutil.which(bin_name)
    if not exe:
        logger.debug(f"m3-embed-server not on PATH; skipping Rust-side doctor")
        return 0
    print()
    print("=== Rust-side service health (m3-embed-server doctor) ===")
    try:
        r = _sp.run([exe, "doctor"], capture_output=False, text=True, timeout=30)
        return r.returncode
    except _sp.TimeoutExpired:
        print("  m3-embed-server doctor timed out after 30s")
        return 1
    except Exception as e:
        print(f"  m3-embed-server doctor failed: {type(e).__name__}: {e}")
        return 1


def main():
    parser = argparse.ArgumentParser(
        description="Memory health check and repair. B16-unified: runs "
                    "legacy DB repair + canonical cascade doctor + Rust-side "
                    "m3-embed-server doctor.",
    )
    add_database_arg(parser)
    parser.add_argument(
        "--skip-repair", action="store_true",
        help="Skip the legacy DB-repair phase (fix_missing_timestamps, "
             "validate_relationships, fix_metadata_json). Useful if you only "
             "want a read-only health check.",
    )
    parser.add_argument(
        "--skip-cascade", action="store_true",
        help="Skip the embedding-cascade health check (memory.doctor).",
    )
    parser.add_argument(
        "--skip-embed-server", action="store_true",
        help="Skip the Rust-side m3-embed-server doctor subprocess.",
    )
    args = parser.parse_args()

    exit_code = 0

    # ── Legacy DB repair (kept verbatim — behavior change later if needed) ──
    if not args.skip_repair:
        db_path = resolve_db_path(args.database)
        if not os.path.exists(db_path):
            logger.error(f"Database not found at {db_path}")
            sys.exit(1)

        conn = sqlite3.connect(db_path)
        try:
            check_sovereign_embedder()
            fix_missing_timestamps(conn)
            validate_relationships(conn)
            fix_metadata_json(conn)
            conn.commit()
            logger.info("Memory health check and repair completed.")
        except Exception as e:
            logger.error(f"Doctor failed: {e}")
            conn.rollback()
            exit_code = 1
        finally:
            conn.close()

    # ── B16: cascade health probe (delegates to memory.doctor.memory_doctor_impl) ──
    if not args.skip_cascade:
        rc = run_cascade_doctor()
        exit_code = max(exit_code, rc)

    # ── B16: Rust-side service doctor ──
    if not args.skip_embed_server:
        rc = run_embed_server_doctor()
        # Don't bump exit_code on Rust doctor failure — service may not be
        # installed everywhere and that's not a Python-side failure.

    sys.exit(exit_code)

if __name__ == "__main__":
    main()
