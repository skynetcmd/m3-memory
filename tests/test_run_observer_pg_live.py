"""Live-PG test for run_observer's ported DB paths (de-gated for PostgreSQL).

Skips cleanly without a reachable cluster. Exercises the SQL the observer drain
runs — the observation_queue LIMIT select, the json_extract turns query (with
turn_index extracted as INTEGER so PG's COALESCE(text,int) mismatch is avoided),
the queue DELETE, the backoff UPDATE with now(), and the source_group_id UPDATE —
directly on PG, without the SLM. The heavy SLM path is unchanged and not exercised.

DSN from M3_PRIMARY_PG_URL/M3_PG_URL (never PG_URL).
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))


# Gated by the requires_pg marker: conftest's collection hook auto-skips this
# module when no Postgres is reachable. pg_dsn() centralizes the
# M3_PRIMARY_PG_URL > M3_PG_URL precedence (replaces the former per-file
# _dsn()/_reachable()/skipif triplet).
from conftest import pg_dsn

pytestmark = pytest.mark.requires_pg
_DSN = pg_dsn()


@pytest.fixture()
def pg(monkeypatch):
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_PG_URL", _DSN)
    monkeypatch.setenv("M3_PRIMARY_PG_URL", _DSN)
    from memory.backends import selector as _selector

    _selector._reset_for_tests()
    from memory.backends.postgres_backend import PostgresBackend

    b = PostgresBackend(dsn=_DSN)
    b._schema_ready = False
    b.ensure_schema()  # applies baseline + pg_040 (observation_queue etc.)
    # ensure the pg_040 tables are present even if a prior test dropped them
    import migrate_pg

    with b.connection() as c:
        migrate_pg.run_pending_pg_migrations(c)
    yield b
    b.close()


def test_observation_queue_and_source_group_paths_on_pg(pg):
    import memory_core as mc
    from memory.backends import active_backend

    _d = active_backend().dialect()
    p = _d.param()
    conv = f"obs-{uuid.uuid4().hex[:8]}"
    mid = str(uuid.uuid4())

    with pg.connection() as c:
        cur = c.cursor()
        cur.execute(
            "INSERT INTO memory_items (id,type,content,conversation_id,scope,metadata_json) "
            "VALUES (%s,'message','hi',%s,'agent',%s)",
            (mid, conv, '{"role":"user","turn_index":3}'),
        )
        cur.execute(
            "INSERT INTO observation_queue (conversation_id,user_id,attempts) "
            "VALUES (%s,%s,0) RETURNING id",
            (conv, "u1"),
        )
        qid = cur.fetchone()[0]

    try:
        # the drain's turns query — json_extract role (text) + turn_index (int)
        je_role = _d.json_extract_text("metadata_json", "role")
        je_ti = _d.json_extract_int("metadata_json", "turn_index")
        je_sid = _d.json_extract_text("metadata_json", "session_id")
        je_cid = _d.json_extract_text("metadata_json", "conversation_id")
        with mc._db() as db:
            turns = db.execute(
                f"SELECT id, content, {je_role} AS role, COALESCE({je_ti},0) AS ti, "
                f"created_at, metadata_json FROM memory_items "
                f"WHERE COALESCE({je_sid},{je_cid},conversation_id)={p} "
                f"AND COALESCE(is_deleted,0)=0 AND type IN ('message','conversation') "
                f"ORDER BY ti ASC",
                (conv,),
            ).fetchall()
        assert len(turns) == 1
        assert turns[0]["role"] == "user"
        assert turns[0]["ti"] == 3  # extracted as integer, not text

        # backoff UPDATE (now()) + source_group_id UPDATE + queue DELETE
        with mc._db() as db:
            db.execute(
                f"UPDATE observation_queue SET attempts=attempts+1, last_error={p}, "
                f"last_attempt_at={_d.now()} WHERE id={p}",
                ("boom", qid),
            )
            db.commit()
        with mc._db() as db:
            db.execute(f"UPDATE memory_items SET source_group_id={p} WHERE id={p}", (7, mid))
            db.commit()
        with mc._db() as db:
            db.execute(f"DELETE FROM observation_queue WHERE id={p}", (qid,))
            db.commit()

        with pg.connection() as c:
            cur = c.cursor()
            cur.execute("SELECT source_group_id FROM memory_items WHERE id=%s", (mid,))
            assert cur.fetchone()[0] == 7
            cur.execute("SELECT count(*) FROM observation_queue WHERE conversation_id=%s", (conv,))
            assert cur.fetchone()[0] == 0
    finally:
        with pg.connection() as c:
            cur = c.cursor()
            cur.execute("DELETE FROM observation_queue WHERE conversation_id=%s", (conv,))
            cur.execute("DELETE FROM memory_items WHERE conversation_id=%s", (conv,))


def test_run_observer_main_not_gated_on_pg(pg, monkeypatch):
    """main() must no longer refuse on postgres. Run it with no args → it hits the
    'no mode selected' usage exit (SystemExit), NOT the require_sqlite RuntimeError."""
    import run_observer

    monkeypatch.setattr(sys, "argv", ["run_observer.py"])
    # Any exit is fine EXCEPT a require_sqlite_backend RuntimeError.
    try:
        run_observer.main()
    except SystemExit:
        pass  # argparse/usage exit — acceptable, proves the gate is gone
    except RuntimeError as e:
        if "SQLite" in str(e) or "stale SQLite" in str(e):
            pytest.fail(f"run_observer still gated on PG: {e}")
