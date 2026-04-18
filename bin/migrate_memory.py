#!/usr/bin/env python3
"""
Migration runner for the m3-memory SQLite databases.

Supports multiple migration targets:
    - main (agent_memory.db) — always present
    - chatlog — optional, controlled by chatlog_config.chatlog_mode()

Subcommands:
    status              Show current version and pending migrations
    up [--to N]         Apply pending migrations (prompts for backup dir + confirmation)
    down [--to N]       Roll back to version N (requires .down.sql files)
    backup [--out PATH] Take a standalone backup
    restore <PATH>      Restore the database from a backup file

All subcommands accept --target {main,chatlog,all} to select which DB(s) to operate on.
Default is "all" (operates on all configured targets).

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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

logging.basicConfig(level=logging.INFO, format='%(name)s: [%(levelname)s] %(message)s')
logger = logging.getLogger("migrate_memory")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "memory", "agent_memory.db")
MIGRATIONS_DIR = os.path.join(BASE_DIR, "memory", "migrations")
CONFIG_PATH = os.path.join(BASE_DIR, "memory", ".migrate_config.json")


# ── Migration Target ────────────────────────────────────────────────────────

@dataclass
class MigrationTarget:
    """Represents a migration target: name, path to DB, and migrations directory."""
    name: str              # "main" or "chatlog"
    db_path: str
    migrations_dir: str


def targets(selected: str = "all") -> List[MigrationTarget]:
    """
    Returns a list of MigrationTarget objects based on selection.

    Args:
        selected: "main", "chatlog", or "all"

    Returns:
        List of MigrationTarget objects configured for the selection.
        If chatlog_config import fails, returns only main.
    """
    main_target = MigrationTarget(name="main", db_path=DB_PATH, migrations_dir=MIGRATIONS_DIR)

    if selected == "main":
        return [main_target]

    if selected == "all" or selected == "chatlog":
        result = [main_target]

        # Try to load chatlog config
        try:
            from chatlog_config import CHATLOG_MIGRATIONS_DIR, chatlog_db_path, chatlog_mode
            mode = chatlog_mode()
            if mode in ("separate", "hybrid"):
                chatlog_target = MigrationTarget(
                    name="chatlog",
                    db_path=chatlog_db_path(),
                    migrations_dir=CHATLOG_MIGRATIONS_DIR
                )
                if selected == "chatlog":
                    return [chatlog_target]
                else:  # all
                    result.append(chatlog_target)
            elif selected == "chatlog":
                logger.warning(f"chatlog_mode() returned '{mode}', not 'separate' or 'hybrid'. No chatlog target.")
                return []
        except ImportError:
            logger.warning("chatlog_config module not found. Operating on main DB only.")
            if selected == "chatlog":
                logger.error("Cannot use --target chatlog without chatlog_config module.")
                return []

        return result

    logger.error(f"Unknown target selection: {selected}")
    return [main_target]

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

def take_backup(backup_dir: str, version_before: int, tag: str, target: MigrationTarget) -> str:
    if not os.path.exists(target.db_path):
        logger.info(f"No existing database to back up: {target.db_path}")
        return ""
    # Create per-target subdirectory under backup_dir
    target_backup_dir = os.path.join(backup_dir, target.name)
    os.makedirs(target_backup_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    db_basename = os.path.basename(target.db_path).replace(".db", "")
    basename = f"{db_basename}.v{version_before:03d}.{tag}.{ts}.db"
    dst = os.path.join(target_backup_dir, basename)
    shutil.copy2(target.db_path, dst)
    for suffix in ("-wal", "-shm"):
        src = target.db_path + suffix
        if os.path.exists(src):
            shutil.copy2(src, dst + suffix)
    logger.info(f"Backup written: {dst}")
    return dst

def restore_backup(backup_path: str, target: MigrationTarget):
    if not os.path.exists(backup_path):
        logger.error(f"Backup not found: {backup_path}")
        sys.exit(1)
    # Move current DB aside first so we can recover if the copy fails mid-way
    if os.path.exists(target.db_path):
        sidecar = target.db_path + ".pre-restore"
        shutil.move(target.db_path, sidecar)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(target.db_path + suffix):
                shutil.move(target.db_path + suffix, sidecar + suffix)
    try:
        shutil.copy2(backup_path, target.db_path)
        for suffix in ("-wal", "-shm"):
            src = backup_path + suffix
            if os.path.exists(src):
                shutil.copy2(src, target.db_path + suffix)
        logger.info(f"Restored {target.db_path} from {backup_path}")
    except Exception as e:
        logger.error(f"Restore failed: {e}")
        sys.exit(1)

# ── Migration discovery ─────────────────────────────────────────────────────

_FNAME_RE = re.compile(r"^(\d+)_(.+?)(?:\.(up|down))?\.sql$")

def discover_migrations(migrations_dir: str):
    """
    Returns a dict: { version: { 'name': str, 'up': path|None, 'down': path|None } }.
    Legacy NNN_name.sql files map to 'up' with down=None.

    Args:
        migrations_dir: Directory to scan for migration files.
    """
    out: dict[int, dict[str, Optional[str]]] = {}
    for filepath in glob.glob(os.path.join(migrations_dir, "*.sql")):
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
    target_list = targets(args.target)
    if not target_list:
        logger.error(f"No targets matched: {args.target}")
        sys.exit(1)

    for target in target_list:
        if len(target_list) > 1:
            print(f"=== {target.name} ===")

        os.makedirs(os.path.dirname(target.db_path), exist_ok=True)
        conn = sqlite3.connect(target.db_path)
        try:
            init_migrations_table(conn)
            applied = set(get_applied_versions(conn))
            cur = current_version(conn)
            migs = discover_migrations(target.migrations_dir)
            all_versions = sorted(migs.keys())
            latest = max(all_versions) if all_versions else 0

            print(f"Database:         {target.db_path}")
            print(f"Current version:  {cur}")
            print(f"Latest available: {latest}")
            print(f"Status:           {'up-to-date' if cur == latest else 'behind'}")
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

        if len(target_list) > 1:
            print()

def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    ans = input(f"{prompt} [y/N]: ").strip().lower()
    return ans in ("y", "yes")

def cmd_up(args):
    target_list = targets(args.target)
    if not target_list:
        logger.error(f"No targets matched: {args.target}")
        sys.exit(1)

    backup_dir = prompt_backup_dir(args.yes)

    for target in target_list:
        if len(target_list) > 1:
            logger.info(f"=== Processing {target.name} ===")

        os.makedirs(os.path.dirname(target.db_path), exist_ok=True)
        conn = sqlite3.connect(target.db_path)
        try:
            init_migrations_table(conn)
            applied = set(get_applied_versions(conn))
            cur = current_version(conn)
            migs = discover_migrations(target.migrations_dir)
            all_versions = sorted(migs.keys())
            target_ver = args.to if args.to is not None else (max(all_versions) if all_versions else 0)

            pending = [v for v in all_versions if v not in applied and v <= target_ver]
            if not pending:
                logger.info(f"{target.name}: Database is up to date. No pending migrations.")
                continue

            print(f"\nCurrent version: v{cur:03d}")
            print(f"Target version:  v{target_ver:03d}")
            print("Will apply:")
            for v in pending:
                entry = migs[v]
                down = "has down" if entry["down"] else "NO down (legacy, irreversible)"
                print(f"  + v{v:03d}  {entry['name']}  [{down}]")

            if not _confirm(f"\nApply {len(pending)} migration(s) to {target.name}?", args.yes):
                logger.info("Aborted by user.")
                continue

            take_backup(backup_dir, cur, "pre-up", target)

            for v in pending:
                entry = migs[v]
                if not entry["up"]:
                    logger.error(f"v{v:03d} has no up file — skipping")
                    continue
                apply_migration(conn, v, entry["name"], entry["up"])

            logger.info(f"{target.name}: Done. Now at v{current_version(conn):03d}.")
        finally:
            conn.close()

def cmd_down(args):
    if args.to is None:
        logger.error("down requires --to N (the version to roll back TO)")
        sys.exit(1)

    target_list = targets(args.target)
    if not target_list:
        logger.error(f"No targets matched: {args.target}")
        sys.exit(1)

    backup_dir = prompt_backup_dir(args.yes)

    for target in target_list:
        if len(target_list) > 1:
            logger.info(f"=== Processing {target.name} ===")

        conn = sqlite3.connect(target.db_path)
        try:
            init_migrations_table(conn)
            applied = get_applied_versions(conn)
            cur = current_version(conn)
            migs = discover_migrations(target.migrations_dir)

            if args.to >= cur:
                logger.info(f"{target.name}: Already at or below v{args.to:03d} (current: v{cur:03d}). Nothing to do.")
                continue

            # Versions to revert, in reverse order
            to_revert = [v for v in reversed(applied) if v > args.to]

            # Pre-flight: every version being reverted needs a down file
            missing = [v for v in to_revert if not migs.get(v, {}).get("down")]
            if missing:
                logger.error(
                    f"{target.name}: Cannot roll back: no down migration available for version(s) {missing}. "
                    f"Legacy migrations (NNN_name.sql) are irreversible. "
                    f"Lowest reversible target above them is v{max(missing):03d}."
                )
                continue

            print(f"\nCurrent version: v{cur:03d}")
            print(f"Target version:  v{args.to:03d}")
            print("Will revert (in order):")
            for v in to_revert:
                print(f"  - v{v:03d}  {migs[v]['name']}")

            if not _confirm(f"\nRevert {len(to_revert)} migration(s) on {target.name}?", args.yes):
                logger.info("Aborted by user.")
                continue

            take_backup(backup_dir, cur, "pre-down", target)

            for v in to_revert:
                revert_migration(conn, v, migs[v]["down"])

            logger.info(f"{target.name}: Done. Now at v{current_version(conn):03d}.")
        finally:
            conn.close()

def cmd_backup(args):
    target_list = targets(args.target)
    if not target_list:
        logger.error(f"No targets matched: {args.target}")
        sys.exit(1)

    backup_dir = args.out or prompt_backup_dir(args.yes)

    for target in target_list:
        conn = sqlite3.connect(target.db_path)
        try:
            init_migrations_table(conn)
            cur = current_version(conn)
        finally:
            conn.close()
        take_backup(backup_dir, cur, "manual", target)

def cmd_restore(args):
    # Restore always requires explicit target specification; default to "main"
    # Warn if user picks "all"
    if args.target == "all":
        logger.warning("Restore with --target all is ambiguous (which backup file for each target?). "
                      "Defaulting to --target main. Use --target chatlog for the chat log DB.")
        target_list = targets("main")
    else:
        target_list = targets(args.target)

    if not target_list:
        logger.error(f"No targets matched: {args.target}")
        sys.exit(1)

    target = target_list[0]  # Restore operates on exactly one target
    if not _confirm(f"Restore {target.name} database from {args.path}? This will overwrite the current DB.", args.yes):
        logger.info("Aborted by user.")
        return
    restore_backup(args.path, target)

# ── Entry point ─────────────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(description="m3-memory database migration runner")
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("status", help="Show current version and pending migrations")
    sp.add_argument("--target", choices=["main", "chatlog", "all"], default="all",
                    help="Which DB target to operate on (default: all configured)")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("up", help="Apply pending migrations")
    sp.add_argument("--to", type=int, default=None, help="Apply up to this version (default: latest)")
    sp.add_argument("--target", choices=["main", "chatlog", "all"], default="all",
                    help="Which DB target to operate on (default: all configured)")
    sp.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompts")
    sp.set_defaults(func=cmd_up)

    sp = sub.add_parser("down", help="Roll back migrations")
    sp.add_argument("--to", type=int, required=True, help="Roll back to this version")
    sp.add_argument("--target", choices=["main", "chatlog", "all"], default="all",
                    help="Which DB target to operate on (default: all configured)")
    sp.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompts")
    sp.set_defaults(func=cmd_down)

    sp = sub.add_parser("backup", help="Take a standalone backup of the database")
    sp.add_argument("--out", type=str, default=None, help="Backup directory (overrides saved default)")
    sp.add_argument("--target", choices=["main", "chatlog", "all"], default="all",
                    help="Which DB target to operate on (default: all configured)")
    sp.add_argument("-y", "--yes", action="store_true", help="Skip interactive prompts")
    sp.set_defaults(func=cmd_backup)

    sp = sub.add_parser("restore", help="Restore the database from a backup file")
    sp.add_argument("path", help="Path to the backup .db file")
    sp.add_argument("--target", choices=["main", "chatlog", "all"], default="main",
                    help="Which DB target to restore (default: main; use chatlog for chat log DB)")
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
