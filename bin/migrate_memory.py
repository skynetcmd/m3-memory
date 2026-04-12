#!/usr/bin/env python3
"""
Migration runner for the m3-memory SQLite database.

Subcommands:
    status              Show current version and pending migrations
    up [--to N]         Apply pending migrations (prompts for backup dir + confirmation)
    down [--to N]       Roll back to version N (requires .down.sql files)
    backup [--out PATH] Take a standalone backup
    restore <PATH>      Restore the database from a backup file

Migration file formats (both supported, sorted by numeric prefix):
    NNN_name.sql            legacy, treated as up-only
    NNN_name.up.sql         explicit up
    NNN_name.down.sql       explicit down, paired with the same NNN

The schema_versions table records applied migrations. Each up/down operation
takes a filesystem-level backup of the database (including -wal/-shm) before
running, so operations are reversible at the file level even if the in-DB
transaction already committed.
"""
import argparse
import glob
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format='%(name)s: [%(levelname)s] %(message)s')
logger = logging.getLogger("migrate_memory")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "memory", "agent_memory.db")
MIGRATIONS_DIR = os.path.join(BASE_DIR, "memory", "migrations")
CONFIG_PATH = os.path.join(BASE_DIR, "memory", ".migrate_config.json")

# ── Config (backup dir persistence) ─────────────────────────────────────────

def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

def prompt_backup_dir(assume_yes: bool) -> str:
    cfg = load_config()
    saved = cfg.get("backup_dir")
    if saved and os.path.isdir(saved):
        return saved
    if assume_yes:
        # Non-interactive: fall back to out-of-repo default under the user's home
        default = os.path.join(os.path.expanduser("~"), ".m3-memory", "backups")
        os.makedirs(default, exist_ok=True)
        cfg["backup_dir"] = default
        save_config(cfg)
        logger.info(f"Non-interactive mode: using backup dir {default}")
        return default

    print("\nBefore applying migrations, the database will be backed up.")
    print("Recommended: choose a directory OUTSIDE this repo to avoid any risk")
    print("of accidentally committing backup files (even though *.db is gitignored).")
    default = os.path.join(os.path.expanduser("~"), ".m3-memory", "backups")
    print(f"Default: {default}")
    choice = input("Backup directory [press Enter for default]: ").strip()
    backup_dir = choice or default
    backup_dir = os.path.abspath(os.path.expanduser(backup_dir))
    os.makedirs(backup_dir, exist_ok=True)

    # Warn if the user picked a path inside the repo
    try:
        if os.path.commonpath([backup_dir, BASE_DIR]) == BASE_DIR:
            print(f"Note: {backup_dir} is inside the repo. *.db files are gitignored,")
            print("      so they won't be committed, but out-of-repo is still cleaner.")
    except ValueError:
        # Different drives on Windows — definitely not in-repo
        pass

    cfg["backup_dir"] = backup_dir
    save_config(cfg)
    return backup_dir

# ── Backup / restore ────────────────────────────────────────────────────────

def take_backup(backup_dir: str, version_before: int, tag: str) -> str:
    if not os.path.exists(DB_PATH):
        logger.info("No existing database to back up.")
        return ""
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    basename = f"agent_memory.v{version_before:03d}.{tag}.{ts}.db"
    dst = os.path.join(backup_dir, basename)
    shutil.copy2(DB_PATH, dst)
    for suffix in ("-wal", "-shm"):
        src = DB_PATH + suffix
        if os.path.exists(src):
            shutil.copy2(src, dst + suffix)
    logger.info(f"Backup written: {dst}")
    return dst

def restore_backup(backup_path: str):
    if not os.path.exists(backup_path):
        logger.error(f"Backup not found: {backup_path}")
        sys.exit(1)
    # Move current DB aside first so we can recover if the copy fails mid-way
    if os.path.exists(DB_PATH):
        sidecar = DB_PATH + ".pre-restore"
        shutil.move(DB_PATH, sidecar)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(DB_PATH + suffix):
                shutil.move(DB_PATH + suffix, sidecar + suffix)
    try:
        shutil.copy2(backup_path, DB_PATH)
        for suffix in ("-wal", "-shm"):
            src = backup_path + suffix
            if os.path.exists(src):
                shutil.copy2(src, DB_PATH + suffix)
        logger.info(f"Restored {DB_PATH} from {backup_path}")
    except Exception as e:
        logger.error(f"Restore failed: {e}")
        sys.exit(1)

# ── Migration discovery ─────────────────────────────────────────────────────

_FNAME_RE = re.compile(r"^(\d+)_(.+?)(?:\.(up|down))?\.sql$")

