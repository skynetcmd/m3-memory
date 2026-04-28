"""Tests for bin/m3_enrich.py — argparse, profile resolution, type-allowlist,
DB resolution, dry-run, _ensure_migration_025 idempotence.

Network is mocked or skipped where possible. Tests that need an actual
DB schema use a minimum-schema fixture similar to test_observer.py.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "bin"))


@pytest.fixture
def stub_db(tmp_path, monkeypatch):
    """Minimum-schema test DB. Mirrors test_observer.py — bypasses
    migrate_memory.py since the chain has a known migration-002 issue."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE memory_items (
            id TEXT PRIMARY KEY, type TEXT, title TEXT, content TEXT,
            metadata_json TEXT, conversation_id TEXT, user_id TEXT,
            valid_from TEXT, created_at TEXT,
            is_deleted INTEGER DEFAULT 0,
            variant TEXT
        );
        CREATE TABLE memory_relationships (
            id TEXT PRIMARY KEY, from_id TEXT, to_id TEXT,
            relationship_type TEXT, created_at TEXT
        );
        CREATE TABLE memory_embeddings (
            id TEXT PRIMARY KEY, memory_id TEXT, embedding BLOB,
            embed_model TEXT, dim INTEGER, created_at TEXT
        );
        CREATE TABLE chroma_sync_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT,
            operation TEXT, enqueued_at TEXT
        );
    """)
    conn.commit()
    conn.close()
    return db_path


def test_resolve_db_explicit_path_wins(tmp_path, monkeypatch):
    import m3_enrich
    target = tmp_path / "explicit.db"
    target.write_text("placeholder", encoding="utf-8")
    monkeypatch.setenv("M3_DATABASE", "/fake/missing/path.db")
    out = m3_enrich._resolve_db(str(target), "M3_DATABASE", "agent_memory.db")
    assert out == target.resolve()


def test_resolve_db_env_fallback(tmp_path, monkeypatch):
    import m3_enrich
    target = tmp_path / "from_env.db"
    target.write_text("x", encoding="utf-8")
    monkeypatch.setenv("M3_DATABASE", str(target))
    out = m3_enrich._resolve_db(None, "M3_DATABASE", "agent_memory.db")
    assert out == target.resolve()


def test_resolve_db_returns_none_when_missing(tmp_path, monkeypatch):
    import m3_enrich
    monkeypatch.delenv("M3_DATABASE", raising=False)
    monkeypatch.delenv("M3_CHATLOG_DATABASE", raising=False)
    out = m3_enrich._resolve_db("/no/such/path.db", "M3_DATABASE", "agent_memory.db")
    assert out is None


def test_build_type_allowlist_default():
    import m3_enrich
    import argparse
    args = argparse.Namespace(
        include_summaries=False, include_notes=False, include_types=None,
    )
    out = m3_enrich._build_type_allowlist(args)
    assert out == ("message", "conversation", "chat_log")


def test_build_type_allowlist_with_summaries_and_notes():
    import m3_enrich
    import argparse
    args = argparse.Namespace(
        include_summaries=True, include_notes=True, include_types=None,
    )
    out = m3_enrich._build_type_allowlist(args)
    assert "summary" in out
    assert "note" in out
    assert "message" in out


def test_build_type_allowlist_extra_types():
    import m3_enrich
    import argparse
    args = argparse.Namespace(
        include_summaries=False, include_notes=False,
        include_types="decision,plan,fact",
    )
    out = m3_enrich._build_type_allowlist(args)
    assert "decision" in out
    assert "plan" in out
    assert "fact" in out


def test_build_type_allowlist_skips_observation_in_extra_types():
    """ALWAYS_SKIP_TYPES (observation) cannot be re-added via --include-types."""
    import m3_enrich
    import argparse
    args = argparse.Namespace(
        include_summaries=False, include_notes=False,
        include_types="observation,decision",
    )
    out = m3_enrich._build_type_allowlist(args)
    assert "observation" not in out
    assert "decision" in out


def test_load_profile_with_path_resolves_explicit_yaml(tmp_path, monkeypatch):
    """--profile-path beats --profile."""
    import m3_enrich
    yaml_path = tmp_path / "custom_profile.yaml"
    yaml_path.write_text("""\
url: http://localhost:0/v1
model: test-model
api_key_service: TEST_TOKEN
backend: openai
labels: [observed]
fallback: observed
temperature: 0
timeout_s: 1.0
max_tokens: 512
system: |
  test system prompt
