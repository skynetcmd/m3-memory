#!/usr/bin/env python3
"""
Migration runner for the m3-memory SQLite databases.

Supports multiple migration targets:
    - main (agent_memory.db) — always present
    - chatlog — present when the configured chatlog DB path differs from main

Subcommands:
    status                    Show current version and pending migrations
    plan [--to N]             Preview DDL that pending migrations would run (no changes)
    up [--to N] [--dry-run]   Apply pending migrations (prompts for backup + confirmation)
    down --to N [--dry-run]   Roll back to version N (requires .down.sql files)
    backup [--out PATH]       Take a standalone backup
    restore <PATH>            Restore the database from a backup file

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


# Tables that uniquely identify the *main* schema. `agents` has been present
# since v001 of the main migrations stack and is never created by chatlog
# migrations or by memory_core lazy-init. Several other main-only tables
# (memory_history, tasks, gdpr_requests, agent_retention_policies) reinforce
# the signal; we accept any one of them as proof.
#
# The chatlog schema is a deliberate subset of main — same memory_items /
# memory_embeddings / FTS — so there is NO chatlog-only marker we can detect
# positively. Classification is "main if any of these markers exist, chatlog
# if none of them do AND the file has the storage tables". This asymmetry is
# why we cannot just symmetrically check both signatures.
_MAIN_SIGNATURE_TABLES: frozenset[str] = frozenset({
    "agents",
    "memory_history",
    "tasks",
    "gdpr_requests",
    "agent_retention_policies",
    "activity_logs",
})

# Tables present on any post-bootstrap chatlog DB. Used to distinguish
# "chatlog" from "unknown" when the main signatures are absent.
_CHATLOG_STORAGE_TABLES: frozenset[str] = frozenset({
    "memory_items",
    "memory_embeddings",
    "memory_relationships",
})


def _classify_db(db_path: str) -> str:
    """Identify a SQLite file's schema kind from its tables.

    Returns one of:
        "empty"   — file does not exist OR has no user tables (fresh DB)
        "main"    — has main-only tables (agents / memory_history / tasks)
        "chatlog" — has chatlog storage tables but no main-only tables
        "unknown" — has tables but matches neither signature

    Path-equality alone is unreliable: a user can set CHATLOG_DB_PATH to a
    path that happens to also match M3_DATABASE, or the chatlog DB can be
    pointed at by M3_DATABASE accidentally. Looking at actual schema makes
    the runner robust to whatever path-resolution surprises arise.

    Note: a chatlog DB that has been *lazy-touched* by memory_core picks up
    memory_items + memory_embeddings + chroma_sync_queue, but never the
    main-only tables in _MAIN_SIGNATURE_TABLES. So presence-of-storage
    plus absence-of-main-signatures is a reliable "this is chatlog".
    """
    if not os.path.exists(db_path):
        return "empty"
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        # Locked/missing/corrupt — be conservative: callers should treat
        # "unknown" as "do not auto-migrate".
        return "unknown"
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    except sqlite3.Error:
        return "unknown"
    finally:
        conn.close()
    tables = {r[0] for r in rows}
    if not tables:
        return "empty"
    has_main = bool(tables & _MAIN_SIGNATURE_TABLES)
    has_storage = bool(tables & _CHATLOG_STORAGE_TABLES)
    if has_main:
        return "main"
    if has_storage:
        return "chatlog"
    return "unknown"


def targets(selected: str = "all") -> List[MigrationTarget]:
    """
    Returns a list of MigrationTarget objects based on selection.

    Args:
        selected: "main", "chatlog", or "all"

    Returns:
        List of MigrationTarget objects configured for the selection.
        If chatlog_config import fails, returns only main.

    Hardening: when the resolved "main" path actually points at a chatlog DB
    (M3_DATABASE misconfigured, or a user pointing the runner at the wrong
    file), the main target is *removed* from the result and an error is
    logged. Applying main-stack DDL to a chatlog DB is never the right call
    — schema_versions rows will be wrong, and most main migrations will
    fail with "no such column" / "no such table" errors after partial
    application leaves the chatlog DB in a half-migrated state.
    """
    # Honor M3_DATABASE for the main target so migrations land on the DB the
    # caller is targeting, not always agent_memory.db.
    try:
        from m3_sdk import resolve_db_path as _resolve_main
        main_path = _resolve_main(None)
    except ImportError:
        main_path = DB_PATH

    main_kind = _classify_db(main_path)
    main_target_valid = main_kind in ("main", "empty")
    if not main_target_valid:
        # Defensive: refuse to attach main migrations dir to a chatlog DB.
        # When --target main is explicit, this is a hard error (return []).
        # When --target all is in effect, we drop main but still try chatlog.
        if main_kind == "chatlog":
            logger.error(
                "Refusing to apply main migrations to %s — schema fingerprint says "
                "this is a chatlog DB. Set M3_DATABASE to your main agent_memory.db, "
                "or use --target chatlog.", main_path,
            )
        else:  # unknown
            logger.error(
                "Refusing to apply main migrations to %s — schema fingerprint is "
                "unrecognised (kind=%s). Inspect the file or restore from backup.",
                main_path, main_kind,
            )

    main_target = (
        MigrationTarget(name="main", db_path=main_path, migrations_dir=MIGRATIONS_DIR)
        if main_target_valid else None
    )

    if selected == "main":
        return [main_target] if main_target else []

    if selected == "all" or selected == "chatlog":
        result: List[MigrationTarget] = []
        if main_target:
            result.append(main_target)

        # Chatlog migrations only run when chatlog lives in a different file
        # than the main DB. With path-equality unification, same-file means
        # the chatlog migrations would already have been applied by the main
        # migration run.
        try:
            from chatlog_config import CHATLOG_MIGRATIONS_DIR, chatlog_db_path
            chatlog_path = os.path.abspath(chatlog_db_path())
            if chatlog_path != os.path.abspath(main_path):
                chatlog_kind = _classify_db(chatlog_path)
                if chatlog_kind in ("chatlog", "empty"):
                    chatlog_target = MigrationTarget(
                        name="chatlog",
                        db_path=chatlog_path,
                        migrations_dir=CHATLOG_MIGRATIONS_DIR,
                    )
                    if selected == "chatlog":
                        return [chatlog_target]
                    result.append(chatlog_target)
                else:  # main / unknown
                    logger.error(
                        "Refusing to apply chatlog migrations to %s — schema "
                        "fingerprint says kind=%s. Set CHATLOG_DB_PATH to a "
                        "real chatlog DB or omit it.", chatlog_path, chatlog_kind,
                    )
                    if selected == "chatlog":
                        return []
            elif main_kind == "chatlog" and selected in ("chatlog", "all"):
                # Recovery path: M3_DATABASE was misdirected at the chatlog DB,
                # so chatlog_db_path() also resolves to it and main was refused
                # above. Run chatlog migrations against this path so the user's
                # data still gets schema fixes instead of being stranded.
                logger.warning(
                    "Main path resolves to a chatlog DB; routing chatlog migrations "
                    "to %s. Set M3_DATABASE to your main agent_memory.db to avoid "
                    "this fallback.", chatlog_path,
                )
                chatlog_target = MigrationTarget(
                    name="chatlog",
                    db_path=chatlog_path,
                    migrations_dir=CHATLOG_MIGRATIONS_DIR,
                )
                if selected == "chatlog":
                    return [chatlog_target]
                result.append(chatlog_target)
            elif selected == "chatlog":
                logger.warning("Chatlog DB path equals main DB path; chatlog migrations run as part of main.")
                return []
        except ImportError:
            logger.warning("chatlog_config module not found. Operating on main DB only.")
            if selected == "chatlog":
                logger.error("Cannot use --target chatlog without chatlog_config module.")
                return []

        return result

    logger.error(f"Unknown target selection: {selected}")
    return [main_target] if main_target else []

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
    # Prefer SQLite's online backup API — it takes a consistent snapshot even
    # while other connections are writing. Falls back to file-copy if the
    # source can't be opened (rare on a locked DB on Windows).
    try:
        src_conn = sqlite3.connect(target.db_path)
        try:
            dst_conn = sqlite3.connect(dst)
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
    except sqlite3.Error as e:
        logger.warning(f"Online backup failed ({e}); falling back to file copy.")
        shutil.copy2(target.db_path, dst)
        for suffix in ("-wal", "-shm", "-journal"):
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
        for suffix in ("-wal", "-shm", "-journal"):
            if os.path.exists(target.db_path + suffix):
                shutil.move(target.db_path + suffix, sidecar + suffix)
    try:
        shutil.copy2(backup_path, target.db_path)
        for suffix in ("-wal", "-shm", "-journal"):
            src = backup_path + suffix
            if os.path.exists(src):
                shutil.copy2(src, target.db_path + suffix)
        verify = sqlite3.connect(target.db_path)
        try:
            result = verify.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError(f"integrity_check failed: {result}")
        finally:
            verify.close()
        logger.info(f"Restored {target.db_path} from {backup_path} (integrity_check ok)")
    except Exception as e:
        logger.error(f"Restore failed: {e}")
        sys.exit(1)

# ── Migration discovery ─────────────────────────────────────────────────────

_FNAME_RE = re.compile(r"^(\d+)_(.+?)(?:\.(up|down))?\.sql$")

def discover_migrations(migrations_dir: str):
    """
    Returns a dict: { version: { 'name': str, 'up': path|None, 'down': path|None } }.
    Legacy NNN_name.sql files map to 'up' with down=None.

    Filesystems do not guarantee `glob` ordering, so we sort filenames before
    iterating — this makes version conflicts (e.g. two files with the same
    NNN prefix) deterministic, with later sort-order winning and a warning
    emitted.

    Args:
        migrations_dir: Directory to scan for migration files.
    """
    out: dict[int, dict[str, Optional[str]]] = {}
    seen_paths: dict[tuple[int, str], str] = {}
    for filepath in sorted(glob.glob(os.path.join(migrations_dir, "*.sql"))):
        fname = os.path.basename(filepath)
        m = _FNAME_RE.match(fname)
        if not m:
            logger.warning(f"Skipping malformed migration file: {fname}")
            continue
        version = int(m.group(1))
        name = m.group(2)
        direction = m.group(3) or "up"  # 'up', 'down', or legacy (treated as 'up')
        key = (version, direction)
        if key in seen_paths:
            logger.warning(
                f"Duplicate migration for v{version:03d} {direction}: "
                f"{os.path.basename(seen_paths[key])} vs {fname} — using {fname}."
            )
        seen_paths[key] = filepath
        entry = out.setdefault(version, {"name": name, "up": None, "down": None})
        if direction == "down":
            entry["down"] = filepath
        else:
            entry["up"] = filepath
            entry["name"] = name
    return out

def init_migrations_table(conn):
    # FK enforcement matches the runtime pool settings in m3_sdk so that
    # migrations which rely on ON DELETE CASCADE (e.g. 002) behave identically
    # when run here vs. when the app is running.
    conn.execute("PRAGMA foreign_keys = ON")
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
    """Run a migration script atomically.

    sqlite3's `executescript` issues an implicit COMMIT before the script and
    does not itself run in a transaction, so a plain `BEGIN` wrapper is
    discarded. We instead wrap the script in a SAVEPOINT — SAVEPOINT semantics
    survive executescript's implicit commit and give us all-or-nothing
    behaviour. Migration files must NOT contain their own COMMIT/ROLLBACK
    statements (the repo convention; enforced by _validate_migration_sql).
    """
    with open(filepath, "r", encoding="utf-8") as f:
        sql_script = f.read()
    _validate_migration_sql(filepath, sql_script)
    wrapped = (
        "SAVEPOINT mig_apply;\n"
        f"{sql_script}\n"
        "RELEASE SAVEPOINT mig_apply;\n"
    )
    try:
        conn.executescript(wrapped)
    except sqlite3.Error:
        # Best-effort rollback of the savepoint; swallow if the script never
        # got far enough to open it.
        try: conn.execute("ROLLBACK TO SAVEPOINT mig_apply")
        except sqlite3.Error: pass
        try: conn.execute("RELEASE SAVEPOINT mig_apply")
        except sqlite3.Error: pass
        raise

_FORBIDDEN_STATEMENTS_RE = re.compile(
    r"(?im)^\s*(COMMIT|ROLLBACK|BEGIN|END\s+TRANSACTION)\b"
)

def _validate_migration_sql(filepath: str, sql: str) -> None:
    """Reject migration files that issue their own COMMIT / ROLLBACK / BEGIN.

    Those statements break our SAVEPOINT-based atomicity wrapper. Strips
    `-- ...` and `/* ... */` comments before checking so commentary using
    those words doesn't trip the detector.
    """
    stripped = re.sub(r"--[^\n]*", "", sql)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)
    m = _FORBIDDEN_STATEMENTS_RE.search(stripped)
    if m:
        raise ValueError(
            f"Migration {os.path.basename(filepath)} contains a top-level "
            f"{m.group(1).upper()} statement, which conflicts with the "
            f"SAVEPOINT wrapper used by migrate_memory.py. Remove it — "
            f"migrations run inside an implicit transaction."
        )

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
        try: conn.rollback()
        except sqlite3.Error: pass
        err = str(e).lower()
        if "duplicate column name" in err or "already exists" in err:
            # Preserve legacy idempotency: schema already has what this migration adds
            logger.warning(
                f"v{version:03d} partial-apply detected ({e}); marking applied. "
                "Inspect schema if this recurs."
            )
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
        try: conn.rollback()
        except sqlite3.Error: pass
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

def _print_pending_plan(pending: list[int], migs: dict, direction: str) -> None:
    """Pretty-print the SQL that would run for each pending/to-revert version."""
    print("\n--- Migration plan ---")
    for v in pending:
        entry = migs[v]
        path = entry["up"] if direction == "up" else entry["down"]
        if not path:
            print(f"\n# v{v:03d} ({direction}): MISSING")
            continue
        print(f"\n# v{v:03d} ({direction}): {os.path.basename(path)}")
        try:
            with open(path, "r", encoding="utf-8") as f:
                print(f.read().rstrip())
        except OSError as e:
            print(f"# <error reading {path}: {e}>")
    print("\n--- end of plan ---\n")

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

            if getattr(args, "dry_run", False):
                _print_pending_plan(pending, migs, "up")
                logger.info(f"{target.name}: Dry run — no changes made.")
                continue

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

            if getattr(args, "dry_run", False):
                _print_pending_plan(to_revert, migs, "down")
                logger.info(f"{target.name}: Dry run — no changes made.")
                continue

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

def cmd_plan(args):
    """Dry-run preview: print DDL for every pending migration without touching the DB."""
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
            migs = discover_migrations(target.migrations_dir)
            all_versions = sorted(migs.keys())
            target_ver = args.to if args.to is not None else (max(all_versions) if all_versions else 0)
            pending = [v for v in all_versions if v not in applied and v <= target_ver]
            if not pending:
                logger.info(f"{target.name}: No pending migrations.")
                continue
            _print_pending_plan(pending, migs, "up")
        finally:
            conn.close()

# ── Entry point ─────────────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(description="m3-memory database migration runner")
    # Top-level --database sets M3_DATABASE for the duration of the run so
    # targets() picks up the override. Subcommand --target still selects which
    # DB family (main / chatlog) to touch.
    p.add_argument(
        "--database",
        default=None,
        metavar="PATH",
        help="SQLite main-DB path override. Sets M3_DATABASE for this run. "
             "Default: memory/agent_memory.db.",
    )
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
    sp.add_argument("--dry-run", action="store_true", help="Print the plan + DDL without applying anything")
    sp.set_defaults(func=cmd_up)

    sp = sub.add_parser("down", help="Roll back migrations")
    sp.add_argument("--to", type=int, required=True, help="Roll back to this version")
    sp.add_argument("--target", choices=["main", "chatlog", "all"], default="all",
                    help="Which DB target to operate on (default: all configured)")
    sp.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompts")
    sp.add_argument("--dry-run", action="store_true", help="Print the plan + DDL without reverting anything")
    sp.set_defaults(func=cmd_down)

    sp = sub.add_parser("plan", help="Preview DDL that pending migrations would run (no changes)")
    sp.add_argument("--to", type=int, default=None, help="Plan up to this version (default: latest)")
    sp.add_argument("--target", choices=["main", "chatlog", "all"], default="all",
                    help="Which DB target to operate on (default: all configured)")
    sp.set_defaults(func=cmd_plan)

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

    if args.database:
        os.environ["M3_DATABASE"] = args.database

    # Back-compat: `python migrate_memory.py` with no args == `up` (the old behavior),
    # but now with the new prompts. Scripts that relied on the old no-arg invocation
    # will still work, just interactively.
    if args.command is None:
        args = parser.parse_args(["up"])

    args.func(args)

if __name__ == "__main__":
    main()
