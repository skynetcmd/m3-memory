"""Live-PG tests for the entity/graph/orchestration/enrich port.

Skips cleanly without a reachable cluster. Asserts (a) ensure_schema() creates
every table the ported subsystems need, and (b) the ported impls that use the
backend-divergent constructs — ci_equals (was COLLATE NOCASE), json_extract_int
(was CAST(json_extract())), and notify's RETURNING id (was last_insert_rowid())
— actually work end to end on PostgreSQL, persisting their side effects.

The DSN is resolved from M3_PG_URL / PG_URL and MUST be a throwaway cluster; a
forbidden-host guard (M3_PG_FORBIDDEN_HOSTS) refuses to run destructively against
named infrastructure hosts.
"""
from __future__ import annotations

import json
import os
import uuid

import pytest

_FORBIDDEN = [
    h.strip() for h in os.environ.get("M3_PG_FORBIDDEN_HOSTS", "").split(",") if h.strip()
]


# Gated by the requires_pg marker (auto-skips when no Postgres reachable).
# pg_dsn() centralizes the M3_PRIMARY_PG_URL > M3_PG_URL precedence — NEVER PG_URL
# (the deprecated warehouse var points at production; a live test resolving to it
# would run destructive DDL against the warehouse).
from conftest import pg_dsn

pytestmark = pytest.mark.requires_pg
_DSN = pg_dsn()

# The nine tables the ported entity/graph/orchestration/enrich subsystems require
# and which pg_primary_v1.sql must create.
_PORTED_TABLES = [
    "entities",
    "entity_relationships",
    "memory_item_entities",
    "entity_extraction_queue",
    "entity_embeddings",
    "fact_enrichment_queue",
    "notifications",
    "tasks",
    "bypass_surface",
]


@pytest.fixture()
def pg_backend(monkeypatch):
    assert _DSN is not None
    if any(f in _DSN for f in _FORBIDDEN):
        pytest.fail("refusing to run destructive tests against a forbidden host")
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_PG_URL", _DSN)

    from memory.backends import selector as _selector

    _selector._reset_for_tests()
    from memory.backends.postgres_backend import PostgresBackend

    b = PostgresBackend(dsn=_DSN)
    # Other live-PG tests share this cluster and recreate core tables with MINIMAL
    # shapes; ensure_schema()'s CREATE TABLE IF NOT EXISTS would then skip them and
    # leave columns missing (e.g. agents.agent_id). Drop the tables this suite owns
    # first so ensure_schema rebuilds the FULL shape deterministically.
    with b.connection() as c:
        c.cursor().execute(
            "DROP TABLE IF EXISTS notifications, tasks, bypass_surface, "
            "memory_item_entities, entity_relationships, entity_extraction_queue, "
            "entity_embeddings, fact_enrichment_queue, entities, agents, "
            "memory_history, memory_corroborations, memory_relationships, "
            "memory_embeddings, memory_items CASCADE"
        )
    b._schema_ready = False
    b.ensure_schema()
    yield b
    b.close()


def test_ensure_schema_creates_all_ported_tables(pg_backend):
    """ensure_schema() must create every table the port depends on — else the
    dialected SQL runs on PG and hits 'relation does not exist'."""
    with pg_backend.connection() as c:
        cur = c.cursor()
        for t in _PORTED_TABLES:
            cur.execute("SELECT to_regclass(%s)", (t,))
            assert cur.fetchone()[0] is not None, f"table {t} missing from PG schema"


