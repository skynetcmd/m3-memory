#!/usr/bin/env python3
"""
sync_all.py — Hourly sync runner (SQLite <-> PostgreSQL + ChromaDB).
Runs both pg_sync.py and chroma_sync, offline-tolerant.
Safe to call on any platform; skips gracefully if target unreachable.

Usage:
    python bin/sync_all.py
    python bin/sync_all.py --dry-run   (connectivity check only)
"""
import argparse
import logging
import os
import pathlib
import platform
import socket
import subprocess
import sys

IS_WIN = platform.system() == "Windows"

BASE    = pathlib.Path(__file__).parent.parent.resolve()
LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "sync_all.log"
PY      = BASE / ".venv" / ("Scripts/python.exe" if IS_WIN else "bin/python")
TARGET_IP = os.environ.get("POSTGRES_SERVER", os.environ.get("SYNC_TARGET_IP", ""))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("sync_all")


def is_reachable(host: str, port: int = 5432, timeout: float = 3.0) -> bool:
    """TCP probe — faster and more reliable than ping across platforms."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        pass
    # fallback: try ChromaDB port
    try:
        with socket.create_connection((host, 8000), timeout=timeout):
            return True
    except OSError:
        return False


def run_pg_sync(dry_run: bool) -> bool:
    if dry_run:
        log.info("[DRY-RUN] Would run pg_sync.py")
        return True
    log.info("Running pg_sync.py (SQLite <-> PostgreSQL)...")
    try:
        result = subprocess.run(
            [str(PY), str(BASE / "bin" / "pg_sync.py")],
            capture_output=True, text=True, timeout=60
        )
        for line in (result.stdout + result.stderr).splitlines():
            if line.strip():
                log.info(f"  pg_sync: {line}")
        if result.returncode == 0:
            log.info("pg_sync completed successfully.")
            return True
        else:
            log.error(f"pg_sync exited with code {result.returncode}")
            return False
    except subprocess.TimeoutExpired:
        log.error("pg_sync timed out after 60s")
        return False
    except Exception as e:
        log.error(f"pg_sync failed: {type(e).__name__}: {e}")
        return False


def run_chroma_sync(dry_run: bool) -> bool:
    if dry_run:
        log.info("[DRY-RUN] Would run chroma_sync via chroma_sync_cli.py")
        return True
    log.info("Running ChromaDB sync (both directions)...")
    try:
        env = os.environ.copy()
        env.setdefault("CHROMA_BASE_URL", f"http://{TARGET_IP}:8000")
        env.setdefault("LM_STUDIO_EMBED_URL", "http://127.0.0.1:1234/v1/embeddings")
        result = subprocess.run(
            [str(PY), str(BASE / "bin" / "chroma_sync_cli.py"), "both"],
            capture_output=True, text=True, timeout=120, env=env
        )
        for line in (result.stdout + result.stderr).splitlines():
            if line.strip():
                log.info(f"  chroma: {line}")
        if result.returncode == 0:
            log.info("ChromaDB sync completed.")
            return True
        else:
            log.error(f"chroma_sync exited with code {result.returncode}")
            return False
    except subprocess.TimeoutExpired:
        log.error("chroma_sync timed out after 120s")
        return False
    except Exception as e:
        log.error(f"chroma_sync failed: {type(e).__name__}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Hourly sync runner")
    parser.add_argument("--dry-run", action="store_true", help="Check connectivity only")
    args = parser.parse_args()

    log.info(f"=== sync_all starting [{platform.system()}] ===")

    if not TARGET_IP:
        log.info("SYNC_TARGET_IP not set — skipping sync.")
        sys.exit(0)

    if not is_reachable(TARGET_IP):
        log.warning(f"PostgreSQL data warehouse ({TARGET_IP}) unreachable — skipping sync (will retry next hour).")
        sys.exit(0)

    log.info(f"PostgreSQL data warehouse ({TARGET_IP}) reachable — running full sync.")

    pg_ok     = run_pg_sync(args.dry_run)
    chroma_ok = run_chroma_sync(args.dry_run)

    if pg_ok and chroma_ok:
        log.info("=== sync_all complete: all systems synced ===")
        sys.exit(0)
    else:
        log.error(f"=== sync_all finished with errors: pg={pg_ok} chroma={chroma_ok} ===")
        sys.exit(1)


if __name__ == "__main__":
    main()
