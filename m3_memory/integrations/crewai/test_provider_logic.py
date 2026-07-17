"""Hermetic logic tests for the CrewAI integration — NO live m3, NO live CrewAI.

Exercises the pure mapping/scope/tenancy logic that has no I/O: the embed_model
identity derivation, scope-path normalization + prefix matching, record↔row
round-trip, and the module-level helpers (written-id parse, immediate-child,
delete-match). The live round-trip (dual-embed, cross-agent search, isolation) is
a separate live test (needs a real DB + optionally crewai) — DESIGN_PHILOSOPHIES
§3: a test that passes only because a live service is reachable is NOT hermetic.

Uses a tiny fake ``MemoryRecord`` (a namespace) so nothing imports crewai. Run:
``python m3_memory/integrations/crewai/test_provider_logic.py``
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

# Allow running as a bare script: repo root is four levels up.
_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _c(cond: bool, msg: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        _c.failed = True  # type: ignore[attr-defined]


_c.failed = False  # type: ignore[attr-defined]


def _fake_record(**kw):
    """A stand-in for crewai.memory.types.MemoryRecord (duck-typed)."""
    base = dict(
        id="", content="", scope="/", categories=[], metadata={},
        importance=0.5, created_at=None, last_accessed=None, embedding=None,
        source=None, private=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_embed_model_identity() -> None:
    from m3_memory.integrations.crewai import mapping

    print("mapping.crewai_embed_model — per-dim identity tag")
    _c(mapping.crewai_embed_model(3072) == "crewai-3072", "3072 -> crewai-3072")
    _c(mapping.crewai_embed_model(768) == "crewai-768", "768 -> crewai-768")
    _c(mapping.crewai_embed_model(3072) != mapping.crewai_embed_model(768),
       "different dims -> different identities (no cross-embedder collision)")
    try:
        mapping.crewai_embed_model(0)
        _c(False, "dim 0 should raise")
    except ValueError:
        _c(True, "dim < 1 raises (fail loud)")


def test_scope_normalization_and_matching() -> None:
    from m3_memory.integrations.crewai import mapping

    print("mapping.normalize_scope_prefix")
    _c(mapping.normalize_scope_prefix(None) == "", "None -> '' (match-all)")
    _c(mapping.normalize_scope_prefix("/") == "", "'/' -> '' (match-all)")
    _c(mapping.normalize_scope_prefix("crew/research") == "/crew/research",
       "adds leading slash")
    _c(mapping.normalize_scope_prefix("/crew/research/") == "/crew/research",
       "strips trailing slash")

    print("mapping.scope_matches — prefix/descendant, segment-boundary safe")
    _c(mapping.scope_matches("/crew/research", "/crew/research"), "exact match")
    _c(mapping.scope_matches("/crew/research/facts", "/crew/research"),
       "descendant matches")
    _c(mapping.scope_matches("/crew/research", ""), "empty prefix matches all")
    _c(not mapping.scope_matches("/crew/research2", "/crew/research"),
       "segment boundary: /research does NOT match /research2 (the LIKE trap)")
    _c(not mapping.scope_matches("/crew/other", "/crew/research"),
       "sibling does not match")


def test_record_to_write_args() -> None:
    from m3_memory.integrations.crewai import mapping

    print("mapping.record_to_write_args — tenancy stamp + scope/categories in md")
    rec = _fake_record(
        content="the sky is blue", scope="/crew/facts",
        categories=["observation"], importance=0.8, private=True,
        source="agent-x", metadata={"k": "v"},
    )
    args = mapping.record_to_write_args(rec, user_id="crew-alpha", scope="user")
    _c(args["content"] == "the sky is blue", "content carried")
    _c(args["user_id"] == "crew-alpha", "user_id (tenancy) stamped")
    _c(args["scope"] == "user", "m3 scope category stamped")
    _c(args["importance"] == 0.8, "importance carried")
    _c(args["source"] == "crewai", "source tagged crewai")
    md = args["metadata"]
    _c(md[mapping.SCOPE_PATH_KEY] == "/crew/facts", "crewai scope path in metadata")
    _c(md[mapping.CATEGORIES_KEY] == ["observation"], "categories in metadata")
    _c(md[mapping.PRIVATE_KEY] is True, "private flag in metadata")
    _c(md[mapping.CREWAI_SOURCE_KEY] == "agent-x", "crewai source in metadata")
    _c(md["k"] == "v", "user metadata preserved")


def test_item_to_record_roundtrip() -> None:
    from m3_memory.integrations.crewai import mapping

    print("mapping round-trip: record -> write_args.metadata -> item -> record")
    import json
    rec = _fake_record(
        id="m1", content="body", scope="/crew/facts",
        categories=["obs"], importance=0.7, private=True, source="a1",
        metadata={"tag": "z"},
    )
    args = mapping.record_to_write_args(rec, user_id="u", scope="user")
    # Simulate the m3 item row that search/list would return.
    item = {
        "id": "m1", "content": "body", "importance": 0.7,
        "created_at": "2026-07-17T00:00:00Z",
        "metadata_json": json.dumps(args["metadata"]),
    }
    back = mapping.item_to_record(item, record_cls=_fake_record)
    _c(back.id == "m1" and back.content == "body", "id + content restored")
    _c(back.scope == "/crew/facts", "scope path restored")
    _c(back.categories == ["obs"], "categories restored")
    _c(back.private is True, "private restored")
    _c(back.source == "a1", "source restored")
    _c(back.metadata.get("tag") == "z", "user metadata restored")
    _c(mapping.SCOPE_PATH_KEY not in back.metadata,
       "reserved keys stripped from user-facing metadata")
    _c(abs(back.importance - 0.7) < 1e-9, "importance restored")


def test_helpers() -> None:
    from m3_memory.integrations.crewai import backend as b

    print("backend._parse_written_id")
    _c(b._parse_written_id("Created: 11111111-2222-3333-4444-555555555555 (embedding deferred)")
       == "11111111-2222-3333-4444-555555555555", "extracts uuid from Created: + suffix")
    _c(b._parse_written_id("Error: nope") is None, "Error -> None")
    _c(b._parse_written_id("") is None, "empty -> None")

    print("backend._immediate_child")
    _c(b._immediate_child("/crew", "/crew/research/facts") == "/crew/research",
       "immediate child on the path")
    _c(b._immediate_child("/crew", "/crew") is None, "node itself -> None")
    _c(b._immediate_child("", "/research/x") == "/research",
       "root prefix immediate child")

    print("backend._deleted_count")
    _c(b._deleted_count({"deleted": 3}, fallback=9) == 3, "reads deleted key")
    _c(b._deleted_count("weird", fallback=9) == 9, "falls back to id count")

    print("backend._match_for_delete — older_than")
    from datetime import datetime, timezone
    old_item = {"metadata_json": "{}", "created_at": "2020-01-01T00:00:00Z"}
    new_item = {"metadata_json": "{}", "created_at": "2030-01-01T00:00:00Z"}
    cutoff = datetime(2025, 1, 1, tzinfo=timezone.utc)
    _c(b._match_for_delete(old_item, None, None, cutoff),
       "record older than cutoff matches")
    _c(not b._match_for_delete(new_item, None, None, cutoff),
       "record newer than cutoff does not match")


def test_backend_requires_tenant() -> None:
    print("M3StorageBackend construction — tenancy enforced (§7)")
    # Import the CLASS without triggering the crewai version guard: the class body
    # itself has no crewai import (only method bodies do, lazily). We import the
    # module directly to bypass the package __getattr__'s version check.
    from m3_memory.integrations.crewai.backend import M3StorageBackend
    try:
        M3StorageBackend(user_id="")
        _c(False, "empty user_id should raise")
    except ValueError:
        _c(True, "empty user_id raises (no anonymous/global mode)")
    try:
        M3StorageBackend(user_id="   ")
        _c(False, "whitespace user_id should raise")
    except ValueError:
        _c(True, "whitespace user_id raises")


def main() -> int:
    print("\n=== CrewAI adapter hermetic logic tests ===\n")
    test_embed_model_identity()
    test_scope_normalization_and_matching()
    test_record_to_write_args()
    test_item_to_record_roundtrip()
    test_helpers()
    test_backend_requires_tenant()
    print()
    if _c.failed:  # type: ignore[attr-defined]
        print("RESULT: FAILED")
        return 1
    print("RESULT: ALL PASSED")
    return 0


# pytest entry points
def test_mapping_and_logic():
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
