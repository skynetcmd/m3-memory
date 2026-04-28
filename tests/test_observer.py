"""Tests for bin/run_observer.py — parse_observations + write path.

Network is mocked via httpx.MockTransport. We assert:
  - parse_observations handles malformed/missing/edge-case JSON gracefully
  - call_observer extracts text from Anthropic content blocks
  - write_observation populates valid_from with referenced_date,
    metadata_json with the audit fields
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "bin"))


@pytest.fixture
def bench_db(tmp_path, monkeypatch):
    """Spin up an isolated test DB with the minimum tables Observer needs.

    Bypasses the full migration chain (which has a known BEGIN/SAVEPOINT
    conflict in 002_enforce_relationships.sql) and creates only the
    memory_items / memory_relationships / memory_embeddings tables that
    write_observation touches. Sufficient for unit-level testing of the
    Observer write path.
    """
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE memory_items (
            id TEXT PRIMARY KEY,
            type TEXT,
            title TEXT,
            content TEXT,
            metadata_json TEXT,
            agent_id TEXT,
            model_id TEXT,
            change_agent TEXT,
            importance REAL,
            source TEXT,
            origin_device TEXT,
            is_deleted INTEGER DEFAULT 0,
            expires_at TEXT,
            decay_rate REAL,
            created_at TEXT,
            updated_at TEXT,
            last_accessed_at TEXT,
            access_count INTEGER DEFAULT 0,
            user_id TEXT,
            scope TEXT,
            valid_from TEXT,
            valid_to TEXT,
            content_hash TEXT,
            read_at TEXT,
            conversation_id TEXT,
            refresh_on TEXT,
            refresh_reason TEXT,
            variant TEXT
        );
        CREATE TABLE memory_relationships (
            id TEXT PRIMARY KEY,
            from_id TEXT,
            to_id TEXT,
            relationship_type TEXT,
            created_at TEXT,
            metadata TEXT
        );
        CREATE TABLE memory_embeddings (
            id TEXT PRIMARY KEY,
            memory_id TEXT,
            embedding BLOB,
            embed_model TEXT,
            dim INTEGER,
            created_at TEXT,
            vector_kind TEXT DEFAULT 'default'
        );
        CREATE TABLE memory_history (
            id TEXT PRIMARY KEY,
            memory_id TEXT,
            action TEXT,
            old_value TEXT,
            new_value TEXT,
            field TEXT,
            changed_by TEXT,
            changed_at TEXT
        );
        CREATE TABLE observation_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL UNIQUE,
            user_id TEXT,
            enqueued_at TEXT,
            attempts INTEGER DEFAULT 0,
            last_error TEXT,
            last_attempt_at TEXT
        );
        CREATE TABLE chroma_sync_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT,
            operation TEXT,
            enqueued_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
    """)
    conn.commit()
    conn.close()
    return db_path


def test_parse_observations_valid_input():
    import run_observer
    text = json.dumps({
        "observations": [
            {"text": "User went to Paris.",
             "observation_date": "2023-05-22",
             "referenced_date": "2023-05-21",
             "relative_date": "yesterday",
             "confidence": 0.95,
             "supersedes_hint": None}
        ]
    })
    out = run_observer.parse_observations(text)
    assert len(out) == 1
    assert out[0]["text"] == "User went to Paris."
    assert out[0]["observation_date"] == "2023-05-22"
    assert out[0]["referenced_date"] == "2023-05-21"
    assert out[0]["relative_date"] == "yesterday"
    assert out[0]["confidence"] == 0.95


def test_parse_observations_strips_code_fences():
    import run_observer
    text = '```json\n{"observations": [{"text": "abc def",' \
           ' "observation_date": "2023-01-01"}]}\n```'
    out = run_observer.parse_observations(text)
    assert len(out) == 1
    assert out[0]["text"] == "abc def"


def test_parse_observations_drops_missing_text():
    import run_observer
    text = json.dumps({"observations": [
        {"observation_date": "2023-01-01"},  # no text
        {"text": "x", "observation_date": "2023-01-01"},  # too short
        {"text": "User valid.", "observation_date": "2023-01-01"},
    ]})
    out = run_observer.parse_observations(text)
    assert len(out) == 1
    assert out[0]["text"] == "User valid."


def test_parse_observations_drops_low_confidence():
    import run_observer
    text = json.dumps({"observations": [
        {"text": "ok confidence", "observation_date": "2023-01-01", "confidence": 0.7},
        {"text": "low confidence", "observation_date": "2023-01-01", "confidence": 0.5},
    ]})
    out = run_observer.parse_observations(text)
    assert len(out) == 1
    assert out[0]["text"] == "ok confidence"


def test_parse_observations_normalizes_date_separators():
    import run_observer
    text = json.dumps({"observations": [
        {"text": "User test.",
         "observation_date": "2023/05/22 (Mon) 14:30",
         "referenced_date": "2023/05/21"}
    ]})
    out = run_observer.parse_observations(text)
    assert out[0]["observation_date"] == "2023-05-22"
    assert out[0]["referenced_date"] == "2023-05-21"


def test_parse_observations_coerces_string_null():
    """The model sometimes emits 'null' as a string rather than JSON null."""
    import run_observer
    # When relative_date is the string "null", the parser should coerce to None.
    text = '{"observations": [{"text": "User x test.", ' \
           '"observation_date": "2023-01-01", "relative_date": "null"}]}'
    out = run_observer.parse_observations(text)
    assert out[0]["relative_date"] is None


def test_parse_observations_malformed_json_returns_empty():
    import run_observer
    assert run_observer.parse_observations("not json") == []
    assert run_observer.parse_observations("{not parseable}") == []
    assert run_observer.parse_observations("") == []


