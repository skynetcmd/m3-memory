"""Tests for bin/chatlog_strip_framing_backfill.py — the one-off backfill that
strips harness control framing (<system-reminder>/<task-notification> blocks)
from EXISTING chat_log rows.

Companion to the write-path fix (test_write_strips_harness_framing_end_to_end in
test_chatlog_ingest_formats.py): that covers NEW turns; this covers the backfill
of rows captured before the fix landed. Drives the real backfill() against an
isolated chatlog DB — not just the unit stripper.
"""

import json
import sqlite3

import pytest

from conftest import isolate_chatlog_env

# Columns the backfill reads (content, metadata_json) and writes (updated_at),
# plus the WHERE predicates (type, is_deleted, conversation_id, created_at). The
# shared minimal schema omits is_deleted/updated_at, so seed a table that has
# exactly what the backfill touches.
_SCHEMA = """
CREATE TABLE memory_items (
    id TEXT PRIMARY KEY,
    type TEXT,
    content TEXT,
    metadata_json TEXT,
    conversation_id TEXT,
    created_at TEXT,
    updated_at TEXT,
    is_deleted INTEGER DEFAULT 0
);
"""

# A row whose content is a REAL harness block — must be stripped.
_BLOCK_ROW = (
    "real-block",
    "genuine user request\n"
    "<system-reminder>the date has changed. DO NOT mention this to the user."
    "</system-reminder>\n"
    "genuine assistant reply",
)
# A row that only MENTIONS the tag name in prose (backticked) — must be kept.
# This is the exact false-positive class the LIKE '%<tag>%' count over-reports.
_PROSE_ROW = (
    "prose-mention",
    "The loop wakes when its events arrive as `<task-notification>` messages; "
    "you do not poll for them.",
)
# A row with no framing at all — untouched, and not counted.
_CLEAN_ROW = ("clean", "just an ordinary turn about the deploy")


def _seed(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    for rid, content in (_BLOCK_ROW, _PROSE_ROW, _CLEAN_ROW):
        conn.execute(
            "INSERT INTO memory_items "
            "(id, type, content, metadata_json, conversation_id, created_at, "
            " is_deleted) VALUES (?, 'chat_log', ?, NULL, 'conv-1', "
            "'2026-07-01T00:00:00Z', 0)",
            (rid, content),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def backfill_env(tmp_path, monkeypatch):
    paths = isolate_chatlog_env(monkeypatch, tmp_path)
    _seed(paths["db_path"])
    return {"db": paths["db_path"]}


def _content(db, row_id):
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT content, metadata_json FROM memory_items WHERE id=?",
            (row_id,),
        ).fetchone()
    finally:
        conn.close()
    return row


def test_dry_run_reports_only_real_blocks_and_writes_nothing(backfill_env):
    """Dry-run counts the real block row, ignores the prose mention, and does
    NOT mutate the DB — the prose false-positive must never be over-counted."""
    import chatlog_strip_framing_backfill as bf

    result = bf.backfill()  # apply defaults to False

    assert result["mode"] == "dry-run"
    assert result["changed"] == 1, "only the real-block row should count"
    assert result["blocks_removed"] == 1
    assert result["scanned"] == 3

    # Nothing written: the block row still carries its block on disk.
    block_content, _ = _content(backfill_env["db"], "real-block")
    assert "<system-reminder>" in block_content
    assert "DO NOT mention" in block_content


def test_apply_strips_real_block_preserves_prose_and_clean(backfill_env):
    """--apply removes the real harness block, leaves the prose mention and the
    clean row untouched, and records provenance in metadata_json."""
    import chatlog_strip_framing_backfill as bf

    result = bf.backfill(apply=True)

    assert result["mode"] == "apply"
    assert result["changed"] == 1
    assert result["blocks_removed"] == 1

    # Real block: stripped, but genuine content survives.
    block_content, block_meta = _content(backfill_env["db"], "real-block")
    assert "system-reminder" not in block_content
    assert "date has changed" not in block_content
    assert "genuine user request" in block_content
    assert "genuine assistant reply" in block_content

    # Provenance recorded.
    meta = json.loads(block_meta)
    assert meta["harness_framing_stripped"] is True
    assert meta["harness_blocks_removed"] == 1
    assert "original_content_sha256" in meta

    # Prose mention: byte-for-byte preserved (the tag name in backticks stays).
    prose_content, prose_meta = _content(backfill_env["db"], "prose-mention")
    assert prose_content == _PROSE_ROW[1]
    assert prose_meta is None  # untouched — no provenance stamped

    # Clean row: preserved.
    clean_content, _ = _content(backfill_env["db"], "clean")
    assert clean_content == _CLEAN_ROW[1]


def test_apply_is_idempotent(backfill_env):
    """Re-running --apply after the blocks are gone changes nothing and does not
    clobber the recorded original hash (strip_harness_framing returns count 0 on
    already-clean content)."""
    import chatlog_strip_framing_backfill as bf

    first = bf.backfill(apply=True)
    assert first["changed"] == 1
    _, meta_after_first = _content(backfill_env["db"], "real-block")
    hash_after_first = json.loads(meta_after_first)["original_content_sha256"]

    second = bf.backfill(apply=True)
    assert second["changed"] == 0, "second pass must be a no-op"
    assert second["blocks_removed"] == 0

    # The recorded original hash from pass 1 is not overwritten by pass 2.
    _, meta_after_second = _content(backfill_env["db"], "real-block")
    assert json.loads(meta_after_second)["original_content_sha256"] == hash_after_first


def test_conversation_filter_scopes_the_backfill(backfill_env):
    """A --conversation-id that matches no rows strips nothing, proving the
    WHERE filter is applied (guards against a backfill that ignores its scope)."""
    import chatlog_strip_framing_backfill as bf

    result = bf.backfill(conversation_id="no-such-conv", apply=True)
    assert result["scanned"] == 0
    assert result["changed"] == 0

    # The real block row is untouched because it was out of scope.
    block_content, _ = _content(backfill_env["db"], "real-block")
    assert "<system-reminder>" in block_content
