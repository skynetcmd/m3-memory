"""Tests for `m3 embedder reembed` (bin/reembed_space.py).

This tool DELETES vectors, so the properties that matter most are the safety
ones: dry-run must be the default and must not touch the DB, a backup must be
taken before the first delete, and the family-folding must never mistake two
tags of the SAME model for a mix (which would delete perfectly good vectors).
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import reembed_space  # noqa: E402


def _store(tmp_path, rows):
    """(vector_kind, embed_model, dim, count) -> a temp agent_memory.db."""
    db = tmp_path / "agent_memory.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE memory_embeddings (id TEXT, memory_id TEXT, embedding BLOB, "
        "embed_model TEXT, dim INTEGER, created_at TEXT, content_hash TEXT, "
        "vector_kind TEXT)"
    )
    n = 0
    for kind, model, dim, count in rows:
        for _ in range(count):
            n += 1
            con.execute(
                "INSERT INTO memory_embeddings (id, memory_id, embed_model, dim, "
                "vector_kind) VALUES (?,?,?,?,?)",
                (str(n), str(n), model, dim, kind),
            )
    con.commit()
    con.close()
    return str(db)


def _count(db):
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]
    finally:
        con.close()


MIXED = [
    ("default", "bge-m3-GGUF-Q4_K_M.gguf", 1024, 100),
    ("default", "qwen3-embedding:0.6b", 1024, 7),
]


# ── safety ───────────────────────────────────────────────────────────────────

def test_dry_run_is_the_default_and_deletes_nothing(tmp_path, capsys):
    db = _store(tmp_path, MIXED)
    assert reembed_space.main(["--db", db]) == 0
    assert _count(db) == 107, "dry run must not modify the store"
    assert "DRY RUN" in capsys.readouterr().out


def test_apply_deletes_only_the_minority_family(tmp_path):
    db = _store(tmp_path, MIXED)
    assert reembed_space.main(["--db", db, "--apply", "--no-backfill"]) == 0
    assert _count(db) == 100
    con = sqlite3.connect(db)
    left = {r[0] for r in con.execute("SELECT DISTINCT embed_model FROM memory_embeddings")}
    con.close()
    assert left == {"bge-m3-GGUF-Q4_K_M.gguf"}


def test_apply_takes_a_restorable_backup(tmp_path):
    db = _store(tmp_path, MIXED)
    reembed_space.main(["--db", db, "--apply", "--no-backfill"])
    baks = [p for p in os.listdir(tmp_path) if "-prereembed" in p]
    assert len(baks) == 1, "exactly one backup expected"
    assert _count(str(tmp_path / baks[0])) == 107, "backup must hold the pre-delete state"


def test_no_backup_flag_skips_the_copy(tmp_path):
    db = _store(tmp_path, MIXED)
    reembed_space.main(["--db", db, "--apply", "--no-backup", "--no-backfill"])
    assert not [p for p in os.listdir(tmp_path) if "-prereembed" in p]


# ── correctness of what gets chosen ──────────────────────────────────────────

def test_single_family_with_two_tags_is_a_no_op(tmp_path, capsys):
    """The dangerous false positive: two bge-m3 tags are ONE model. Deleting
    either would destroy good vectors for nothing."""
    db = _store(tmp_path, [
        ("default", "bge-m3-GGUF-Q4_K_M.gguf", 1024, 50),
        ("default", "text-embedding-bge-m3", 1024, 50),
    ])
    assert reembed_space.main(["--db", db, "--apply", "--no-backfill"]) == 0
    assert _count(db) == 100
    assert "nothing to retire" in capsys.readouterr().out


def test_keeps_the_largest_family_by_default(tmp_path, capsys):
    db = _store(tmp_path, [
        ("default", "qwen3-embedding:0.6b", 1024, 90),
        ("default", "bge-m3-GGUF-Q4_K_M.gguf", 1024, 10),
    ])
    reembed_space.main(["--db", db])
    assert "Keeping : qwen3-embedding" in capsys.readouterr().out


def test_explicit_keep_overrides_the_majority(tmp_path, capsys):
    db = _store(tmp_path, [
        ("default", "qwen3-embedding:0.6b", 1024, 90),
        ("default", "bge-m3-GGUF-Q4_K_M.gguf", 1024, 10),
    ])
    reembed_space.main(["--db", db, "--keep", "bge-m3"])
    out = capsys.readouterr().out
    assert "Keeping : bge-m3" in out
    assert "90" in out, "the larger qwen3 family should be the one retired"


def test_unknown_keep_family_errors_without_deleting(tmp_path, capsys):
    db = _store(tmp_path, MIXED)
    rc = reembed_space.main(["--db", db, "--keep", "not-a-real-family", "--apply"])
    assert rc == 1
    assert _count(db) == 107
    assert "not present in this store" in capsys.readouterr().out


def test_missing_db_errors_cleanly(tmp_path, capsys):
    rc = reembed_space.main(["--db", str(tmp_path / "nope.db")])
    assert rc == 1
    assert "no such DB" in capsys.readouterr().out


def test_empty_store_is_a_no_op(tmp_path, capsys):
    db = _store(tmp_path, [])
    assert reembed_space.main(["--db", db, "--apply"]) == 0
    assert "nothing to do" in capsys.readouterr().out.lower()


def test_backfill_handoff_always_passes_the_resolved_db(tmp_path, monkeypatch):
    """Regression (2026-07-23): the handoff omitted --db unless the user passed
    one, so embed_backfill fell back to its pre-Homecoming repo-relative default
    and aborted with "DB not found" — AFTER the deletes had committed, leaving
    the store with vectors removed and nothing regenerating them."""
    db = _store(tmp_path, MIXED)
    seen = {}

    def _fake_call(cmd):
        seen["cmd"] = cmd
        return 0

    monkeypatch.setattr("subprocess.call", _fake_call)
    # NOTE: invoked WITHOUT --db. That is the path that broke: the old code
    # only forwarded --db when the user supplied one, so the default-resolved
    # engine DB was never passed on. Passing --db here would test the case
    # that always worked.
    monkeypatch.setattr(reembed_space, "_resolve_default_db", lambda: db)
    reembed_space.main(["--apply"])
    assert "--db" in seen["cmd"], "handoff must always scope the sweeper to the target DB"
    assert seen["cmd"][seen["cmd"].index("--db") + 1] == db


def test_backfill_failure_is_reported_not_swallowed(tmp_path, capsys, monkeypatch):
    """If regeneration fails the operator must be told the store is mid-way."""
    db = _store(tmp_path, MIXED)
    monkeypatch.setattr("subprocess.call", lambda cmd: 2)
    rc = reembed_space.main(["--db", db, "--apply"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "NOT yet regenerated" in out
    assert "Re-run manually" in out


# ── backend portability (SQLite / PostgreSQL / future MariaDB) ───────────────

def test_pooled_backend_uses_the_seam_not_a_file_handle(tmp_path, monkeypatch):
    """On a pooled backend there is ONE store: the delete must go through
    active_backend().connection(), never sqlite3.connect(db_path)."""
    used = {}

    class _Cur:
        rowcount = 3
        def execute(self, sql, params):
            used.setdefault("sql", sql)
            used.setdefault("params", []).append(params)
            return self

    class _Conn:
        def cursor(self):
            return _Cur()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def commit(self):
            used["committed"] = True

    class _Dialect:
        def param(self):
            return "%s"

    class _Backend:
        name = "postgres"

        def connection(self):
            return _Conn()

        def dialect(self):
            return _Dialect()

    monkeypatch.setattr(reembed_space, "_is_file_backend", lambda: False)
    monkeypatch.setattr("memory.backends.active_backend", lambda: _Backend())
    import sqlite3 as _s
    monkeypatch.setattr(_s, "connect", lambda *a, **k: pytest.fail(
        "pooled backend must not open a sqlite file"))

    n = reembed_space._delete_doomed("ignored", [("default", "qwen3-embedding:0.6b",
                                                  "qwen3-embedding", 1024, 3)])
    assert n == 3
    assert "%s" in used["sql"], "binds must use the backend's placeholder"
    assert "?" not in used["sql"]


def test_file_backend_honours_the_db_path(tmp_path, monkeypatch):
    """On SQLite --db names a specific file; the seam's connection() takes no
    path, so using it would delete from the DEFAULT store instead."""
    db = _store(tmp_path, MIXED)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = _store(other_dir, MIXED)
    monkeypatch.setattr(reembed_space, "_is_file_backend", lambda: True)
    reembed_space._delete_doomed(db, [("default", "qwen3-embedding:0.6b",
                                       "qwen3-embedding", 1024, 7)])
    assert _count(db) == 100, "target file must lose exactly its stale rows"
    assert _count(other) == 107, "a different store must be untouched"


def test_pooled_backend_refuses_silent_backup(tmp_path, capsys, monkeypatch):
    """A file copy cannot snapshot a server-hosted store — implying otherwise
    would promise a rollback that does not exist."""
    db = _store(tmp_path, MIXED)
    monkeypatch.setattr(reembed_space, "_is_file_backend", lambda: False)
    rc = reembed_space.main(["--db", db, "--apply"])
    assert rc == 1
    assert "--no-backup is required" in capsys.readouterr().out


@pytest.mark.parametrize("rows,expected_kept", [
    # vector_kind partitions spaces by design, so one model per kind is fine.
    ([("default", "bge-m3-GGUF-Q4_K_M.gguf", 1024, 10),
      ("fallback", "qwen3-embedding:0.6b", 1024, 10)], 20),
])
def test_separate_vector_kinds_are_not_a_mix(tmp_path, rows, expected_kept):
    db = _store(tmp_path, rows)
    reembed_space.main(["--db", db, "--apply", "--no-backfill"])
    assert _count(db) == expected_kept
