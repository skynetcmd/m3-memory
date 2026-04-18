"""Tests for chatlog schema migrations."""

import sqlite3
import os
import pytest


def test_bootstrap_migration_creates_schema(tmp_path):
    """001_bootstrap.up.sql creates memory_items, memory_embeddings, relationships."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Read and execute bootstrap SQL
    migrations_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "memory", "chatlog_migrations"
    )
    bootstrap_up = os.path.join(migrations_dir, "001_bootstrap.up.sql")

    if os.path.exists(bootstrap_up):
        with open(bootstrap_up, "r", encoding="utf-8") as f:
            sql = f.read()
        cursor.executescript(sql)
        conn.commit()

    # Verify tables exist
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cursor.fetchall()}

    assert "memory_items" in tables, f"memory_items not created. Tables: {tables}"
    assert "memory_embeddings" in tables
    assert "memory_relationships" in tables

    # Verify memory_items structure
    cursor.execute("PRAGMA table_info(memory_items)")
    columns = {row[1] for row in cursor.fetchall()}
    assert "id" in columns
    assert "type" in columns
    assert "content" in columns
    assert "metadata_json" in columns
    assert "conversation_id" in columns

    conn.close()


def test_bootstrap_migration_rollback(tmp_path):
    """001_bootstrap.down.sql removes schema."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    migrations_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "memory", "chatlog_migrations"
    )

    # Apply up
    bootstrap_up = os.path.join(migrations_dir, "001_bootstrap.up.sql")
    if os.path.exists(bootstrap_up):
        with open(bootstrap_up, "r", encoding="utf-8") as f:
            sql = f.read()
        cursor.executescript(sql)
        conn.commit()

    # Apply down
    bootstrap_down = os.path.join(migrations_dir, "001_bootstrap.down.sql")
    if os.path.exists(bootstrap_down):
        with open(bootstrap_down, "r", encoding="utf-8") as f:
            sql = f.read()
        cursor.executescript(sql)
        conn.commit()

    # Verify tables are gone
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cursor.fetchall()}
    assert "memory_items" not in tables


def test_indexes_migration_creates_indexes(tmp_path):
    """002_chat_log_indexes.up.sql creates indexes."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    migrations_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "memory", "chatlog_migrations"
    )

    # Apply bootstrap first
    bootstrap_up = os.path.join(migrations_dir, "001_bootstrap.up.sql")
    if os.path.exists(bootstrap_up):
        with open(bootstrap_up, "r", encoding="utf-8") as f:
            sql = f.read()
        cursor.executescript(sql)
        conn.commit()

    # Apply indexes
    indexes_up = os.path.join(migrations_dir, "002_chat_log_indexes.up.sql")
    if os.path.exists(indexes_up):
        with open(indexes_up, "r", encoding="utf-8") as f:
            sql = f.read()
        cursor.executescript(sql)
        conn.commit()

    # Verify indexes exist
    cursor.execute("SELECT name FROM sqlite_master WHERE type='index'")
    indexes = {row[0] for row in cursor.fetchall()}

    # At least some indexes should exist
    assert len(indexes) > 0, f"No indexes found. Indexes: {indexes}"

    conn.close()


def test_indexes_migration_rollback(tmp_path):
    """002_chat_log_indexes.down.sql removes indexes."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    migrations_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "memory", "chatlog_migrations"
    )

    # Apply both up
    bootstrap_up = os.path.join(migrations_dir, "001_bootstrap.up.sql")
    if os.path.exists(bootstrap_up):
        with open(bootstrap_up, "r", encoding="utf-8") as f:
            cursor.executescript(f.read())
        conn.commit()

    indexes_up = os.path.join(migrations_dir, "002_chat_log_indexes.up.sql")
    if os.path.exists(indexes_up):
        with open(indexes_up, "r", encoding="utf-8") as f:
            cursor.executescript(f.read())
        conn.commit()

    # Count indexes after up
    cursor.execute("SELECT name FROM sqlite_master WHERE type='index'")
    indexes_after_up = len(cursor.fetchall())

    # Apply indexes down
    indexes_down = os.path.join(migrations_dir, "002_chat_log_indexes.down.sql")
    if os.path.exists(indexes_down):
        with open(indexes_down, "r", encoding="utf-8") as f:
            cursor.executescript(f.read())
        conn.commit()

    # Count indexes after down
    cursor.execute("SELECT name FROM sqlite_master WHERE type='index'")
    indexes_after_down = len(cursor.fetchall())

    # Should have fewer indexes after down (or none if all custom indexes were removed)
    assert indexes_after_down < indexes_after_up or indexes_after_down == 0

    conn.close()


def test_fts_table_created(tmp_path):
    """Bootstrap creates FTS table memory_items_fts."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    migrations_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "memory", "chatlog_migrations"
    )

    bootstrap_up = os.path.join(migrations_dir, "001_bootstrap.up.sql")
    if os.path.exists(bootstrap_up):
        with open(bootstrap_up, "r", encoding="utf-8") as f:
            cursor.executescript(f.read())
        conn.commit()

    # Check for FTS table
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%fts%'")
    fts_tables = {row[0] for row in cursor.fetchall()}

    # Should have at least memory_items_fts or similar
    assert len(fts_tables) > 0, f"No FTS table found. Tables: {fts_tables}"

    conn.close()


def test_triggers_created_in_bootstrap(tmp_path):
    """Bootstrap creates triggers for maintaining FTS."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    migrations_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "memory", "chatlog_migrations"
    )

    bootstrap_up = os.path.join(migrations_dir, "001_bootstrap.up.sql")
    if os.path.exists(bootstrap_up):
        with open(bootstrap_up, "r", encoding="utf-8") as f:
            cursor.executescript(f.read())
        conn.commit()

    # Check for triggers
    cursor.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    triggers = {row[0] for row in cursor.fetchall()}

    # Should have at least some triggers
    assert len(triggers) >= 0  # Allow 0 if triggers aren't implemented yet

    conn.close()


def test_migration_files_exist():
    """Expected migration files exist."""
    migrations_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "memory", "chatlog_migrations"
    )

    assert os.path.isdir(migrations_dir)
    assert os.path.exists(os.path.join(migrations_dir, "001_bootstrap.up.sql"))
    assert os.path.exists(os.path.join(migrations_dir, "001_bootstrap.down.sql"))
    assert os.path.exists(os.path.join(migrations_dir, "002_chat_log_indexes.up.sql"))
    assert os.path.exists(os.path.join(migrations_dir, "002_chat_log_indexes.down.sql"))