""", encoding="utf-8")
    out = m3_enrich._load_profile_with_path(name="enrich_local_qwen", path=str(yaml_path))
    assert out.name == "custom_profile"
    assert out.model == "test-model"


def test_load_profile_with_path_aborts_on_missing(tmp_path):
    import m3_enrich
    with pytest.raises(SystemExit) as exc:
        m3_enrich._load_profile_with_path(name=None, path="/no/such/profile.yaml")
    # sys.exit("...") puts the message in exc.value.code (a string)
    assert "profile path not found" in str(exc.value.code)


def test_load_profile_falls_back_to_named_profile(monkeypatch):
    """When --profile-path is None, _load_profile_with_path uses --profile."""
    import m3_enrich
    out = m3_enrich._load_profile_with_path(name="enrich_local_qwen", path=None)
    assert out.name == "enrich_local_qwen"
    assert out.model == "qwen/qwen3-8b"


def test_load_profile_aborts_on_unknown_name():
    import m3_enrich
    with pytest.raises(SystemExit) as exc:
        m3_enrich._load_profile_with_path(name="enrich_does_not_exist", path=None)
    assert "not found" in str(exc.value.code).lower()


def test_ensure_migration_025_creates_queues(stub_db, monkeypatch):
    """_ensure_migration_025 should create observation_queue + reflector_queue
    on a DB that lacks them. Idempotent on second call."""
    import m3_enrich
    m3_enrich._ensure_migration_025(stub_db)
    conn = sqlite3.connect(str(stub_db))
    tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "observation_queue" in tables
    assert "reflector_queue" in tables
    # Run again — should be a no-op (idempotent).
    m3_enrich._ensure_migration_025(stub_db)
    conn.close()


def test_ensure_migration_025_creates_chroma_sync_queue_if_missing(tmp_path, monkeypatch):
    """Chatlog DBs lack chroma_sync_queue by default; the helper must
    add it lazily so memory_write_impl(embed=True) doesn't crash."""
    import m3_enrich
    db_path = tmp_path / "no_chroma.db"
    conn = sqlite3.connect(str(db_path))
    # Minimum schema EXCEPT chroma_sync_queue. memory_items needs the
    # columns referenced by migration 025's partial index
    # (type, user_id, valid_from).
    conn.executescript("""
        CREATE TABLE memory_items (
            id TEXT PRIMARY KEY, type TEXT, content TEXT,
            user_id TEXT, valid_from TEXT,
            is_deleted INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()
    m3_enrich._ensure_migration_025(db_path)
    conn = sqlite3.connect(str(db_path))
    tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "chroma_sync_queue" in tables
    assert "observation_queue" in tables
    conn.close()


def test_query_eligible_groups_groups_by_conversation_id(stub_db):
    """Verify that rows with the same conversation_id collapse into one
    group, and that --limit caps at the conversation level."""
    import m3_enrich
    conn = sqlite3.connect(str(stub_db))
    rows = [
        # conv-1 → 3 turns
        ("t1", "message", None, "User went to Paris.",
         '{"turn_index":0}', "conv-1", "alice", "2024-01-01T00:00:00Z"),
        ("t2", "message", None, "Trip was great.",
         '{"turn_index":1}', "conv-1", "alice", "2024-01-01T00:00:01Z"),
        ("t3", "message", None, "Going back next year.",
         '{"turn_index":2}', "conv-1", "alice", "2024-01-01T00:00:02Z"),
        # conv-2 → 1 turn
        ("t4", "message", None, "User loves spicy food.",
         '{"turn_index":0}', "conv-2", "alice", "2024-01-02T00:00:00Z"),
        # conv-3 → 1 turn (will be filtered: type='note' not in default allowlist)
        ("t5", "note", None, "User note.",
         '{}', "conv-3", "alice", "2024-01-03T00:00:00Z"),
    ]
    for row in rows:
        conn.execute(
            "INSERT INTO memory_items (id, type, title, content, metadata_json, "
            "conversation_id, user_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
            row,
        )
    conn.commit()
    conn.close()

    groups = m3_enrich._query_eligible_groups(
        stub_db, type_allowlist=("message", "conversation", "chat_log"), limit=None,
    )
    # Should get 2 conversation groups (note row excluded by type allowlist)
    assert len(groups) == 2
    # Sorted by group size descending — conv-1 (3 turns) first
    assert len(groups[0][2]) == 3
    assert len(groups[1][2]) == 1


def test_query_eligible_groups_respects_limit(stub_db):
    """--limit caps groups, not rows."""
    import m3_enrich
    conn = sqlite3.connect(str(stub_db))
    for i in range(5):
        conn.execute(
            "INSERT INTO memory_items (id, type, content, conversation_id, user_id, "
            "metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"row-{i}", "message", f"content {i}", f"conv-{i}", "alice",
             '{"turn_index":0}', "2024-01-01T00:00:00Z"),
        )
    conn.commit()
    conn.close()
    groups = m3_enrich._query_eligible_groups(
        stub_db, type_allowlist=("message",), limit=3,
    )
    assert len(groups) == 3


def test_estimate_cost_wall_known_models():
    """Cost calculator returns numeric estimates for known cloud models,
    free-local for unknown."""
    import m3_enrich
    from slm_intent import Profile

    haiku = Profile(
        name="t", url="x", model="claude-haiku-4-5", system="x",
        labels=("o",), fallback="o", temperature=0, timeout_s=1.0,
        api_key_service="x", backend="anthropic",
    )
    cost, wall = m3_enrich._estimate_cost_wall(haiku, n_groups=1000)
    assert "$" in cost
    assert "min" in wall

    qwen = Profile(
        name="t", url="x", model="qwen/qwen3-8b", system="x",
        labels=("o",), fallback="o", temperature=0, timeout_s=1.0,
        api_key_service="x", backend="anthropic",
    )
    cost, wall = m3_enrich._estimate_cost_wall(qwen, n_groups=1000)
    assert "local" in cost.lower()


# ─── Phase E1 + E2: auto-enrich enqueue hook + drain-queue ──────────────────


def test_observation_enqueue_idempotent(stub_db, monkeypatch):
    """observation_enqueue_impl uses INSERT OR IGNORE on conversation_id —
    re-enqueueing the same conversation should be a no-op."""
    import sqlite3
    monkeypatch.setenv("M3_DATABASE", str(stub_db))
    # Migrate to add observation_queue / reflector_queue tables.
    import m3_enrich
    m3_enrich._ensure_migration_025(stub_db)

    import memory_core as mc
    r1 = mc.observation_enqueue_impl("conv-debounce-test", user_id="alice")
    r2 = mc.observation_enqueue_impl("conv-debounce-test", user_id="alice")
    r3 = mc.observation_enqueue_impl("conv-debounce-test", user_id="bob")

    assert "Enqueued" in r1
    # Re-enqueue must hit the UNIQUE conversation_id constraint via INSERT OR IGNORE
    # and return a stable queue_id (idempotent).
    assert "Enqueued" in r2
    assert "Enqueued" in r3

    conn = sqlite3.connect(str(stub_db))
    rows = conn.execute(
        "SELECT id, conversation_id, user_id FROM observation_queue "
        "WHERE conversation_id='conv-debounce-test'"
    ).fetchall()
    conn.close()
    # Exactly ONE row despite three calls — this is the debounce guarantee.
    assert len(rows) == 1
    # The first user_id wins on the row (alice), even though bob was the last
    # caller — INSERT OR IGNORE preserves the first write.
    assert rows[0][1] == "conv-debounce-test"
    assert rows[0][2] == "alice"


def test_drain_queue_mode_handles_empty_queue(stub_db, monkeypatch, capsys):
    """--drain-queue with no pending rows should print 'queue empty' and
    exit 0 — not crash or block."""
    import asyncio
    import argparse

    monkeypatch.setenv("M3_DATABASE", str(stub_db))
    import m3_enrich
    m3_enrich._ensure_migration_025(stub_db)

    args = argparse.Namespace(
        profile="enrich_local_qwen",
        profile_path=None,
        reflector_profile=None,
        core_only=False,
        chatlog_only=False,
        core_db=str(stub_db),
        chatlog_db=None,  # only one DB target — keeps the test simple
        target_variant="test-drain-empty",
        concurrency=1,
        drain_batch=10,
        drain_queue=True,
        # remaining args set so the parser-namespace mirrors real CLI invocation
        no_reflect=True,
        reflector_threshold=50,
        dry_run=False,
        skip_preflight=True,
        yes=True,
        limit=None,
        include_summaries=False,
        include_notes=False,
        include_types=None,
    )

    # _main_async dispatches drain_queue_mode early when args.drain_queue=True.
    # No SLM call is made because the queue is empty — observer.drain_queue_mode
    # returns immediately on empty.
    rc = asyncio.run(m3_enrich._main_async(args))
    assert rc == 0
    captured = capsys.readouterr()
    assert "queue empty" in captured.out
    assert "drain-queue COMPLETE" in captured.out


def test_chatlog_ingest_auto_enrich_gated_by_env(monkeypatch):
    """chatlog_ingest.py only enqueues when M3_AUTO_ENRICH=1. We can't easily
    end-to-end test the full ingest pipeline here, but we can verify the
    truthy-check by inspecting the source — as a defensive sanity check."""
    import inspect
    import importlib
    spec = importlib.util.find_spec("chatlog_ingest")
    assert spec is not None, "chatlog_ingest must be importable for E1"
    src = Path(spec.origin).read_text(encoding="utf-8")
    # Hook string-match: the env-var name and the enqueue call must both
    # appear in the post-ingest section of main(). String matching is a
    # cheap proxy for actually wiring up the host-agent hook chain.
    assert "M3_AUTO_ENRICH" in src
    assert "observation_enqueue_impl" in src
    # Debounce guard
    assert "M3_AUTO_ENRICH_MIN_TURNS" in src
