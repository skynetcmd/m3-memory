"""
bin/homecoming.py — "Homecoming" migration script for m3-memory.
Relocates repo-relative state to ~/.m3-memory/.

This tool is non-destructive: it COPIES databases using the SQLite Backup API
and MOVES configuration files. It does NOT modify system-wide tool settings
(Claude/Gemini) to ensure safety. Users should update their tool settings
manually to point to the new bridge paths.
"""

import os
import sys
import shutil
import sqlite3
import logging
from pathlib import Path

# Add bin to path for m3_sdk
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from m3_sdk import get_m3_root
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
    """Identify files in the current repo's memory/ folder that should move."""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assets = {
        "core_db": os.path.join(base, "memory", "agent_memory.db"),
        "chatlog_db": os.path.join(base, "memory", "agent_chatlog.db"),
        "test_bench_db": os.path.join(base, "memory", "agent_test_bench.db"),
        "chatlog_config": os.path.join(base, "memory", ".chatlog_config.json"),
        "chatlog_state": os.path.join(base, "memory", ".chatlog_state.json"),
        "chatlog_cursor": os.path.join(base, "memory", ".chatlog_ingest_cursor.json"),
        "chatlog_spill": os.path.join(base, "memory", "chatlog_spill"),
        "migrate_config": os.path.join(base, "memory", ".migrate_config.json"),
        # Note: .agent_os_salt is already in home, but M3_SDK now looks for it in get_m3_root()
        "salt": os.path.join(os.path.expanduser("~"), ".agent_os_salt")
    }
    return {k: v for k, v in assets.items() if os.path.exists(v)}

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
    target_root = get_m3_root()
    logger.info(f"Homecoming Target Root: {target_root}")
    
    assets = get_legacy_assets()
    if not assets:
        logger.info("No legacy assets found in repository. Current configuration is already standard.")
        return

    total_size = sum(get_size_mb(v) for v in assets.values())
    logger.info(f"Found {len(assets)} legacy assets ({total_size:.2f} MB)")

    # Ensure target directory structure
    os.makedirs(os.path.join(target_root, "memory"), exist_ok=True)
    
    for key, src in assets.items():
        dst_name = os.path.basename(src)
        
        # Salt goes in the root; everything else goes in memory/
        if "salt" in key:
            dst = os.path.join(target_root, dst_name)
        else:
            dst = os.path.join(target_root, "memory", dst_name)
            
        if os.path.exists(dst):
            logger.warning(f"Destination {dst} already exists. Skipping.")
            continue

        if src.endswith(".db"):
            backup_db(src, dst)
        else:
            logger.info(f"Copying {dst_name}...")
            try:
                if os.path.isdir(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
                logger.info(f"Successfully copied {dst_name}")
            except Exception as e:
                logger.error(f"Failed to copy {dst_name}: {e}")
    
    logger.info("\nMigration Step 1 (Data) completed.")
    logger.info("Next Step: Update your Gemini/Claude/Aider settings to use the new bridge paths.")
    logger.info(f"New data root: {target_root}")

if __name__ == "__main__":
    main()
