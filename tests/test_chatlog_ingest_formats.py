"""Tests for bin/chatlog_ingest.py against REAL on-disk transcript schemas.

Fixtures under tests/fixtures/ mirror the shapes Claude Code and Gemini CLI
actually emit. If upstream changes either schema these tests surface the drift.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")

# Ensure bin/ on path
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "bin"))


# ─── Claude Code parser ───────────────────────────────────────────────────────

def test_parse_claude_code_real_schema():
    import chatlog_ingest
    with open(os.path.join(FIXTURES, "claude_code_sample.jsonl"), "r", encoding="utf-8") as f:
        raw = f.read()
    items, session_id = chatlog_ingest._parse_claude_code(raw)

    assert session_id == "fix-claude-0001"
    # Expect: 2 user + 2 assistant = 4. system / permission-mode lines skipped.
    assert len(items) == 4
    roles = [i["role"] for i in items]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert items[0]["content"] == "hello there"
    assert items[1]["content"] == "hi! how can i help?"
    # Assistant list-content with tool_use block: only text part survives.
    assert items[3]["content"] == "running."
    # Usage propagation.
    assert items[1]["tokens_in"] == 10
    assert items[1]["tokens_out"] == 7
    assert items[0]["tokens_in"] is None  # user rows have no usage
    # Model + provider.
    assert items[1]["model_id"] == "claude-opus-4-7"
    assert items[1]["provider"] == "anthropic"
    # conversation_id comes from sessionId.
    for item in items:
        assert item["conversation_id"] == "fix-claude-0001"


def test_parse_claude_code_skips_non_chat_types():
    import chatlog_ingest
    raw = (
        '{"type":"permission-mode","permissionMode":"default","sessionId":"x"}\n'
        '{"type":"file-history-snapshot","messageId":"x","snapshot":{}}\n'
        '{"type":"attachment","uuid":"a","sessionId":"x"}\n'
        '{"type":"system","subtype":"compact","sessionId":"x"}\n'
        '{"type":"user","message":{"role":"user","content":"kept"},"sessionId":"x","uuid":"u1"}\n'
    )
    items, session_id = chatlog_ingest._parse_claude_code(raw)
    assert session_id == "x"
    assert len(items) == 1
    assert items[0]["content"] == "kept"


def test_parse_claude_code_empty_and_malformed():
    import chatlog_ingest
    items, session_id = chatlog_ingest._parse_claude_code("")
    assert items == []
    assert session_id is None

    raw = '{"type":"user","message":{"role":"user","content":"ok"},"sessionId":"z","uuid":"u1"}\n{garbage\n'
    items, session_id = chatlog_ingest._parse_claude_code(raw)
    assert session_id == "z"
    assert len(items) == 1
    assert items[0]["content"] == "ok"


def test_parse_claude_code_skips_empty_content():
    """Tool-use-only assistant turns (no text block) produce empty content -> skip."""
    import chatlog_ingest
    raw = ('{"type":"assistant","message":{"role":"assistant","model":"claude-opus-4-7",'
           '"content":[{"type":"tool_use","id":"t","name":"X","input":{}}]},'
           '"uuid":"a1","sessionId":"s"}\n')
    items, _ = chatlog_ingest._parse_claude_code(raw)
    assert items == []


# ─── Gemini CLI parser ────────────────────────────────────────────────────────

def test_parse_gemini_cli_real_schema():
    import chatlog_ingest
    with open(os.path.join(FIXTURES, "gemini_session_sample.json"), "r", encoding="utf-8") as f:
        raw = f.read()
    items, session_id = chatlog_ingest._parse_gemini_cli(raw)

    assert session_id == "fix-gemini-0001"
    # Expect: 1 user + 2 gemini (assistant). info row skipped.
    assert len(items) == 3
    roles = [i["role"] for i in items]
    assert roles == ["user", "assistant", "assistant"]
    assert items[0]["content"] == "what is chat log status"
    assert items[1]["content"] == "I'll check the chat log status now."
    assert items[1]["model_id"] == "gemini-2.5-pro"
    assert items[1]["provider"] == "google"
    assert items[1]["tokens_in"] == 120
    assert items[1]["tokens_out"] == 18
    assert items[0]["tokens_in"] is None
    for item in items:
        assert item["conversation_id"] == "fix-gemini-0001"


def test_parse_gemini_cli_empty_and_malformed():
    import chatlog_ingest
    items, session_id = chatlog_ingest._parse_gemini_cli("")
    assert items == []
    assert session_id is None

    items, session_id = chatlog_ingest._parse_gemini_cli("not json at all")
    assert items == []


def test_parse_gemini_cli_handles_string_content_for_user():
    import chatlog_ingest
    raw = json.dumps({
        "sessionId": "s1",
        "messages": [
            {"id": "m1", "timestamp": "t", "type": "user", "content": "string form"},
            {"id": "m2", "timestamp": "t", "type": "gemini",
             "content": [{"text": "list "}, {"text": "form"}], "model": "gemini-2.5-pro"},
        ],
    })
    items, session_id = chatlog_ingest._parse_gemini_cli(raw)
    assert session_id == "s1"
    assert len(items) == 2
    assert items[0]["content"] == "string form"
    assert items[1]["content"] == "list form"


# ─── Provider inference ───────────────────────────────────────────────────────

@pytest.mark.parametrize("model,expected", [
    ("claude-opus-4-7", "anthropic"),
    ("claude-3-sonnet", "anthropic"),
    ("gemini-2.5-pro", "google"),
    ("palm-2", "google"),
    ("gpt-4o", "openai"),
    ("o1-preview", "openai"),
    ("o3-mini", "openai"),
    ("grok-4", "xai"),
    ("deepseek-chat", "deepseek"),
    ("llama-2-70b", "local"),
    ("mistral-7b", "local"),
    ("qwen-72b", "local"),
    ("mystery-model", "other"),
    ("", "other"),
])
def test_infer_provider(model, expected):
    import chatlog_ingest
    assert chatlog_ingest.infer_provider(model) == expected


# ─── End-to-end: --transcript-path ingest via _ingest() ───────────────────────

from conftest import isolate_chatlog_env, create_memory_items_schema


@pytest.fixture
def ingest_env(tmp_path, monkeypatch):
    """Route chatlog writes + cursor into tmp_path so tests are isolated."""
    paths = isolate_chatlog_env(monkeypatch, tmp_path)

    cursor_file = tmp_path / ".chatlog_ingest_cursor.json"
    import chatlog_ingest
    monkeypatch.setattr(chatlog_ingest, "_cursor_path", lambda: str(cursor_file))

    create_memory_items_schema(paths["db_path"])
    yield {"db": paths["db_path"], "cursor": cursor_file}


@pytest.mark.asyncio
async def test_ingest_claude_code_from_file_writes_real_rows(ingest_env):
    import chatlog_ingest, chatlog_core
    fixture = os.path.join(FIXTURES, "claude_code_sample.jsonl")

    result = await chatlog_ingest._ingest("claude-code", fixture,
                                          session_override="", variant="test")
    await chatlog_core._flush_once()

    assert result["failed"] == 0
    assert result["written"] == 4  # 4 chat rows from the fixture
    assert result["session_id"] == "fix-claude-0001"

    import sqlite3
    conn = sqlite3.connect(str(ingest_env["db"]))
    rows = conn.execute(
        "SELECT role_from_meta, variant, conversation_id FROM ("
        "  SELECT json_extract(metadata_json,'$.role') AS role_from_meta, "
        "         variant, conversation_id FROM memory_items"
        ") ORDER BY conversation_id"
    ).fetchall()
    assert len(rows) == 4
    for role, variant, conv in rows:
        assert conv == "fix-claude-0001"
        assert variant == "test"
        assert role in ("user", "assistant")


@pytest.mark.asyncio
async def test_ingest_gemini_from_file_writes_real_rows(ingest_env):
    import chatlog_ingest, chatlog_core
    fixture = os.path.join(FIXTURES, "gemini_session_sample.json")

    result = await chatlog_ingest._ingest("gemini-cli", fixture,
                                          session_override="", variant="test")
    await chatlog_core._flush_once()

    assert result["failed"] == 0
    assert result["written"] == 3
    assert result["session_id"] == "fix-gemini-0001"


@pytest.mark.asyncio
async def test_ingest_is_idempotent_via_cursor(ingest_env):
    """Second invocation on the same transcript skips already-seen uuids."""
    import chatlog_ingest, chatlog_core
    fixture = os.path.join(FIXTURES, "claude_code_sample.jsonl")

    first = await chatlog_ingest._ingest("claude-code", fixture, "", "test")
    await chatlog_core._flush_once()
    assert first["written"] == 4

    second = await chatlog_ingest._ingest("claude-code", fixture, "", "test")
    await chatlog_core._flush_once()
    assert second["written"] == 0
    assert second["skipped"] == 4


@pytest.mark.asyncio
async def test_ingest_missing_transcript_path_is_soft_failure(ingest_env):
    import chatlog_ingest
    result = await chatlog_ingest._ingest("claude-code", r"C:\does\not\exist.jsonl",
                                          "", None)
    assert result["written"] == 0
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_ingest_session_override_mismatch_prefers_transcript(ingest_env, caplog):
    """If the envelope --session-id disagrees with the transcript's own
    sessionId, log a warning and let the transcript's value win on both the
    rows and the cursor — one identifier across the pipeline."""
    import logging
    import chatlog_ingest, chatlog_core
    fixture = os.path.join(FIXTURES, "claude_code_sample.jsonl")

    with caplog.at_level(logging.WARNING, logger="chatlog_ingest"):
        result = await chatlog_ingest._ingest(
            "claude-code", fixture,
            session_override="envelope-said-something-else",
            variant="test",
        )
    await chatlog_core._flush_once()

    # Transcript's sessionId wins for result and for rows.
    assert result["session_id"] == "fix-claude-0001"

    import sqlite3
    conn = sqlite3.connect(str(ingest_env["db"]))
    conv_ids = {row[0] for row in conn.execute("SELECT conversation_id FROM memory_items")}
    assert conv_ids == {"fix-claude-0001"}

    # And a warning was emitted so operators can spot the divergence.
    assert any("session_id mismatch" in r.message for r in caplog.records)
