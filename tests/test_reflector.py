"""Tests for bin/run_reflector.py — parse_reflector_output + supersede edge writes.

Network mocked via httpx.MockTransport.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "bin"))


@pytest.fixture
def bench_db(tmp_path, monkeypatch):
    """Minimum-schema test DB. Bypasses migrate_memory.py because
    002_enforce_relationships.sql has a known BEGIN/SAVEPOINT conflict;
    the Reflector unit tests only need memory_items + memory_relationships."""
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
        CREATE TABLE reflector_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            user_id TEXT,
            obs_count_at_enqueue INTEGER,
            enqueued_at TEXT,
            attempts INTEGER DEFAULT 0,
            last_error TEXT,
            last_attempt_at TEXT
        );
    """)
    conn.commit()
    conn.close()
    return db_path


def test_parse_reflector_output_drops_noop_supersedes():
    import run_reflector
    text = json.dumps({
        "observations": [
            {"text": "User lives in Seattle.", "observation_date": "2023-01-15"},
        ],
        "supersedes": [
            {"new_text": "Same text.", "old_text": "Same text."},  # no-op merge
            {"new_text": "User moved to Austin.", "old_text": "User lives in Seattle."},
            {"new_text": "", "old_text": "x"},  # empty new
            {"new_text": "x", "old_text": ""},  # empty old
        ]
    })
    obs, sup = run_reflector.parse_reflector_output(text)
    # No-op + empties dropped; one real supersede pair remains.
    assert len(sup) == 1
    assert sup[0]["new_text"] == "User moved to Austin."
    assert sup[0]["old_text"] == "User lives in Seattle."


def test_parse_reflector_output_handles_missing_keys():
    import run_reflector
    obs, sup = run_reflector.parse_reflector_output('{"observations": []}')
    assert obs == []
    assert sup == []


def test_parse_reflector_output_strips_code_fences():
    import run_reflector
    text = '```json\n{"observations": [], "supersedes": []}\n```'
    obs, sup = run_reflector.parse_reflector_output(text)
    assert obs == []
    assert sup == []


def test_parse_reflector_output_malformed_returns_empty():
    import run_reflector
    obs, sup = run_reflector.parse_reflector_output("not json")
    assert obs == []
    assert sup == []


def test_call_reflector_anthropic_shape():
    import run_reflector

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "observations": [{"text": "x", "observation_date": "2023-01-01"}],
                    "supersedes": [
                        {"new_text": "User moved to Austin.",
                         "old_text": "User lives in Seattle."}
                    ],
                })
            }],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        })

    transport = httpx.MockTransport(handler)

    class _Profile:
        backend = "anthropic"
        url = "http://mock.test/v1/messages"
        model = "test-model"
        system = "reflect facts"
        max_tokens = 8192
        input_max_chars = 40000
        temperature = 0
        timeout_s = 30.0
        anthropic_version = "2023-06-01"

    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            return await run_reflector.call_reflector(
                existing=[{"text": "User lives in Seattle."}],
                new=[{"text": "User moved to Austin."}],
                profile=_Profile(),
                client=client,
                token="test-token",
            )

    obs, sup = asyncio.run(go())
    # Verify the input shape was correct
    body = captured["json"]
    assert "messages" in body
    user_content = json.loads(body["messages"][0]["content"])
    assert "existing" in user_content
    assert "new" in user_content
    # Verify parsing
    assert len(sup) == 1
    assert sup[0]["new_text"] == "User moved to Austin."


def test_find_observation_id_by_text_exact():
    import run_reflector
    rows = [
        ("id-1", {"text": "User lives in Seattle."}),
        ("id-2", {"text": "User moved to Austin."}),
    ]
    assert run_reflector._find_observation_id_by_text(rows, "User lives in Seattle.") == "id-1"
    assert run_reflector._find_observation_id_by_text(rows, "User moved to Austin.") == "id-2"


def test_find_observation_id_by_text_prefix_fallback():
    """When the Reflector slightly rephrases, prefix match should work."""
    import run_reflector
    rows = [
        ("id-1", {"text": "User has been living in Seattle for 5 years."}),
    ]
    # Reflector emits a truncated version
    out = run_reflector._find_observation_id_by_text(rows, "User has been living in Seattle")
    assert out == "id-1"


def test_find_observation_id_by_text_no_match():
    import run_reflector
    rows = [("id-1", {"text": "Completely unrelated."})]
    assert run_reflector._find_observation_id_by_text(rows, "User moved to Austin.") is None


def test_load_observations_for_conv_groups_correctly(bench_db):
    """Verify _load_observations_for_conv pulls only the requested
    (user_id, conversation_id) pair's observations."""
    import run_reflector

    # Seed two observations under conv-1 and one under conv-2 for user 'alice',
    # plus one under conv-1 for user 'bob' (must be excluded).
    conn = sqlite3.connect(str(bench_db))
    seeds = [
        ("o-1", "alice", "conv-1", "fact A", "2023-01-01"),
        ("o-2", "alice", "conv-1", "fact B", "2023-01-02"),
        ("o-3", "alice", "conv-2", "fact C", "2023-01-01"),
        ("o-4", "bob",   "conv-1", "fact D", "2023-01-01"),
    ]
    for oid, uid, cid, text, vf in seeds:
        meta = json.dumps({
            "observation_date": vf, "referenced_date": vf,
            "conversation_id": cid, "confidence": 0.95,
        })
        conn.execute(
            "INSERT INTO memory_items (id, type, content, user_id, valid_from, "
            "created_at, metadata_json) VALUES (?, 'observation', ?, ?, ?, ?, ?)",
            (oid, text, uid, vf, vf, meta),
        )
    conn.commit()
    conn.close()

    rows = run_reflector._load_observations_for_conv(
        user_id="alice", conversation_id="conv-1"
    )
    assert len(rows) == 2  # alice/conv-1 only
    texts = sorted(r[1]["text"] for r in rows)
    assert texts == ["fact A", "fact B"]
