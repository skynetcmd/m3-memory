"""Guards on SQLite-only tools that bypass the backend seam.

Several runtime-critical tools open sqlite3.connect directly (bypassing the pooled
seam). On a PostgreSQL-primary deployment they would silently read/write a stale
SQLite file instead of the live PG store. Each calls require_sqlite_backend() at
entry so PG-primary gets a loud refusal, not silent corruption. These tests assert
the refusal fires under postgres and is a no-op under the default sqlite.
"""
from __future__ import annotations

import argparse
import asyncio

import pytest


def _force_pg(monkeypatch):
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_PG_URL", "postgresql://u:p@127.0.0.1:5433/none")
    from memory.backends import selector as _sel
    _sel._reset_for_tests()


def test_curate_memory_apply_works_on_postgres(monkeypatch):
    """curate_memory_apply was PORTED to PG (its bulk impls are dialected +
    backend-routed), so it must NOT refuse on postgres. An empty plan is a no-op
    that returns a structured result, never the require_sqlite_backend RuntimeError.
    (Full row-level behavior is covered by test_curator_apply_pg_live.py.)"""
    _force_pg(monkeypatch)
    import curator_apply
    out = curator_apply.apply_memory_plan({})  # empty plan: no DB work, no refusal
    assert out["store"] == "memory"
    assert out["errors"] == []
    assert out["summary"] == {"deleted_soft": 0, "deleted_hard": 0, "linked": 0, "updated": 0}


def test_cognitive_loop_refuses_on_postgres(monkeypatch):
    _force_pg(monkeypatch)
    import m3_cognitive_loop
    with pytest.raises(RuntimeError, match="SQLite-only|stale SQLite"):
        asyncio.run(m3_cognitive_loop.main_loop(argparse.Namespace(interval=1)))


def test_m3_entities_not_gated_on_postgres(monkeypatch):
    """m3_entities was ported to PG (mc._db() + dialected SQL, status column from
    pg_041). It must NOT raise the require_sqlite_backend RuntimeError. With a
    bogus profile it exits early (SystemExit) — proving the gate is gone.
    (Full DB-path behavior is in test_m3_entities_pg_live.py.)"""
    _force_pg(monkeypatch)
    import m3_entities
    try:
        asyncio.run(m3_entities._main_async(argparse.Namespace(profile="__nope__")))
    except SystemExit:
        pass  # profile-not-found exit — acceptable, proves no SQLite refusal
    except RuntimeError as e:
        if "SQLite" in str(e) or "stale SQLite" in str(e):
            pytest.fail(f"m3_entities still gated on PG: {e}")


def test_m3_enrich_not_gated_on_postgres(monkeypatch):
    """m3_enrich was ported to PG (mc._db() reads, seam writes, _PgStateConn
    state machine, tables from pg_040/041/042). It must NOT raise the
    require_sqlite_backend RuntimeError. A bogus profile exits early (SystemExit).
    (Full DB + state-machine behavior is in test_m3_enrich_pg_live.py.)"""
    _force_pg(monkeypatch)
    import m3_enrich
    try:
        asyncio.run(m3_enrich._main_async(
            argparse.Namespace(profile="__nope__", profile_path=None)
        ))
    except SystemExit:
        pass  # profile-not-found exit — acceptable, proves no SQLite refusal
    except RuntimeError as e:
        if "SQLite" in str(e) or "stale SQLite" in str(e):
            pytest.fail(f"m3_enrich still gated on PG: {e}")


def test_chroma_sync_refuses_on_postgres(monkeypatch):
    _force_pg(monkeypatch)
    import memory_sync
    with pytest.raises(RuntimeError, match="SQLite-only|stale SQLite"):
        asyncio.run(memory_sync.chroma_sync_impl())


def test_chatlog_status_main_count_na_on_postgres(monkeypatch):
    """chatlog_status must NOT read a stale SQLite main store on PG — it reports
    n/a for the primary-store count rather than crashing or misreporting."""
    _force_pg(monkeypatch)
    import chatlog_status

    result = chatlog_status.chatlog_status_impl()
    # the main-store chat_log count is marked n/a under postgres
    text = str(result)
    assert "PostgreSQL" in text or "n/a" in text


def test_guards_are_noop_on_sqlite(monkeypatch):
    """The default (sqlite) backend must not trip any guard — proving these are
    pure fail-loud gates that never affect a normal SQLite deployment."""
    monkeypatch.delenv("M3_DB_BACKEND", raising=False)
    from memory.backends import require_sqlite_backend
    from memory.backends import selector as _sel
    _sel._reset_for_tests()
    # No raise for any of the still-gated tool names. (curate_memory_apply was
    # ported to PG and no longer uses this guard.)
    for tool in ("m3_cognitive_loop", "chroma_sync"):
        require_sqlite_backend(tool)
