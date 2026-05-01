#!/usr/bin/env python3
"""CLI script to report ChromaDB sync health metrics.

Provides visibility into the ChromaDB bi-directional sync system by querying
local SQLite tables and pinging the remote ChromaDB instance. Read-only; safe
for cron.

Usage:
    python bin/chroma_health.py                    # human-readable summary
    python bin/chroma_health.py --json             # JSON output
    python bin/chroma_health.py --check            # exit 0 (ok), 1 (warn), 2 (critical)
    python bin/chroma_health.py --quiet            # suppress info; show problems only

Can be wired into:
    - sync_all.py (call at end of sync to log health)
    - Windows Scheduled Task / cron job
    - Manual ad-hoc invocation
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx

# ── Constants ─────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(BASE_DIR / "bin"))

# Thresholds (from specification)
QUEUE_WARN_THRESHOLD = int(os.environ.get("M3_CHROMA_SYNC_QUEUE_WARN", 100000))
QUEUE_MAX_THRESHOLD = int(os.environ.get("M3_CHROMA_SYNC_QUEUE_MAX", 500000))
SYNC_STALE_HOURS = 4

# Environment / defaults
CHROMA_BASE_URL = os.environ.get("CHROMA_BASE_URL", "")
CHROMA_COLLECTION = "agent_memory"
CHROMA_V2_PREFIX = "/api/v2/tenants/default_tenant/databases/default_database/collections"
CHROMA_CONNECT_TIMEOUT = 3.0

# Database
DEFAULT_DB_PATH = BASE_DIR / "memory" / "agent_memory.db"
M3_DATABASE = os.environ.get("M3_DATABASE", str(DEFAULT_DB_PATH))

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger("chroma_health")


@contextmanager
def _get_db(db_path: str):
    """Simple context manager for SQLite DB."""
    import sqlite3

    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _table_exists(db, table_name: str) -> bool:
    """Check if a table exists in the SQLite DB."""
    res = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return res is not None


def _get_queue_size() -> int:
    """Get count of items in chroma_sync_queue."""
    try:
        with _get_db(M3_DATABASE) as db:
            if not _table_exists(db, "chroma_sync_queue"):
                return 0
            row = db.execute("SELECT COUNT(*) as cnt FROM chroma_sync_queue").fetchone()
            return row["cnt"] if row else 0
    except Exception as e:
        logger.warning(f"Failed to query queue size: {e}")
        return 0


def _get_mirror_size_and_timestamp() -> tuple[int, str | None]:
    """Get count of items in chroma_mirror and last updated timestamp."""
    try:
        with _get_db(M3_DATABASE) as db:
            if not _table_exists(db, "chroma_mirror"):
                return 0, None
            row = db.execute(
                "SELECT COUNT(*) as cnt, MAX(pulled_at) as last_pulled FROM chroma_mirror"
            ).fetchone()
            cnt = row["cnt"] if row else 0
            last_pulled = row["last_pulled"] if row else None
            return cnt, last_pulled
    except Exception as e:
        logger.warning(f"Failed to query mirror size: {e}")
        return 0, None


def _get_conflict_count() -> int:
    """Get count of unresolved conflicts in sync_conflicts table."""
    try:
        with _get_db(M3_DATABASE) as db:
            if not _table_exists(db, "sync_conflicts"):
                return 0
            row = db.execute(
                "SELECT COUNT(*) as cnt FROM sync_conflicts WHERE resolution = 'pending'"
            ).fetchone()
            return row["cnt"] if row else 0
    except Exception as e:
        logger.warning(f"Failed to query conflict count: {e}")
        return 0


def _get_last_sync_timestamp() -> str | None:
    """Get last sync run timestamp from logs/sync_all.log if available."""
    try:
        log_file = BASE_DIR / "logs" / "sync_all.log"
        if not log_file.exists():
            return None
        # Read last 50 lines to find the most recent sync_all complete message
        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()[-50:]
        for line in reversed(lines):
            if "sync_all complete" in line or "sync_all starting" in line:
                # Extract timestamp from line (format: "2026-04-26 10:00:00,123")
                parts = line.split(" ")
                if len(parts) >= 2:
                    date_part = parts[0]
                    time_part = parts[1].split(",")[0]
                    try:
                        dt = datetime.fromisoformat(f"{date_part}T{time_part}")
                        return dt.replace(tzinfo=timezone.utc).isoformat()
                    except ValueError:
                        pass
        return None
    except Exception as e:
        logger.warning(f"Failed to read sync log: {e}")
        return None


def _get_sync_status() -> tuple[str, str]:
    """Determine sync status from log. Returns (last_run_utc, status)."""
    last_run = _get_last_sync_timestamp()
    if not last_run:
        return None, "unknown"

    try:
        last_run_dt = datetime.fromisoformat(last_run)
        now = datetime.now(timezone.utc)
        hours_ago = (now - last_run_dt).total_seconds() / 3600

        if hours_ago > SYNC_STALE_HOURS:
            return last_run, "warn"
        else:
            return last_run, "ok"
    except Exception:
        return last_run, "unknown"


async def _check_chroma_health() -> tuple[bool, str, int]:
    """
    Check if ChromaDB is reachable and measure heartbeat latency.
    Returns (reachable, version, latency_ms).
    """
    if not CHROMA_BASE_URL:
        return False, "unknown", -1

    try:
        async with httpx.AsyncClient() as client:
            # Ping heartbeat endpoint
            start = datetime.now(timezone.utc)
            resp = await client.get(
                f"{CHROMA_BASE_URL}/api/v2/heartbeat", timeout=CHROMA_CONNECT_TIMEOUT
            )
            elapsed_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)

            if resp.status_code == 200:
                # Try to get version
                try:
                    version_resp = await client.get(
                        f"{CHROMA_BASE_URL}/api/v2/version", timeout=CHROMA_CONNECT_TIMEOUT
                    )
                    version = "0.6.3"  # default fallback
                    if version_resp.status_code == 200:
                        data = version_resp.json()
                        version = data.get("version", version)
                    return True, version, elapsed_ms
                except Exception:
                    return True, "unknown", elapsed_ms
            else:
                return False, "unknown", elapsed_ms
    except (httpx.TimeoutException, httpx.ConnectError, Exception) as e:
        logger.debug(f"ChromaDB check failed: {e}")
        return False, "unknown", -1


def _determine_status(
    queue_size: int,
    conflict_count: int,
    remote_reachable: bool,
    last_sync_status: str,
    last_sync_hours_ago: float | None,
) -> str:
    """Determine overall health status based on metrics."""
    # critical: queue > max OR (remote unreachable AND queue > 1000)
    if queue_size > QUEUE_MAX_THRESHOLD:
        return "critical"
    if not remote_reachable and queue_size > 1000:
        return "critical"

    # warn: queue > warn_threshold OR conflicts > 0 OR (last_sync > 4h AND remote reachable)
    if queue_size > QUEUE_WARN_THRESHOLD:
        return "warn"
    if conflict_count > 0:
        return "warn"
    if (
        remote_reachable
        and last_sync_hours_ago is not None
        and last_sync_hours_ago > SYNC_STALE_HOURS
    ):
        return "warn"

    # ok: all nominal
    return "ok"


async def gather_health() -> dict:
    """Gather all health metrics."""
    timestamp_utc = datetime.now(timezone.utc).isoformat()

    # Queue
    queue_size = _get_queue_size()

    # Mirror
    mirror_size, mirror_last_pulled = _get_mirror_size_and_timestamp()

    # Remote
    remote_reachable, remote_version, remote_heartbeat_ms = await _check_chroma_health()

    # Sync
    last_sync_utc, last_sync_status = _get_sync_status()
    last_sync_hours_ago = None
    if last_sync_utc:
        try:
            last_sync_dt = datetime.fromisoformat(last_sync_utc)
            now = datetime.now(timezone.utc)
            last_sync_hours_ago = (now - last_sync_dt).total_seconds() / 3600
        except Exception:
            pass

    # Conflicts
    conflict_count = _get_conflict_count()

    # Determine overall status
    overall_status = _determine_status(
        queue_size,
        conflict_count,
        remote_reachable,
        last_sync_status,
        last_sync_hours_ago,
    )

    return {
        "timestamp_utc": timestamp_utc,
        "queue": {
            "size": queue_size,
            "warn_threshold": QUEUE_WARN_THRESHOLD,
            "max_threshold": QUEUE_MAX_THRESHOLD,
            "status": "ok" if queue_size <= QUEUE_WARN_THRESHOLD else ("critical" if queue_size > QUEUE_MAX_THRESHOLD else "warn"),
        },
        "mirror": {
            "size": mirror_size,
            "last_pulled_utc": mirror_last_pulled,
        },
        "remote": {
            "reachable": remote_reachable,
            "heartbeat_ms": remote_heartbeat_ms if remote_heartbeat_ms >= 0 else None,
            "version": remote_version,
        },
        "sync": {
            "last_run_utc": last_sync_utc,
            "last_status": last_sync_status,
        },
        "conflicts": {
            "count": conflict_count,
        },
        "overall_status": overall_status,
    }


def format_human_readable(health: dict) -> str:
    """Format health dict as human-readable text."""
    lines = []
    lines.append("ChromaDB Sync Health Report")
    lines.append(f"Timestamp: {health['timestamp_utc']}")
    lines.append("")

    # Queue
    queue = health["queue"]
    queue_status = queue["status"]
    lines.append(f"Queue: {queue['size']:,} items [{queue_status.upper()}]")
    lines.append(
        f"  Thresholds: warn={queue['warn_threshold']:,}, critical={queue['max_threshold']:,}"
    )

    # Mirror
    mirror = health["mirror"]
    lines.append(f"Mirror: {mirror['size']:,} items")
    if mirror["last_pulled_utc"]:
        lines.append(f"  Last pulled: {mirror['last_pulled_utc']}")

    # Remote
    remote = health["remote"]
    if remote["reachable"]:
        latency_str = (
            f"{remote['heartbeat_ms']}ms" if remote["heartbeat_ms"] is not None else "N/A"
        )
        lines.append(f"Remote: REACHABLE (v{remote['version']}, {latency_str})")
    else:
        lines.append("Remote: UNREACHABLE")

    # Sync
    sync = health["sync"]
    lines.append(f"Last sync: {sync['last_run_utc'] or 'unknown'} [{sync['last_status']}]")

    # Conflicts
    conflicts = health["conflicts"]
    if conflicts["count"] > 0:
        lines.append(f"Conflicts: {conflicts['count']} pending")

    # Overall
    lines.append("")
    overall = health["overall_status"].upper()
    lines.append(f"OVERALL STATUS: {overall}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="ChromaDB sync health reporter (read-only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON instead of human-readable text",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit with status code: 0=ok, 1=warn, 2=critical (for cron alerting)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress info output; only show problems",
    )
    parser.add_argument(
        "--database",
        type=str,
        default=M3_DATABASE,
        help=f"Path to SQLite DB (default: {DEFAULT_DB_PATH})",
    )
    args = parser.parse_args()

    # Override database if provided
    if args.database:
        globals()["M3_DATABASE"] = args.database

    # Gather health metrics asynchronously
    try:
        import asyncio

        health = asyncio.run(gather_health())
    except Exception as e:
        print(f"Error gathering health metrics: {e}", file=sys.stderr)
        sys.exit(2)

    # Output
    if args.json:
        print(json.dumps(health, indent=2))
    elif args.quiet:
        # Only print if there's a problem
        if health["overall_status"] != "ok":
            print(format_human_readable(health))
    else:
        # Default: human-readable
        print(format_human_readable(health))

    # Exit code for --check
    if args.check:
        status_map = {"ok": 0, "warn": 1, "critical": 2}
        sys.exit(status_map.get(health["overall_status"], 2))


if __name__ == "__main__":
    main()