def discover_migrations():
    """
    Returns a dict: { version: { 'name': str, 'up': path|None, 'down': path|None } }.
    Legacy NNN_name.sql files map to 'up' with down=None.
    """
    out = {}
    for filepath in glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql")):
        fname = os.path.basename(filepath)
        m = _FNAME_RE.match(fname)
        if not m:
            logger.warning(f"Skipping malformed migration file: {fname}")
            continue
        version = int(m.group(1))
        name = m.group(2)
        direction = m.group(3)  # 'up', 'down', or None (legacy)
        entry = out.setdefault(version, {"name": name, "up": None, "down": None})
        if direction == "down":
            entry["down"] = filepath
        else:
            # 'up' or legacy (no direction)
            entry["up"] = filepath
            entry["name"] = name
    return out

def init_migrations_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_versions (
            version INTEGER PRIMARY KEY,
            filename TEXT NOT NULL,
            applied_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()

def get_applied_versions(conn):
    rows = conn.execute("SELECT version FROM schema_versions ORDER BY version").fetchall()
    return [row[0] for row in rows]

def current_version(conn) -> int:
    applied = get_applied_versions(conn)
    return max(applied) if applied else 0

# ── Core apply / revert ─────────────────────────────────────────────────────

def _run_sql_transaction(conn, filepath: str):
    with open(filepath, "r", encoding="utf-8") as f:
        sql_script = f.read()
    conn.execute("BEGIN")
    conn.executescript(sql_script)

def apply_migration(conn, version: int, name: str, up_path: str):
    logger.info(f"Applying migration {version:03d}: {os.path.basename(up_path)}")
    try:
        _run_sql_transaction(conn, up_path)
        conn.execute(
            "INSERT INTO schema_versions (version, filename) VALUES (?, ?)",
            (version, os.path.basename(up_path)),
        )
        conn.commit()
        logger.info(f"  -> applied v{version:03d}")
    except sqlite3.OperationalError as e:
        conn.rollback()
        err = str(e).lower()
        if "duplicate column name" in err or "already exists" in err:
            # Preserve legacy idempotency: schema already has what this migration adds
            logger.warning(f"v{version:03d} already partially applied ({e}); marking applied.")
            conn.execute(
                "INSERT OR IGNORE INTO schema_versions (version, filename) VALUES (?, ?)",
                (version, os.path.basename(up_path)),
            )
            conn.commit()
        else:
            logger.error(f"Failed to apply v{version:03d}: {e}")
            raise

def revert_migration(conn, version: int, down_path: str):
    logger.info(f"Reverting migration {version:03d}: {os.path.basename(down_path)}")
    try:
        _run_sql_transaction(conn, down_path)
        conn.execute("DELETE FROM schema_versions WHERE version = ?", (version,))
        conn.commit()
        logger.info(f"  -> reverted v{version:03d}")
    except sqlite3.OperationalError as e:
        conn.rollback()
        logger.error(f"Failed to revert v{version:03d}: {e}")
        raise

# ── Subcommands ─────────────────────────────────────────────────────────────

def cmd_status(args):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        init_migrations_table(conn)
        applied = set(get_applied_versions(conn))
        cur = current_version(conn)
        migs = discover_migrations()
        all_versions = sorted(migs.keys())
        target = max(all_versions) if all_versions else 0

        print(f"Database:         {DB_PATH}")
        print(f"Current version:  {cur}")
        print(f"Latest available: {target}")
        print(f"Status:           {'up-to-date' if cur == target else 'behind'}")
        print()
        print("Migrations:")
        for v in all_versions:
            entry = migs[v]
            mark = "x" if v in applied else " "
            down = "yes" if entry["down"] else "no "
            up_name = os.path.basename(entry["up"]) if entry["up"] else "(missing up!)"
            print(f"  [{mark}] v{v:03d}  down={down}  {up_name}")

        pending = [v for v in all_versions if v not in applied]
        if pending:
            print()
            print(f"Pending: {pending}")
    finally:
        conn.close()

def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    ans = input(f"{prompt} [y/N]: ").strip().lower()
    return ans in ("y", "yes")

def cmd_up(args):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        init_migrations_table(conn)
        applied = set(get_applied_versions(conn))
        cur = current_version(conn)
        migs = discover_migrations()
        all_versions = sorted(migs.keys())
        target = args.to if args.to is not None else (max(all_versions) if all_versions else 0)

        pending = [v for v in all_versions if v not in applied and v <= target]
        if not pending:
            logger.info("Database is up to date. No pending migrations.")
            return

        print(f"\nCurrent version: v{cur:03d}")
        print(f"Target version:  v{target:03d}")
        print("Will apply:")
        for v in pending:
            entry = migs[v]
            down = "has down" if entry["down"] else "NO down (legacy, irreversible)"
            print(f"  + v{v:03d}  {entry['name']}  [{down}]")

        backup_dir = prompt_backup_dir(args.yes)
        if not _confirm(f"\nApply {len(pending)} migration(s)?", args.yes):
            logger.info("Aborted by user.")
            return

        take_backup(backup_dir, cur, "pre-up")

        for v in pending:
            entry = migs[v]
            if not entry["up"]:
                logger.error(f"v{v:03d} has no up file — skipping")
                continue
            apply_migration(conn, v, entry["name"], entry["up"])

        logger.info(f"Done. Now at v{current_version(conn):03d}.")
    finally:
        conn.close()

def cmd_down(args):
    if args.to is None:
        logger.error("down requires --to N (the version to roll back TO)")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    try:
        init_migrations_table(conn)
        applied = get_applied_versions(conn)
        cur = current_version(conn)
        migs = discover_migrations()

        if args.to >= cur:
            logger.info(f"Already at or below v{args.to:03d} (current: v{cur:03d}). Nothing to do.")
            return

        # Versions to revert, in reverse order
        to_revert = [v for v in reversed(applied) if v > args.to]

        # Pre-flight: every version being reverted needs a down file
        missing = [v for v in to_revert if not migs.get(v, {}).get("down")]
        if missing:
            logger.error(
                f"Cannot roll back: no down migration available for version(s) {missing}. "
                f"Legacy migrations (NNN_name.sql) are irreversible. "
                f"Lowest reversible target above them is v{max(missing):03d}."
            )
            sys.exit(1)

        print(f"\nCurrent version: v{cur:03d}")
        print(f"Target version:  v{args.to:03d}")
        print("Will revert (in order):")
        for v in to_revert:
            print(f"  - v{v:03d}  {migs[v]['name']}")

        backup_dir = prompt_backup_dir(args.yes)
        if not _confirm(f"\nRevert {len(to_revert)} migration(s)?", args.yes):
            logger.info("Aborted by user.")
            return

        take_backup(backup_dir, cur, "pre-down")

        for v in to_revert:
            revert_migration(conn, v, migs[v]["down"])

        logger.info(f"Done. Now at v{current_version(conn):03d}.")
    finally:
        conn.close()

def cmd_backup(args):
    backup_dir = args.out or prompt_backup_dir(args.yes)
    conn = sqlite3.connect(DB_PATH)
    try:
        init_migrations_table(conn)
        cur = current_version(conn)
    finally:
        conn.close()
    take_backup(backup_dir, cur, "manual")

def cmd_restore(args):
    if not _confirm(f"Restore database from {args.path}? This will overwrite the current DB.", args.yes):
        logger.info("Aborted by user.")
        return
    restore_backup(args.path)

# ── Entry point ─────────────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(description="m3-memory database migration runner")
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("status", help="Show current version and pending migrations")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("up", help="Apply pending migrations")
    sp.add_argument("--to", type=int, default=None, help="Apply up to this version (default: latest)")
    sp.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompts")
    sp.set_defaults(func=cmd_up)

    sp = sub.add_parser("down", help="Roll back migrations")
    sp.add_argument("--to", type=int, required=True, help="Roll back to this version")
    sp.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompts")
    sp.set_defaults(func=cmd_down)

    sp = sub.add_parser("backup", help="Take a standalone backup of the database")
    sp.add_argument("--out", type=str, default=None, help="Backup directory (overrides saved default)")
    sp.add_argument("-y", "--yes", action="store_true", help="Skip interactive prompts")
    sp.set_defaults(func=cmd_backup)

    sp = sub.add_parser("restore", help="Restore the database from a backup file")
    sp.add_argument("path", help="Path to the backup .db file")
    sp.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")
    sp.set_defaults(func=cmd_restore)

    return p

def main():
    if not os.path.exists(MIGRATIONS_DIR):
        logger.error(f"Migrations directory not found: {MIGRATIONS_DIR}")
        sys.exit(1)

    parser = build_parser()
    args = parser.parse_args()

    # Back-compat: `python migrate_memory.py` with no args == `up` (the old behavior),
    # but now with the new prompts. Scripts that relied on the old no-arg invocation
    # will still work, just interactively.
    if args.command is None:
        args = parser.parse_args(["up"])

    args.func(args)

if __name__ == "__main__":
    main()
