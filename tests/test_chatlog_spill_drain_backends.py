"""chatlog spill drainage must work on EVERY supported backend.

A spilled turn exists only as a JSONL line on disk — not in any store, not
FTS-searchable, not embeddable — until `drain_spill` recovers it. Spill is
written by `chatlog_core._spill_batch` on ANY flush exception and on queue-full
backpressure, with no backend gating, so a PostgreSQL-backed deployment spills
exactly like a SQLite one (a PG outage is precisely when spill accumulates).

Before 2026-08-09 `drain_spill` hardcoded `INSERT OR IGNORE INTO memory_items`
with `?` placeholders and took a `sqlite3.Connection`. On PostgreSQL that is
wrong three independent ways — the table is `chat_log_items`, `INSERT OR IGNORE`
is SQLite-only syntax, and the placeholder is `%s` — so spilled turns could
never be recovered. These tests pin the seam-routed behaviour (§10a).
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))


def _write_spill(spill_dir: Path, marker: str, n: int = 3) -> list[str]:
    """Write `n` spilled chatlog turns and return their ids."""
    spill_dir.mkdir(parents=True, exist_ok=True)
    ids = [str(uuid.uuid4()) for _ in range(n)]
    with (spill_dir / "20260809.jsonl").open("w", encoding="utf-8") as fh:
        for i, rid in enumerate(ids):
            fh.write(json.dumps({
                "_id": rid,
                "_content": f"{marker} turn {i}",
                "_title": "spill-drain-test",
                "role": "user",
                "conversation_id": f"{marker}-conv",
                "host_agent": "pytest",
                "_created_at": "2026-08-09T00:00:00Z",
            }) + "\n")
    return ids


def _drain_and_count(marker: str) -> tuple[int, int]:
    """Run the shipped drain_spill and count what landed. Returns (drained, found)."""
    import chatlog_embed_sweeper
    from m3_sdk import M3Context
    from memory.backends import chatlog_table, dialect

    drained = asyncio.run(chatlog_embed_sweeper.drain_spill())

    ctx = M3Context.for_db(None)
    with ctx.get_chatlog_conn() as conn:
        cur = conn.execute(
            f"SELECT COUNT(*) FROM {chatlog_table('items')} "
            f"WHERE conversation_id = {dialect().param()}",
            (f"{marker}-conv",),
        )
        found = cur.fetchone()[0]
    return drained, found


def test_drain_spill_sqlite(tmp_path, monkeypatch):
    """SQLite: spilled turns land in memory_items and the spill file is removed."""
    from conftest import create_full_main_schema

    db = tmp_path / "chatlog.db"
    create_full_main_schema(str(db))
    monkeypatch.setenv("M3_DATABASE", str(db))
    monkeypatch.setenv("M3_CHATLOG_DB_PATH", str(db))

    import chatlog_config
    spill_dir = tmp_path / "spill"
    monkeypatch.setattr(chatlog_config, "SPILL_DIR", str(spill_dir))

    marker = f"vv-{uuid.uuid4().hex[:10]}"
    _write_spill(spill_dir, marker)

    drained, found = _drain_and_count(marker)
    assert drained == 3
    assert found == 3
    # A fully-drained file is deleted; a partially-failed one is KEPT (never
    # discard rows that did not land).
    assert not list(spill_dir.glob("*.jsonl"))


@pytest.mark.requires_pg
def test_drain_spill_postgres(tmp_path, monkeypatch, pg_url):
    """PostgreSQL: the same drain lands rows in chat_log_items via the seam.

    This is the case the pre-2026-08-09 implementation could not do at all.
    """
    monkeypatch.setenv("M3_PRIMARY_PG_URL", pg_url)
    monkeypatch.setenv("M3_PG_URL", pg_url)
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")

    from memory.backends import dialect
    # Guard against a silent sqlite fallback reporting a hollow pass.
    assert dialect().backend == "postgres", (
        f"expected the postgres dialect, resolved {dialect().backend!r} — "
        "the test would otherwise 'pass' without exercising PG at all"
    )

    import chatlog_config
    spill_dir = tmp_path / "spill"
    monkeypatch.setattr(chatlog_config, "SPILL_DIR", str(spill_dir))

    marker = f"vv-{uuid.uuid4().hex[:10]}"
    _write_spill(spill_dir, marker)

    drained, found = _drain_and_count(marker)
    assert drained == 3
    assert found == 3
    assert not list(spill_dir.glob("*.jsonl"))


def test_drain_preserves_tenancy_and_provenance_fields(tmp_path, monkeypatch):
    """§7: a drained turn must keep user_id/scope, or it loses its tenancy.

    `Dialect.scope_predicates` filters every search by user_id + scope. A row
    inserted without them is invisible to its owner's scoped search — or worse,
    unscoped. content_hash backs dedup, and origin_device's column DEFAULT is
    the literal 'macbook', so omitting it mislabels every drained row on a
    Windows or Linux host.

    The spill file carries all of this (a spill line is the queued item
    verbatim). Regression for a drain that hand-rolled a 10-column INSERT and
    silently dropped the other 12 (2026-08-09).
    """
    from conftest import create_full_main_schema

    db = tmp_path / "chatlog.db"
    create_full_main_schema(str(db))
    monkeypatch.setenv("M3_DATABASE", str(db))
    monkeypatch.setenv("M3_CHATLOG_DB_PATH", str(db))

    import chatlog_config
    spill_dir = tmp_path / "spill"
    spill_dir.mkdir()
    monkeypatch.setattr(chatlog_config, "SPILL_DIR", str(spill_dir))

    marker = f"vv-{uuid.uuid4().hex[:10]}"
    rid = str(uuid.uuid4())
    (spill_dir / "t.jsonl").write_text(json.dumps({
        "_id": rid,
        "_content": "tenant-scoped turn",
        "_title": "t",
        "_created_at": "2026-08-09T00:00:00Z",
        "conversation_id": f"{marker}-conv",
        "user_id": "tenant-alice",
        "scope": "private",
        "origin_device": "test-device-01",
        "model_id": "claude-opus-5",
        "host_agent": "claude-code",
    }) + "\n", encoding="utf-8")

    import chatlog_embed_sweeper
    assert asyncio.run(chatlog_embed_sweeper.drain_spill()) == 1

    from m3_sdk import M3Context
    from memory.backends import chatlog_table, dialect
    ctx = M3Context.for_db(None)
    with ctx.get_chatlog_conn() as conn:
        row = conn.execute(
            f"SELECT user_id, scope, origin_device, content_hash, model_id "
            f"FROM {chatlog_table('items')} WHERE id = {dialect().param()}",
            (rid,),
        ).fetchone()

    assert row is not None, "drained row not found"
    user_id, scope, origin_device, content_hash, model_id = row
    assert user_id == "tenant-alice", "tenancy lost — row escapes scope_predicates"
    assert scope == "private", "scope lost — row escapes scope_predicates"
    assert origin_device == "test-device-01", "origin_device lost (column default is 'macbook')"
    assert content_hash, "content_hash missing — dedup broken"
    assert model_id == "claude-opus-5"


@pytest.mark.requires_pg
def test_stale_target_guard_is_sqlite_only(tmp_path, monkeypatch, pg_url):
    """§10/§10a: the stale-target guard must NOT run on a pooled backend.

    On PostgreSQL `_db_path` is a LABEL, not a location, so a filesystem check
    on it is meaningless — and non-deterministic: os.path.abspath() resolves a
    bare label against the CWD (drains from one working directory, refused from
    another), while a DSN-shaped label fails outright and would strand PG spill
    permanently. Regression for a guard added and corrected on 2026-08-09; the
    first version gated on nothing and would have refused every PG drain whose
    label did not happen to look like a local relative path.
    """
    monkeypatch.setenv("M3_PRIMARY_PG_URL", pg_url)
    monkeypatch.setenv("M3_PG_URL", pg_url)
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")

    from memory.backends import dialect
    assert dialect().backend == "postgres"

    import chatlog_config
    spill_dir = tmp_path / "spill"
    spill_dir.mkdir()
    monkeypatch.setattr(chatlog_config, "SPILL_DIR", str(spill_dir))

    marker = f"vv-{uuid.uuid4().hex[:10]}"
    # A DSN-shaped label: no such directory exists anywhere on disk.
    (spill_dir / "pg.jsonl").write_text(json.dumps({
        "_id": str(uuid.uuid4()),
        "_content": "pg spilled turn",
        "conversation_id": f"{marker}-conv",
        "_db_path": "postgresql://db.internal:5432/m3",
    }) + "\n", encoding="utf-8")

    drained, found = _drain_and_count(marker)
    assert drained == 1, "PG spill was refused by a SQLite-only filesystem guard"
    assert found == 1
    assert not list(spill_dir.glob("*.jsonl"))


def test_drain_spill_refuses_stale_target_and_keeps_file(tmp_path, monkeypatch):
    """§3: a stale `_db_path` must not silently conjure an orphan store.

    SQLite creates a database on connect, and the seam creates missing parent
    directories — so a spilled row pointing at a deleted tree (an old engine
    root, a removed benchmark DB) would be written into a brand-new empty file
    nobody reads, and the spill file deleted afterwards. The turns would be
    reported "drained" and be effectively lost.

    Found 2026-08-09 while adding the backend V&V above: the write SUCCEEDED
    against a path whose parent directory did not exist. Guard: refuse the
    target, keep the spill file, log loudly.
    """
    from conftest import create_full_main_schema

    db = tmp_path / "chatlog.db"
    create_full_main_schema(str(db))
    monkeypatch.setenv("M3_DATABASE", str(db))
    monkeypatch.setenv("M3_CHATLOG_DB_PATH", str(db))

    import chatlog_config
    spill_dir = tmp_path / "spill"
    monkeypatch.setattr(chatlog_config, "SPILL_DIR", str(spill_dir))

    marker = f"vv-{uuid.uuid4().hex[:10]}"
    _write_spill(spill_dir, marker)

    # Point a spilled row at an unwritable target so its group fails.
    bad = spill_dir / "20260810.jsonl"
    bad.write_text(json.dumps({
        "_id": str(uuid.uuid4()),
        "_content": "unroutable",
        "conversation_id": f"{marker}-conv",
        "_db_path": str(tmp_path / "nonexistent-dir" / "nope.db"),
    }) + "\n", encoding="utf-8")

    import chatlog_embed_sweeper
    asyncio.run(chatlog_embed_sweeper.drain_spill())

    # The good file drained and is gone; the failing one is retained.
    assert bad.exists(), "a spill file with an undrained row must be kept, not deleted"