def test_parse_observations_non_list_observations_field():
    import run_observer
    text = json.dumps({"observations": "not a list"})
    assert run_observer.parse_observations(text) == []


def test_call_observer_anthropic_shape():
    """call_observer should send Anthropic-shape body and parse content blocks."""
    import run_observer

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={
            "content": [
                {"type": "text", "text": json.dumps({
                    "observations": [
                        {"text": "User did a thing.",
                         "observation_date": "2023-01-01",
                         "referenced_date": "2023-01-01"}
                    ]
                })}
            ],
            "usage": {"input_tokens": 50, "output_tokens": 30},
            "stop_reason": "end_turn",
        })

    transport = httpx.MockTransport(handler)

    class _Profile:
        backend = "anthropic"
        url = "http://mock.test/v1/messages"
        model = "test-model"
        system = "extract facts"
        max_tokens = 1024
        input_max_chars = 20000
        temperature = 0
        timeout_s = 30.0
        anthropic_version = "2023-06-01"

    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            return await run_observer.call_observer(
                {"session_date": "2023-01-01", "turns": []},
                _Profile(), client, token="test-token",
            )

    out = asyncio.run(go())
    # Verify the Anthropic shape was used
    assert "system" in captured["json"]
    assert captured["json"]["system"] == "extract facts"
    assert captured["json"]["messages"][0]["role"] == "user"
    assert captured["headers"]["x-api-key"] == "test-token"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    # Verify parsing
    assert len(out) == 1
    assert out[0]["text"] == "User did a thing."


def test_call_observer_openai_fallback():
    """When backend='openai', call_observer should send chat/completions shape."""
    import run_observer

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({
                "observations": [
                    {"text": "User OAI test.",
                     "observation_date": "2023-01-01"}
                ]
            })}}]
        })

    transport = httpx.MockTransport(handler)

    class _Profile:
        backend = "openai"
        url = "http://mock.test/v1/chat/completions"
        model = "test-model"
        system = "extract facts"
        max_tokens = 1024
        input_max_chars = 20000
        temperature = 0
        timeout_s = 30.0

    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            return await run_observer.call_observer(
                {"session_date": "2023-01-01", "turns": []},
                _Profile(), client, token="test-token",
            )

    out = asyncio.run(go())
    # OpenAI shape: messages list with system + user
    assert captured["json"]["messages"][0]["role"] == "system"
    assert captured["json"]["messages"][1]["role"] == "user"
    assert len(out) == 1
    assert out[0]["text"] == "User OAI test."


def test_write_observation_populates_three_dates(bench_db, monkeypatch):
    """write_observation should set valid_from = referenced_date and store
    the audit fields (observation_date, relative_date, supersedes_hint) in
    metadata_json."""
    import memory_core
    import run_observer
    # Bypass _ensure_sync_tables which tries to invoke migrate_memory.py
    # — known fail in unit-test isolation due to migration 002's BEGIN.
    monkeypatch.setattr(memory_core, "_ensure_sync_tables", lambda *a, **kw: None)
    monkeypatch.setenv("M3_OBSERVER_NO_EMBED", "1")

    obs = {
        "text": "User started yoga in March 2024.",
        "observation_date": "2024-04-15",
        "referenced_date": "2024-03-01",
        "relative_date": "March 2024",
        "confidence": 0.9,
        "supersedes_hint": None,
    }

    obs_id = asyncio.run(run_observer.write_observation(
        obs, target_variant="test-variant",
        user_id="alice", conversation_id="conv-1",
        source_turn_ids=["turn-1", "turn-2"],
    ))
    assert obs_id is not None

    conn = sqlite3.connect(str(bench_db))
    row = conn.execute(
        "SELECT type, content, valid_from, variant, user_id, metadata_json "
        "FROM memory_items WHERE id=?", (obs_id,)
    ).fetchone()
    assert row[0] == "observation"
    assert row[1] == "User started yoga in March 2024."
    assert row[2] == "2024-03-01"  # valid_from = referenced_date
    assert row[3] == "test-variant"
    assert row[4] == "alice"
    md = json.loads(row[5])
    assert md["observation_date"] == "2024-04-15"
    assert md["referenced_date"] == "2024-03-01"
    assert md["relative_date"] == "March 2024"
    assert md["confidence"] == 0.9
    assert md["conversation_id"] == "conv-1"
    assert md["source_turn_ids"] == ["turn-1", "turn-2"]
    conn.close()


def test_write_observation_falls_back_to_observation_date_when_referenced_null(bench_db, monkeypatch):
    """Timeless facts (referenced_date=null) should still set a valid_from."""
    import memory_core
    import run_observer
    monkeypatch.setattr(memory_core, "_ensure_sync_tables", lambda *a, **kw: None)
    monkeypatch.setenv("M3_OBSERVER_NO_EMBED", "1")

    obs = {
        "text": "User likes spicy food.",
        "observation_date": "2024-04-15",
        "referenced_date": None,
        "relative_date": None,
        "confidence": 0.95,
        "supersedes_hint": None,
    }
    obs_id = asyncio.run(run_observer.write_observation(
        obs, target_variant="test-variant",
        user_id="alice", conversation_id="conv-1",
        source_turn_ids=[],
    ))
    conn = sqlite3.connect(str(bench_db))
    vf = conn.execute("SELECT valid_from FROM memory_items WHERE id=?", (obs_id,)).fetchone()[0]
    # Should fall back to observation_date when referenced_date is None.
    assert vf == "2024-04-15"
    conn.close()
