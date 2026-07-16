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


def test_curate_memory_apply_refuses_on_postgres(monkeypatch):
    _force_pg(monkeypatch)
    import curator_apply
    with pytest.raises(RuntimeError, match="SQLite-only|stale SQLite"):
        curator_apply.apply_memory_plan({})


def test_cognitive_loop_refuses_on_postgres(monkeypatch):
    _force_pg(monkeypatch)
    import m3_cognitive_loop
    with pytest.raises(RuntimeError, match="SQLite-only|stale SQLite"):
        asyncio.run(m3_cognitive_loop.main_loop(argparse.Namespace(interval=1)))


def test_m3_entities_refuses_on_postgres(monkeypatch):
    _force_pg(monkeypatch)
    import m3_entities
    with pytest.raises(RuntimeError, match="SQLite-only|stale SQLite"):
        asyncio.run(m3_entities._main_async(argparse.Namespace(profile="default")))


def test_m3_enrich_refuses_on_postgres(monkeypatch):
    _force_pg(monkeypatch)
    import m3_enrich
    with pytest.raises(RuntimeError, match="SQLite-only|stale SQLite"):
        asyncio.run(m3_enrich._main_async(
            argparse.Namespace(profile="default", profile_path=None)
        ))


def test_guards_are_noop_on_sqlite(monkeypatch):
    """The default (sqlite) backend must not trip any guard — proving these are
    pure fail-loud gates that never affect a normal SQLite deployment."""
    monkeypatch.delenv("M3_DB_BACKEND", raising=False)
    from memory.backends import require_sqlite_backend
    from memory.backends import selector as _sel
    _sel._reset_for_tests()
    # No raise for any of the tool names.
    for tool in ("curate_memory_apply", "m3_cognitive_loop", "m3_entities",
                 "m3_enrich", "run_observer"):
        require_sqlite_backend(tool)
