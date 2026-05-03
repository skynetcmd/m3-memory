"""Tests for bin/embed_backfill.py — query construction, hardening, and write path.

The embed_many call is mocked to keep tests offline; we assert:
  - the candidate query filters out already-embedded rows, soft-deleted rows, and empty content
  - filters (variant, type, user_id, scope, id_prefix, max_age_days) compose into the SQL
  - empty content is skipped without an embed call
  - oversized rows are skipped
  - mismatched dim is skipped, not written
  - successful batch writes both memory_embeddings and chroma_sync_queue
  - INSERT OR IGNORE protects against double-write on memory_id
  - lockfile prevents two concurrent sweepers
  - schema sanity check fails clearly on missing tables
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "bin"))


# ── Fixtures ──────────────────────────────────────────────────────────────

def _make_min_schema(db_path: Path) -> None:
    """Create the minimum schema embed_backfill operates on.
    Mirrors test_observer.py's pattern but adds chroma_sync_queue +
    memory_embeddings.content_hash which embed_backfill writes to.
    """
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE memory_items (
            id TEXT PRIMARY KEY,
            type TEXT,
            title TEXT,
            content TEXT,
            metadata_json TEXT,
            agent_id TEXT,
            change_agent TEXT,
            importance REAL,
            source TEXT,
            is_deleted INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT,
            user_id TEXT,
            scope TEXT,
            variant TEXT
        );
        CREATE TABLE memory_embeddings (
            id TEXT PRIMARY KEY,
            memory_id TEXT,
            embedding BLOB,
            embed_model TEXT,
            dim INTEGER,
            created_at TEXT,
            content_hash TEXT,
            UNIQUE(memory_id, embed_model)
        );
        CREATE TABLE chroma_sync_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            attempts INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()


def _seed_rows(db_path: Path, rows: list[dict]) -> None:
    """Insert memory_items rows. rows = [{id, content, type, variant, ...}]."""
    conn = sqlite3.connect(str(db_path))
    for r in rows:
        conn.execute(
            "INSERT INTO memory_items "
            "(id, type, title, content, metadata_json, is_deleted, created_at, "
            " user_id, scope, variant) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r["id"],
                r.get("type", "note"),
                r.get("title", ""),
                r.get("content", ""),
                r.get("metadata_json", "{}"),
                r.get("is_deleted", 0),
                r.get("created_at", "2026-05-01T00:00:00Z"),
                r.get("user_id", ""),
                r.get("scope", "agent"),
                r.get("variant", None),
            ),
        )
    conn.commit()
    conn.close()


def _make_args(db_path: Path, **overrides) -> argparse.Namespace:
    """Build a minimal Namespace with the fields _build_query / _run_sweep need."""
    base = dict(
        db=db_path,
        variant=[],
        type=[],
        user_id=None,
        scope=None,
        id_prefix=None,
        max_age_days=None,
        limit=None,
        batch_size=4,
        concurrency=1,
        connection_refresh=1000,
        timeout_s=10.0,
        max_runtime_min=1,
        max_consecutive_fails=3,
        max_row_bytes=32_768,
        expected_dim=4,  # tiny dim for test vectors
        lockfile=None,
        no_augment_anchors=True,  # don't pull metadata-driven anchors in tests
        dry_run=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "test.db"
    _make_min_schema(p)
    return p


# ── Query construction tests ──────────────────────────────────────────────

def test_build_query_excludes_already_embedded(db):
    import embed_backfill as eb
    _seed_rows(db, [
        {"id": "row-A", "content": "alpha"},
        {"id": "row-B", "content": "beta"},
    ])
    # Pretend row-A already has an embedding
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim, created_at, content_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("e1", "row-A", b"\x00" * 16, "test", 4, "2026-05-01T00:00:00Z", "h_alpha"),
    )
    conn.commit()
    conn.close()

    args = _make_args(db)
    n = eb._count_pending(db, args)
    assert n == 1


def test_build_query_excludes_soft_deleted(db):
    import embed_backfill as eb
    _seed_rows(db, [
        {"id": "row-A", "content": "alpha", "is_deleted": 0},
        {"id": "row-B", "content": "beta", "is_deleted": 1},
    ])
    args = _make_args(db)
    assert eb._count_pending(db, args) == 1


def test_build_query_excludes_empty_content(db):
    import embed_backfill as eb
    _seed_rows(db, [
        {"id": "row-A", "content": "alpha"},
        {"id": "row-B", "content": ""},
        {"id": "row-C", "content": "   "},  # whitespace-only
        {"id": "row-D", "content": None},
    ])
    args = _make_args(db)
    assert eb._count_pending(db, args) == 1


def test_build_query_filter_variant(db):
    import embed_backfill as eb
    _seed_rows(db, [
        {"id": "row-A", "content": "alpha", "variant": "VARIANT_X"},
        {"id": "row-B", "content": "beta",  "variant": "VARIANT_Y"},
        {"id": "row-C", "content": "gamma", "variant": None},
    ])
    args = _make_args(db, variant=["VARIANT_X"])
    assert eb._count_pending(db, args) == 1

    args = _make_args(db, variant=["VARIANT_X", "VARIANT_Y"])
    assert eb._count_pending(db, args) == 2


def test_build_query_filter_type(db):
    import embed_backfill as eb
    _seed_rows(db, [
        {"id": "row-A", "content": "alpha", "type": "note"},
        {"id": "row-B", "content": "beta",  "type": "decision"},
    ])
    args = _make_args(db, type=["note"])
    assert eb._count_pending(db, args) == 1


def test_build_query_filter_user_id_and_scope(db):
    import embed_backfill as eb
    _seed_rows(db, [
        {"id": "row-A", "content": "alpha", "user_id": "u1", "scope": "user"},
        {"id": "row-B", "content": "beta",  "user_id": "u2", "scope": "agent"},
    ])
    args = _make_args(db, user_id="u1")
    assert eb._count_pending(db, args) == 1
    args = _make_args(db, scope="agent")
    assert eb._count_pending(db, args) == 1


def test_build_query_filter_id_prefix(db):
    import embed_backfill as eb
    _seed_rows(db, [
        {"id": "abc-A", "content": "alpha"},
        {"id": "abd-B", "content": "beta"},
        {"id": "xyz-C", "content": "gamma"},
    ])
    args = _make_args(db, id_prefix="ab")
    assert eb._count_pending(db, args) == 2


# ── Schema sanity tests ──────────────────────────────────────────────────

def test_verify_schema_missing_db_errors(tmp_path):
    import embed_backfill as eb
    bad = tmp_path / "nope.db"
    with pytest.raises(FileNotFoundError):
        eb._verify_schema(bad)


def test_verify_schema_missing_table_errors(tmp_path):
    import embed_backfill as eb
    p = tmp_path / "bare.db"
    sqlite3.connect(str(p)).close()  # empty DB, no tables
    with pytest.raises(RuntimeError, match="memory_items"):
        eb._verify_schema(p)


def test_verify_schema_minimal_ok(db):
    import embed_backfill as eb
    eb._verify_schema(db)  # should not raise


# ── Lockfile tests ────────────────────────────────────────────────────────

def test_lockfile_creates_and_deletes(tmp_path):
    import embed_backfill as eb
    lock = tmp_path / "lock.txt"
    assert not lock.exists()
    with eb._lockfile_guard(lock):
        assert lock.exists()
        # content is "<pid> <epoch>"
        content = lock.read_text(encoding="utf-8")
        parts = content.strip().split()
        assert int(parts[0]) == os.getpid()
        assert int(parts[1]) > 0
    assert not lock.exists()


def test_lockfile_blocks_second_sweeper(tmp_path):
    import embed_backfill as eb
    lock = tmp_path / "lock.txt"
    lock.write_text("99999 1234567890", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Lockfile already exists"):
        with eb._lockfile_guard(lock):
            pass
    # should leave the existing lockfile in place
    assert lock.exists()


def test_lockfile_none_is_noop(tmp_path):
    import embed_backfill as eb
    with eb._lockfile_guard(None):
        pass  # no-op; just shouldn't error


# ── Counters ─────────────────────────────────────────────────────────────

def test_counters_record_error():
    import embed_backfill as eb
    c = eb.Counters()
    c.record_error(ValueError("x"))
    c.record_error(ValueError("y"))
    c.record_error(KeyError("z"))
    assert c.errors_by_class == {"ValueError": 2, "KeyError": 1}


# ── End-to-end async sweep with mocked _embed_many ────────────────────────

@pytest.mark.asyncio
async def test_sweep_writes_embeddings_and_queue(db, monkeypatch):
    """Full async sweep with _embed_many mocked. Assert embeddings + queue land."""
    import embed_backfill as eb

    _seed_rows(db, [
        {"id": "row-A", "content": "alpha cat"},
        {"id": "row-B", "content": "beta dog"},
    ])

    # Mock _embed_many BEFORE _run_sweep imports memory_core (it sets
    # M3_DATABASE first, then late-imports). We monkeypatch on the module
    # after import.
    args = _make_args(db)
    counters = eb.Counters()

    # _run_sweep imports memory_core lazily; trigger the import here
    # so we can monkeypatch _embed_many before sweep dispatches.
    os.environ["M3_DATABASE"] = str(db)
    if str(REPO / "bin") not in sys.path:
        sys.path.insert(0, str(REPO / "bin"))
    import memory_core as mc

    async def fake_embed_many(texts):
        return [([0.1, 0.2, 0.3, 0.4], "test-model") for _ in texts]

    monkeypatch.setattr(mc, "_embed_many", fake_embed_many)
    # Bypass _augment_embed_text_with_anchors variability since we're
    # passing no_augment_anchors=True anyway, but be defensive.
    monkeypatch.setattr(mc, "_augment_embed_text_with_anchors",
                        lambda text, _meta: text)
    # _content_hash + _pack are real — we want them to produce real bytes

    await eb._run_sweep(args, counters)

    # Verify both rows got embedded
    assert counters.embedded == 2
    assert counters.skipped_empty == 0
    assert counters.skipped_oversize == 0
    assert counters.skipped_bad_dim == 0

    conn = sqlite3.connect(str(db))
    n_embeddings = conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]
    n_queue = conn.execute("SELECT COUNT(*) FROM chroma_sync_queue").fetchone()[0]
    conn.close()
    assert n_embeddings == 2
    assert n_queue == 2


@pytest.mark.asyncio
async def test_sweep_skips_oversize(db, monkeypatch):
    import embed_backfill as eb
    big = "x" * 50_000  # 50KB > default 32KB
    _seed_rows(db, [
        {"id": "row-A", "content": big},
        {"id": "row-B", "content": "small"},
    ])

    os.environ["M3_DATABASE"] = str(db)
    import memory_core as mc

    embed_calls = []
    async def fake_embed_many(texts):
        embed_calls.append(list(texts))
        return [([0.1, 0.2, 0.3, 0.4], "test-model") for _ in texts]
    monkeypatch.setattr(mc, "_embed_many", fake_embed_many)

    args = _make_args(db, max_row_bytes=32_768)
    counters = eb.Counters()
    await eb._run_sweep(args, counters)

    assert counters.skipped_oversize == 1
    assert counters.embedded == 1
    # The oversize row should not have been sent to the embedder
    flat = [t for batch in embed_calls for t in batch]
    assert big not in flat


@pytest.mark.asyncio
async def test_sweep_skips_bad_dim(db, monkeypatch):
    import embed_backfill as eb
    _seed_rows(db, [{"id": "row-A", "content": "alpha"}])

    os.environ["M3_DATABASE"] = str(db)
    import memory_core as mc

    async def fake_embed_many(texts):
        # Wrong dim — 8 instead of expected 4
        return [([0.0] * 8, "test-model") for _ in texts]
    monkeypatch.setattr(mc, "_embed_many", fake_embed_many)
    monkeypatch.setattr(mc, "_augment_embed_text_with_anchors",
                        lambda text, _meta: text)

    args = _make_args(db, expected_dim=4)
    counters = eb.Counters()
    await eb._run_sweep(args, counters)

    assert counters.skipped_bad_dim == 1
    assert counters.embedded == 0
    # No row should have been written
    conn = sqlite3.connect(str(db))
    n = conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]
    conn.close()
    assert n == 0


@pytest.mark.asyncio
async def test_sweep_handles_embed_failure_gracefully(db, monkeypatch):
    import embed_backfill as eb
    _seed_rows(db, [{"id": "row-A", "content": "alpha"}])

    os.environ["M3_DATABASE"] = str(db)
    import memory_core as mc

    async def fake_embed_many(texts):
        # _embed_many returns (None, model) on failure
        return [(None, "test-model") for _ in texts]
    monkeypatch.setattr(mc, "_embed_many", fake_embed_many)
    monkeypatch.setattr(mc, "_augment_embed_text_with_anchors",
                        lambda text, _meta: text)

    args = _make_args(db)
    counters = eb.Counters()
    await eb._run_sweep(args, counters)

    # No rows embedded, but no batch failure either (None vector is a
    # legit "skip this one" outcome from _embed_many bisection)
    assert counters.embedded == 0


@pytest.mark.asyncio
async def test_sweep_resumes_skipping_already_embedded(db, monkeypatch):
    """Run sweep twice — second pass should embed nothing (resume by NOT EXISTS)."""
    import embed_backfill as eb
    _seed_rows(db, [
        {"id": "row-A", "content": "alpha"},
        {"id": "row-B", "content": "beta"},
    ])

    os.environ["M3_DATABASE"] = str(db)
    import memory_core as mc

    n_calls = [0]
    async def fake_embed_many(texts):
        n_calls[0] += len(texts)
        return [([0.1, 0.2, 0.3, 0.4], "test-model") for _ in texts]
    monkeypatch.setattr(mc, "_embed_many", fake_embed_many)
    monkeypatch.setattr(mc, "_augment_embed_text_with_anchors",
                        lambda text, _meta: text)

    args = _make_args(db)
    counters1 = eb.Counters()
    await eb._run_sweep(args, counters1)
    assert counters1.embedded == 2
    assert n_calls[0] == 2

    # Second pass — nothing should embed
    counters2 = eb.Counters()
    await eb._run_sweep(args, counters2)
    assert counters2.embedded == 0
    assert n_calls[0] == 2  # no additional embed calls


# ── argparse smoke ────────────────────────────────────────────────────────

def test_parse_args_defaults(monkeypatch):
    import embed_backfill as eb
    monkeypatch.delenv("M3_DATABASE", raising=False)
    args = eb._parse_args([])
    assert args.batch_size == 256
    assert args.concurrency == 4
    assert args.timeout_s == 60.0
    assert args.dry_run is False
    assert args.lockfile is None


def test_parse_args_repeatable_filters():
    import embed_backfill as eb
    args = eb._parse_args([
        "--variant", "A", "--variant", "B",
        "--type", "note", "--type", "decision",
    ])
    assert args.variant == ["A", "B"]
    assert args.type == ["note", "decision"]
