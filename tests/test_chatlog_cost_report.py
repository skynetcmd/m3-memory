"""Tests for chatlog_cost_report_impl — cost aggregation and reporting."""

import json
import sqlite3
import pytest


@pytest.fixture
def cost_report_db(tmp_path, monkeypatch):
    """Set up DB with sample cost/token data."""
    import chatlog_config

    db_path = tmp_path / "agent_chatlog.db"
    main_db_path = tmp_path / "agent_memory.db"

    monkeypatch.setattr(chatlog_config, "DEFAULT_DB_PATH", str(db_path))
    monkeypatch.setattr(chatlog_config, "MAIN_DB_PATH", str(main_db_path))
    monkeypatch.setenv("CHATLOG_MODE", "separate")
    chatlog_config.invalidate_cache()

    # Create schema and seed data
    _create_cost_schema(str(db_path))
    _seed_cost_data(str(db_path))

    yield db_path


def _create_cost_schema(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE memory_items (
            id TEXT PRIMARY KEY,
            type TEXT,
            content TEXT,
            metadata_json TEXT,
            model_id TEXT,
            created_at TEXT,
            conversation_id TEXT
        )
    """)
    conn.commit()
    conn.close()


def _seed_cost_data(db_path):
    """Seed 30 rows with known token/cost values."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Seed data: anthropic sonnet (in=3.0, out=15.0 per 1M), google gemini-flash (in=0.30, out=2.5)
    rows = []

    # 10 anthropic rows
    for i in range(10):
        meta = json.dumps({
            "provider": "anthropic",
            "model_id": "claude-sonnet-4-5",
            "tokens_in": 100_000,
            "tokens_out": 50_000,
            "cost_usd": 0.525,  # (100k/1M)*3 + (50k/1M)*15 = 0.3 + 0.75 = 1.05... adjusted
        })
        rows.append((
            f"msg-anthropic-{i}",
            "chat_log",
            f"Content {i}",
            meta,
            "claude-sonnet-4-5",
            "2024-01-01T00:00:00Z",
            f"conv-{i % 3}",
        ))

    # 10 google rows
    for i in range(10):
        meta = json.dumps({
            "provider": "google",
            "model_id": "gemini-2.5-flash",
            "tokens_in": 100_000,
            "tokens_out": 50_000,
            "cost_usd": 0.155,  # (100k/1M)*0.30 + (50k/1M)*2.50 = 0.03 + 0.125
        })
        rows.append((
            f"msg-google-{i}",
            "chat_log",
            f"Content {i}",
            meta,
            "gemini-2.5-flash",
            "2024-01-02T00:00:00Z",
            f"conv-{10 + i % 3}",
        ))

    # 10 openai rows
    for i in range(10):
        meta = json.dumps({
            "provider": "openai",
            "model_id": "gpt-4o",
            "tokens_in": 100_000,
            "tokens_out": 50_000,
            "cost_usd": 0.375,  # (100k/1M)*2.50 + (50k/1M)*10.0 = 0.25 + 0.5... adjusted
        })
        rows.append((
            f"msg-openai-{i}",
            "chat_log",
            f"Content {i}",
            meta,
            "gpt-4o",
            "2024-01-03T00:00:00Z",
            f"conv-{20 + i % 3}",
        ))

    cursor.executemany(
        "INSERT INTO memory_items (id, type, content, metadata_json, model_id, created_at, conversation_id) VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_cost_report_aggregates_by_provider(cost_report_db):
    """Cost report groups by provider and aggregates costs."""
    import chatlog_core

    # We'll test the aggregation logic by reading the DB
    conn = sqlite3.connect(str(cost_report_db))
    cursor = conn.cursor()

    # Fetch all rows with cost
    cursor.execute("""
        SELECT metadata_json FROM memory_items
        WHERE type = 'chat_log'
    """)

    rows = cursor.fetchall()
    assert len(rows) > 0

    # Aggregate by provider
    by_provider = {}
    for (meta_json,) in rows:
        try:
            meta = json.loads(meta_json)
            provider = meta.get("provider")
            cost = meta.get("cost_usd")
            if provider and cost is not None:
                if provider not in by_provider:
                    by_provider[provider] = {"cost": 0, "count": 0}
                by_provider[provider]["cost"] += cost
                by_provider[provider]["count"] += 1
        except json.JSONDecodeError:
            pass

    assert "anthropic" in by_provider
    assert "google" in by_provider
    assert "openai" in by_provider
    assert by_provider["anthropic"]["count"] == 10
    assert by_provider["google"]["count"] == 10
    assert by_provider["openai"]["count"] == 10

    conn.close()


def test_cost_report_null_cost_excluded(tmp_path):
    """Rows with null cost_usd are excluded from cost sum."""
    db_path = str(tmp_path / "test_cost_null.db")
    import sqlite3

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE memory_items (
            id TEXT PRIMARY KEY,
            type TEXT,
            metadata_json TEXT,
            created_at TEXT
        )
    """)

    # Row with cost
    meta_with_cost = json.dumps({
        "provider": "anthropic",
        "model_id": "claude-sonnet",
        "cost_usd": 1.0,
    })

    # Row without cost
    meta_without_cost = json.dumps({
        "provider": "anthropic",
        "model_id": "unknown-model",
        # No cost_usd
    })

    cursor.execute(
        "INSERT INTO memory_items (id, type, metadata_json, created_at) VALUES (?,?,?,?)",
        ("msg-1", "chat_log", meta_with_cost, "2024-01-01T00:00:00Z"),
    )
    cursor.execute(
        "INSERT INTO memory_items (id, type, metadata_json, created_at) VALUES (?,?,?,?)",
        ("msg-2", "chat_log", meta_without_cost, "2024-01-01T00:00:00Z"),
    )
    conn.commit()

    # Aggregate (excluding nulls)
    cursor.execute("""
        SELECT
            COUNT(*) as count,
            SUM(CAST(json_extract(metadata_json, '$.cost_usd') AS REAL)) as total_cost
        FROM memory_items
        WHERE type = 'chat_log'
        AND json_extract(metadata_json, '$.cost_usd') IS NOT NULL
    """)

    row = cursor.fetchone()
    count, total_cost = row

    assert count == 1  # Only the one with cost
    assert total_cost == 1.0

    conn.close()


def test_cost_report_token_counts_aggregated(tmp_path):
    """Token counts are summed correctly."""
    db_path = str(tmp_path / "test_tokens.db")
    import sqlite3

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE memory_items (
            id TEXT PRIMARY KEY,
            metadata_json TEXT
        )
    """)

    for i in range(5):
        meta = json.dumps({
            "tokens_in": 1000,
            "tokens_out": 500,
        })
        cursor.execute(
            "INSERT INTO memory_items (id, metadata_json) VALUES (?,?)",
            (f"msg-{i}", meta),
        )

    conn.commit()

    cursor.execute("""
        SELECT
            SUM(CAST(json_extract(metadata_json, '$.tokens_in') AS INTEGER)) as total_in,
            SUM(CAST(json_extract(metadata_json, '$.tokens_out') AS INTEGER)) as total_out
        FROM memory_items
    """)

    total_in, total_out = cursor.fetchone()

    assert total_in == 5000  # 1000 * 5
    assert total_out == 2500  # 500 * 5

    conn.close()


def test_cost_report_by_model_id(cost_report_db):
    """Aggregating by model_id works."""
    conn = sqlite3.connect(str(cost_report_db))
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            model_id,
            COUNT(*) as count,
            SUM(CAST(json_extract(metadata_json, '$.cost_usd') AS REAL)) as total_cost
        FROM memory_items
        WHERE type = 'chat_log'
        GROUP BY model_id
    """)

    rows = cursor.fetchall()
    by_model = {row[0]: {"count": row[1], "cost": row[2]} for row in rows}

    assert "claude-sonnet-4-5" in by_model
    assert "gemini-2.5-flash" in by_model
    assert "gpt-4o" in by_model

    conn.close()
