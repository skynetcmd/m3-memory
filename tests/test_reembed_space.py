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


@pytest.mark.parametrize("rows,expected_kept", [
    # vector_kind partitions spaces by design, so one model per kind is fine.
    ([("default", "bge-m3-GGUF-Q4_K_M.gguf", 1024, 10),
      ("fallback", "qwen3-embedding:0.6b", 1024, 10)], 20),
])
def test_separate_vector_kinds_are_not_a_mix(tmp_path, rows, expected_kept):
    db = _store(tmp_path, rows)
    reembed_space.main(["--db", db, "--apply", "--no-backfill"])
    assert _count(db) == expected_kept
