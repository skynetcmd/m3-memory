#!/usr/bin/env python3
"""
sync_all.py — Hourly sync runner (SQLite <-> PostgreSQL).
Runs pg_sync.py once per configured DB. Offline-tolerant.
Safe to call on any platform; skips gracefully if target unreachable or DB absent.

Usage:
    python bin/sync_all.py
    python bin/sync_all.py --dry-run   (connectivity check only)

DB list:
    Repo default: `memory/agent_memory.db`. The agent_memory manifest sweeps
    both `main` and `chatlog` targets internally, so chatlog data gets synced
    in the same pass without listing it separately. Bench DBs and other
    custom databases are NOT auto-detected — set M3_SYNC_DBS to include them.

    Example self-host override:
        M3_SYNC_DBS=memory/agent_memory.db:../m3-memory-bench/data/agent_bench.db
"""
import argparse
import logging
import os
import pathlib
import socket
import subprocess
import sys

from m3_sdk import getenv_compat

IS_WIN = sys.platform == "win32"

# Subprocess timeout for pg_sync.py. First-run full syncs can take several
# minutes; delta syncs are much faster. Override with M3_PG_SYNC_TIMEOUT (seconds).
PG_SYNC_TIMEOUT = int(os.environ.get("M3_PG_SYNC_TIMEOUT", "600"))

BASE    = pathlib.Path(__file__).parent.parent.resolve()
LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "sync_all.log"
PY      = BASE / ".venv" / ("Scripts/python.exe" if IS_WIN else "bin/python")
TARGET_IP = getenv_compat("M3_POSTGRES_SERVER", "POSTGRES_SERVER", getenv_compat("M3_SYNC_TARGET_IP", "SYNC_TARGET_IP", ""))

# Logging is configured in main() via setup_task_runtime so scheduled-task
# runs self-log without a shell `>>` redirect. This minimal fallback keeps
# `log` usable if a caller imports this module without running main().
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("sync_all")

# ── DB list resolution ────────────────────────────────────────────────────────
# Repo default: sync the production memory DB. The agent_memory.yaml manifest
# already sweeps BOTH `main` (agent_memory.db) and `chatlog` (agent_chatlog.db)
# targets in a single pg_sync invocation, so we don't list chatlog separately —
# doing so would either re-sync the same data or fail on a missing manifest.
# Anything beyond this (bench DBs, custom layouts) is self-host territory —
# users wire it up themselves via M3_SYNC_DBS.
_DEFAULT_DBS = [
    "memory/agent_memory.db",
]


def _resolve_dbs() -> list[pathlib.Path]:
    """Return list of DB paths to sync.

    Priority:
      1. M3_SYNC_DBS env var (explicit override; colon- or comma-separated paths).
      2. m3_sdk.resolve_db_path() — the Homecoming-aware resolver (honours
         M3_ENGINE_ROOT / M3_MEMORY_ROOT / M3_DATABASE).
      3. Repo-relative fallback (memory/agent_memory.db) if m3_sdk is not importable.

    Bench DBs and any other databases are NOT auto-detected — set M3_SYNC_DBS
    if you self-host a custom layout.
    """
    raw = os.environ.get("M3_SYNC_DBS", "")
    if raw:
        parts = [p.strip() for p in raw.replace(",", ":").split(":") if p.strip()]
        resolved = []
        for p in parts:
            path = pathlib.Path(p)
            if not path.is_absolute():
                path = BASE / path
            resolved.append(path.resolve())
        return resolved

    # No explicit override — use the Homecoming-aware DB resolver so we always
    # sync the live database.
    try:
        from m3_sdk import resolve_db_path
        return [pathlib.Path(resolve_db_path()).resolve()]
    except Exception as exc:
        log.warning(f"m3_sdk.resolve_db_path() unavailable ({exc}); falling back to repo default")
        path = BASE / _DEFAULT_DBS[0]
        return [path.resolve()]


# ── Network check ─────────────────────────────────────────────────────────────

def is_reachable(host: str, port: int = 5432, timeout: float = 3.0) -> bool:
    """TCP probe — faster and more reliable than ping across platforms."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ── pg_sync runner ────────────────────────────────────────────────────────────

def run_pg_sync_for_db(db_path: pathlib.Path, dry_run: bool) -> bool:
    """Run pg_sync.py --db <path> for one database. Returns True on success."""
    if not db_path.exists():
        log.info(f"  skipping {db_path} — not present on this peer")
        return True  # not an error; this peer just doesn't have that DB

    if dry_run:
        log.info(f"[DRY-RUN] Would run pg_sync.py --db {db_path}")
        return True

    log.info(f"Running pg_sync.py --db {db_path} ...")
    try:
        from _task_runtime import no_window_kwargs
        result = subprocess.run(
            [str(PY), str(BASE / "bin" / "pg_sync.py"), "--db", str(db_path)],
            capture_output=True, text=True, timeout=PG_SYNC_TIMEOUT,
            **no_window_kwargs(),
        )
        for line in (result.stdout + result.stderr).splitlines():
            if line.strip():
                log.info(f"  pg_sync[{db_path.stem}]: {line}")
        if result.returncode == 0:
            log.info(f"pg_sync completed for {db_path.stem}.")
            return True
        else:
            log.error(f"pg_sync exited with code {result.returncode} for {db_path.stem}")
            return False
    except subprocess.TimeoutExpired:
        log.error(f"pg_sync timed out after {PG_SYNC_TIMEOUT}s for {db_path.stem}")
        return False
    except Exception as e:
        log.error(f"pg_sync failed for {db_path.stem}: {type(e).__name__}: {e}")
        return False


def run_pg_sync(dry_run: bool) -> bool:
    """Run pg_sync.py for each configured database. Returns True if all succeed."""
    dbs = _resolve_dbs()
    log.info(f"pg_sync target DBs: {[str(d) for d in dbs]}")
    results = []
    for db in dbs:
        ok = run_pg_sync_for_db(db, dry_run)
        results.append(ok)
    return all(results)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Hourly sync runner")
    parser.add_argument("--dry-run", action="store_true", help="Check connectivity only")
    sys.path.insert(0, str(BASE / "bin"))
    from _task_runtime import add_log_file_arg, setup_task_runtime
    from m3_sdk import add_database_arg
    add_log_file_arg(parser)
    add_database_arg(parser)
    args = parser.parse_args()

    setup_task_runtime(args.log_file or LOG_FILE, lock_name="sync_all")

    if args.database:
        # Pass-through env so the pg_sync subprocess inherits.
        os.environ["M3_DATABASE"] = args.database

    # sys.platform, not platform.system() (WMI-hang risk on Py3.14/Windows).
    _os = {"darwin": "Darwin", "win32": "Windows"}.get(sys.platform, "Linux")
    log.info(f"=== sync_all starting [{_os}] ===")

    if not TARGET_IP:
        log.info("SYNC_TARGET_IP not set — skipping sync.")
        sys.exit(0)

    if not is_reachable(TARGET_IP):
        log.warning(f"PostgreSQL data warehouse ({TARGET_IP}) unreachable — skipping sync (will retry next hour).")
        sys.exit(0)

    log.info(f"PostgreSQL data warehouse ({TARGET_IP}) reachable — running full sync.")

    pg_ok = run_pg_sync(args.dry_run)

    if pg_ok:
        log.info("=== sync_all complete: all systems synced ===")
        sys.exit(0)
    else:
        log.error(f"=== sync_all finished with errors: pg={pg_ok} ===")
        sys.exit(1)


if __name__ == "__main__":
    main()
