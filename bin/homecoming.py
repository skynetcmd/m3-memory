"""
bin/homecoming.py — "Homecoming" migration script for m3-memory.
Relocates repo-relative and old ~/.m3-memory/ state to new decoupled standard roots
(~/.m3/config and ~/.m3/engine).

This tool is non-destructive: it COPIES databases using the SQLite Backup API
and MOVES configuration files. It does NOT modify system-wide tool settings
(Claude/Gemini) to ensure safety. manually update tool settings if needed.
"""

import logging
import os
import shutil
import sqlite3
import sys
from pathlib import Path

# Add bin to path for m3_sdk
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from m3_sdk import get_m3_config_root, get_m3_engine_root
except ImportError:
    print("Error: Could not import m3_sdk. Run from project root.")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("homecoming")

def get_size_mb(path):
    if os.path.isfile(path):
        return os.path.getsize(path) / (1024 * 1024)
    elif os.path.isdir(path):
        total = 0
        for p in Path(path).rglob('*'):
            if p.is_file():
                total += p.stat().st_size
        return total / (1024 * 1024)
    return 0

def get_legacy_assets():
    """Identify legacy assets from repository clone's memory/ folder OR old ~/.m3-memory/
    that should be migrated to new decoupled locations.
    """
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    old_m3_root = os.path.join(os.path.expanduser("~"), ".m3-memory")

    candidates = [
        # (key, rel_path, dst_type)
        ("core_db", "memory/agent_memory.db", "engine"),
        ("chatlog_db", "memory/agent_chatlog.db", "engine"),
        ("test_bench_db", "memory/agent_test_bench.db", "engine"),
        ("chatlog_config", "memory/.chatlog_config.json", "config"),
        ("chatlog_state", "memory/.chatlog_state.json", "engine"),
        ("chatlog_cursor", "memory/.chatlog_ingest_cursor.json", "engine"),
        ("chatlog_spill", "memory/chatlog_spill", "engine"),
        ("migrate_config", "memory/.migrate_config.json", "config"),
        ("salt", ".agent_os_salt", "config"),
    ]

    assets = {}
    for key, rel_path, dst_type in candidates:
        src_repo = os.path.join(base, rel_path)
        src_old_home = os.path.join(old_m3_root, rel_path.replace("memory/", ""))

        if key == "salt":
            src_repo = os.path.join(base, ".agent_os_salt")
            src_old_home = os.path.join(old_m3_root, ".agent_os_salt")

        if os.path.exists(src_repo):
            assets[key] = (src_repo, dst_type)
        elif os.path.exists(src_old_home):
            assets[key] = (src_old_home, dst_type)

    return assets

def backup_db(src, dst):
    """Secure copy using SQLite Backup API."""
    logger.info(f"Backing up {os.path.basename(src)} to {dst}...")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        src_conn = sqlite3.connect(src)
        dst_conn = sqlite3.connect(dst)
        with dst_conn:
            src_conn.backup(dst_conn)
        src_conn.close()
        dst_conn.close()
        logger.info(f"Successfully backed up {os.path.basename(src)}")
    except Exception as e:
        logger.error(f"Failed to backup {src}: {e}")

def main():
    config_root = get_m3_config_root()
    engine_root = get_m3_engine_root()
    logger.info(f"Homecoming Target Config Root: {config_root}")
    logger.info(f"Homecoming Target Engine Root: {engine_root}")

    assets = get_legacy_assets()
    if not assets:
        logger.info("No legacy assets found in repository or old ~/.m3-memory/. Current configuration is already standard.")
        return

    total_size = sum(get_size_mb(src) for src, _ in assets.values())
    logger.info(f"Found {len(assets)} legacy assets ({total_size:.2f} MB)")

    # Ensure target directory structure
    os.makedirs(config_root, exist_ok=True)
    os.makedirs(engine_root, exist_ok=True)

    for key, (src, dst_type) in assets.items():
        dst_name = os.path.basename(src)
        target_dir = config_root if dst_type == "config" else engine_root
        dst = os.path.join(target_dir, dst_name)

        if os.path.exists(dst):
            logger.warning(f"Destination {dst} already exists. Skipping.")
            continue

        if src.endswith(".db"):
            backup_db(src, dst)
        else:
            logger.info(f"Copying {dst_name} to {target_dir}...")
            try:
                if os.path.isdir(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
                logger.info(f"Successfully copied {dst_name}")
            except Exception as e:
                logger.error(f"Failed to copy {dst_name}: {e}")

    logger.info("\nMigration (Data & Configuration) completed.")
    logger.info(f"New configuration root: {config_root}")
    logger.info(f"New engine root: {engine_root}")

if __name__ == "__main__":
    main()
