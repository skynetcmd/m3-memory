"""Hermetic logic tests for the PydanticAI adapter — no pydantic-ai, no live m3.

These exercise the framework-independent seams (mapping + M3Deps operation
routing with a stubbed client) so they run in the normal suite on any interpreter,
even where pydantic-ai isn't installed. The live isinstance/round-trip checks
against real pydantic-ai live in tests/test_pydantic_ai_*.py (gated on the import).
"""

from __future__ import annotations

import os
import sys

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
for _p in (_REPO, os.path.join(_REPO, "bin")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from m3_memory.integrations.pydantic_ai import mapping  # noqa: E402
from m3_memory.integrations.pydantic_ai.deps import (  # noqa: E402
    M3Deps,
    _deleted_any,
    _parse_written_id,
)


# ── mapping ──────────────────────────────────────────────────────────────────

def test_recall_hit_to_dict_surfaces_only_safe_fields():
    item = {
        "id": "m1", "content": "likes dark roast", "type": "preference",
        "importance": 0.7, "created_at": "2026-01-01T00:00:00Z",
        "embedding": b"\x00\x01",  # internal — must NOT surface
        "content_hash": "deadbeef",  # internal — must NOT surface
        "metadata_json": '{"role": "user"}',
    }
    d = mapping.recall_hit_to_dict(0.912345, item)
    assert d["score"] == 0.9123
    assert d["content"] == "likes dark roast"
    assert d["type"] == "preference"
    assert d["metadata"] == {"role": "user"}
    assert "embedding" not in d and "content_hash" not in d


def test_recalled_block_empty_when_no_hits():
    assert mapping.recalled_memories_block([]) == ""


def test_recalled_block_formats_hits():
    rows = [(0.9, {"content": "a"}), (0.5, {"content": "b"})]
    block = mapping.recalled_memories_block(rows, header="Memories")
    assert block.startswith("Memories:")
    assert "- a" in block and "- b" in block


def test_recalled_block_skips_when_all_blank():
    # hits with no content produce no bullet lines -> empty (no bare header).
    assert mapping.recalled_memories_block([(0.9, {"id": "x"})]) == ""


# ── deps parse helpers ───────────────────────────────────────────────────────

def test_parse_written_id():
    assert _parse_written_id("Created: 123e4567-e89b-12d3-a456-426614174000") \
        == "123e4567-e89b-12d3-a456-426614174000"
    assert _parse_written_id("Created: abc (superseded def)") == "abc"
    assert _parse_written_id("Error: nope") == ""
    assert _parse_written_id(None) == ""


def test_deleted_any():
    assert _deleted_any({"deleted": 2}) is True
    assert _deleted_any({"deleted": 0}) is False
    assert _deleted_any(3) is True
    assert _deleted_any("Error: not found") is False
    assert _deleted_any("Deleted 1 memory") is True


# ── tenancy (§7) + operation routing with a stub client ──────────────────────

class _StubClient:
    def __init__(self):
        self.calls = []

    def _tool(self, name, **args):
        self.calls.append((name, args))
        if name == "memory_write":
            return "Created: 11111111-1111-1111-1111-111111111111"
        if name == "memory_search_scored":
            return [(0.8, {"id": "m1", "content": "found"})]
        if name == "memory_delete_bulk":
            return {"deleted": 1}
        return ""


def _deps_with_stub():
    d = M3Deps(user_id="alice")
    d._client = _StubClient()
    return d


def test_m3deps_requires_user_id():
    import pytest
    with pytest.raises(ValueError):
        M3Deps(user_id="")
    with pytest.raises(ValueError):
        M3Deps(user_id="   ")


def test_remember_stamps_tenant_and_scope():
    d = _deps_with_stub()
    new_id = d.remember("likes tea", importance=0.6)
    assert new_id == "11111111-1111-1111-1111-111111111111"
    name, args = d._client.calls[0]
    assert name == "memory_write"
    assert args["user_id"] == "alice"
    assert args["scope"] == "agent"
    assert args["importance"] == 0.6
    assert args["auto_classify"] is True  # type defaulted to 'auto'


def test_recall_stamps_tenant():
    d = _deps_with_stub()
    rows = d.recall("what do I like", k=3)
    assert rows == [(0.8, {"id": "m1", "content": "found"})]
    name, args = d._client.calls[0]
    assert name == "memory_search_scored"
    assert args["user_id"] == "alice"
    assert args["k"] == 3


def test_forget_routes_to_delete_bulk():
    d = _deps_with_stub()
    assert d.forget("m1") is True
    name, args = d._client.calls[0]
    assert name == "memory_delete_bulk"
    assert args["ids"] == ["m1"]


def test_empty_inputs_are_noops():
    d = _deps_with_stub()
    assert d.remember("") == ""
    assert d.recall("") == []
    assert d.forget("") is False
    assert d._client.calls == []  # nothing dispatched
