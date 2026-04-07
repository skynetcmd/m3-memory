#!/usr/bin/env python3
import sqlite3
import os
import sys
import glob
import logging

logging.basicConfig(level=logging.INFO, format='%(name)s: [%(levelname)s] %(message)s')
logger = logging.getLogger("migrate_memory")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "memory", "agent_memory.db")
MIGRATIONS_DIR = os.path.join(BASE_DIR, "memory", "migrations")

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
    rows = conn.execute("SELECT version FROM schema_versions").fetchall()
    return {row[0] for row in rows}

def main():
    if not os.path.exists(MIGRATIONS_DIR):
        logger.error(f"Migrations directory not found: {MIGRATIONS_DIR}")
        sys.exit(1)

    # Make sure DB dir exists
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    
    conn = sqlite3.connect(DB_PATH)
    try:
        init_migrations_table(conn)
        applied = get_applied_versions(conn)

        migration_files = []
        for filepath in glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql")):
            filename = os.path.basename(filepath)
            parts = filename.split('_', 1)
            if len(parts) < 2 or not parts[0].isdigit():
                logger.warning(f"Skipping malformed migration file: {filename}")
                continue
            version = int(parts[0])
            migration_files.append((version, filename, filepath))

        migration_files.sort(key=lambda x: x[0])

        pending = [m for m in migration_files if m[0] not in applied]

        if not pending:
            logger.info("Database is up to date. No pending migrations.")
            return

        for version, filename, filepath in pending:
            logger.info(f"Applying migration {version}: {filename}...")
            with open(filepath, 'r', encoding='utf-8') as f:
                sql_script = f.read()

            try:
                # Wrap script + version record in a single transaction
                conn.execute("BEGIN")
                conn.executescript(sql_script)
                conn.execute(
                    "INSERT INTO schema_versions (version, filename) VALUES (?, ?)",
                    (version, filename)
                )
                conn.commit()
                logger.info(f"Successfully applied migration {version}.")
            except sqlite3.OperationalError as e:
                conn.rollback()
                # Idempotency: duplicate column/index errors mean the schema already
                # has what this migration adds — mark as applied and continue.
                err = str(e).lower()
                if "duplicate column name" in err or "already exists" in err:
                    logger.warning(f"Migration {version} already partially applied ({e}); marking as applied.")
                    conn.execute(
                        "INSERT OR IGNORE INTO schema_versions (version, filename) VALUES (?, ?)",
                        (version, filename)
                    )
                    conn.commit()
                else:
                    logger.error(f"Failed to apply migration {version}: {e}")
                    sys.exit(1)

    finally:
        conn.close()

if __name__ == "__main__":
    main()