def test_entity_count_ci_equals_on_pg(pg_backend, monkeypatch):
    """list_mentions_impl resolves a canonical_name case-insensitively on PG.

    This is the COLLATE-NOCASE -> ci_equals(LOWER=LOWER) path; a lowercase query
    must still match a mixed-case entity name."""
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_PG_URL", _DSN)
    from memory.backends import selector as _selector

    _selector._reset_for_tests()

    conv = f"cnt-{uuid.uuid4().hex[:8]}"
    mem_id = f"m-{uuid.uuid4().hex[:8]}"
    ent_id = f"e-{uuid.uuid4().hex[:8]}"
    with pg_backend.connection() as c:
        cur = c.cursor()
        cur.execute(
            "INSERT INTO memory_items (id, type, content, conversation_id, scope) "
            "VALUES (%s,'note','x',%s,'agent')",
            (mem_id, conv),
        )
        cur.execute(
            "INSERT INTO entities (id, canonical_name, entity_type) VALUES (%s,%s,'product')",
            (ent_id, "Widget"),
        )
        cur.execute(
            "INSERT INTO memory_item_entities (memory_id, entity_id, mention_offset) "
            "VALUES (%s,%s,0)",
            (mem_id, ent_id),
        )
    try:
        from memory import entity_count as ec

        assert ec.count_entities_impl(conv)["count"] == 1
        r = ec.list_mentions_impl(conv, canonical_name="widget")  # lowercase
        assert r["entity_id"] == ent_id
        assert mem_id in r["memory_ids"]
    finally:
        with pg_backend.connection() as c:
            cur = c.cursor()
            cur.execute("DELETE FROM memory_item_entities WHERE memory_id=%s", (mem_id,))
            cur.execute("DELETE FROM entities WHERE id=%s", (ent_id,))
            cur.execute("DELETE FROM memory_items WHERE id=%s", (mem_id,))


def test_graph_json_extract_int_session_window_on_pg(pg_backend, monkeypatch):
    """_neighbor_session_ids finds an adjacent-session turn via json_extract_int.

    Exercises (metadata_json ->> 'session_idx')::int on PG (was
    CAST(json_extract(..)) on SQLite)."""
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_PG_URL", _DSN)
    from memory.backends import selector as _selector

    _selector._reset_for_tests()

    conv = f"gjw-{uuid.uuid4().hex[:8]}"
    ids = [f"m-{uuid.uuid4().hex[:8]}" for _ in range(2)]
    with pg_backend.connection() as c:
        cur = c.cursor()
        for i, mid in enumerate(ids):
            cur.execute(
                "INSERT INTO memory_items (id,type,content,conversation_id,scope,metadata_json,valid_from) "
                "VALUES (%s,'note',%s,%s,'agent',%s,NOW())",
                (mid, f"turn {i}", conv, json.dumps({"session_idx": i, "turn_idx": 0})),
            )
    try:
        from memory import graph as g

        out = g._neighbor_session_ids([ids[0]], window=1, cap_per_session=5)
        assert ids[1] in out, "adjacent-session turn not found via json_extract_int"
    finally:
        with pg_backend.connection() as c:
            c.cursor().execute("DELETE FROM memory_items WHERE conversation_id=%s", (conv,))


def test_orchestration_notify_returning_and_task_tree_on_pg(pg_backend, monkeypatch):
    """notify_impl reads its generated id via RETURNING (no last_insert_rowid on
    PG), and the recursive task_tree CTE runs on PG."""
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_PG_URL", _DSN)
    from memory.backends import selector as _selector

    _selector._reset_for_tests()

    from memory import orchestration as orch

    agent_id = f"ag-{uuid.uuid4().hex[:6]}"
    try:
        orch.agent_register_impl(agent_id, "worker", ["x"], {"k": "v"})
        assert orch._agent_exists(agent_id)
        msg = orch.notify_impl(agent_id, "ping", {"hello": "world"})
        nid = int(msg.split("id=")[1].rstrip(")"))
        assert nid > 0  # RETURNING id path

        tid = orch.task_create_impl("do a thing", agent_id).split("Task created:")[1].strip()
        orch.task_assign_impl(tid, agent_id)
        assert "in_progress" in orch.task_get_impl(tid)
        assert tid[:8] in orch.task_tree_impl(tid)
    finally:
        with pg_backend.connection() as c:
            cur = c.cursor()
            cur.execute("DELETE FROM notifications WHERE agent_id=%s", (agent_id,))
            cur.execute("DELETE FROM tasks WHERE created_by=%s", (agent_id,))
            cur.execute("DELETE FROM agents WHERE agent_id=%s", (agent_id,))
