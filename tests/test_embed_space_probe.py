"""Tests for the mixed embed-space doctor probe.

The probe's job is to catch a store holding vectors from two different
embedding models — cosine across spaces is meaningless, so the minority rows
rank wrongly with no error anywhere. The risk that matters most here is a FALSE
POSITIVE: one model legitimately carries several tags (the in-process GGUF, the
llama-server path and the CPU fallback all tag ``bge-m3-GGUF-Q4_K_M.gguf``,
while LM Studio tags the same model ``text-embedding-bge-m3``), and telling a
healthy operator their store is corrupt would be worse than staying quiet.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

from doctor import embed_space_probe as probe  # noqa: E402

# ── family folding ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("tag,expected", [
    # The bge-m3 aliases documented in docs/EMBED_INPUT_RECIPE.md — all one family.
    ("bge-m3-GGUF-Q4_K_M.gguf", "bge-m3"),
    ("text-embedding-bge-m3", "bge-m3"),
    ("BGE-M3", "bge-m3"),
    # Qwen3 tags seen in the wild (Ollama's and LM Studio's spellings).
    ("qwen3-embedding:0.6b", "qwen3-embedding"),
    ("text-embedding-qwen3-embedding-0.6b", "qwen3-embedding"),
    ("nomic-embed-text-v1.5", "nomic-embed"),
])
def test_family_folds_known_aliases(tag, expected):
    assert probe._family(tag) == expected


def test_unknown_tag_is_its_own_family():
    """Conservative: a model we have never seen is treated as a distinct space
    rather than assumed compatible with anything."""
    assert probe._family("some-future-model-v9") == "some-future-model-v9"


def test_empty_tag_is_untagged():
    assert probe._family("") == "<untagged>"
    assert probe._family(None) == "<untagged>"


# ── end-to-end against a temp store ──────────────────────────────────────────

def _store(tmp_path, rows):
    """Build a minimal agent_memory.db with the given
    (vector_kind, embed_model, dim, count) rows."""
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
    return db


@pytest.fixture
def _engine(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_ENGINE_ROOT", str(tmp_path))
    return tmp_path


def test_single_family_multiple_tags_is_not_flagged(_engine, capsys):
    """The critical false-positive case: two bge-m3 tags are ONE space."""
    _store(_engine, [
        ("default", "bge-m3-GGUF-Q4_K_M.gguf", 1024, 50),
        ("default", "text-embedding-bge-m3", 1024, 50),
    ])
    assert probe.run(brief=True) == 0
    out = capsys.readouterr().out
    assert "MIXED" not in out
    assert "ok" in out.lower()


def test_two_families_are_flagged(_engine, capsys):
    _store(_engine, [
        ("default", "bge-m3-GGUF-Q4_K_M.gguf", 1024, 990),
        ("default", "qwen3-embedding:0.6b", 1024, 10),
    ])
    assert probe.run(brief=True) == 0
    assert "MIXED" in capsys.readouterr().out


def test_verbose_names_the_minority_and_the_fix(_engine, capsys):
    _store(_engine, [
        ("default", "bge-m3-GGUF-Q4_K_M.gguf", 1024, 990),
        ("default", "qwen3-embedding:0.6b", 1024, 10),
    ])
    probe.run(brief=False)
    out = capsys.readouterr().out
    assert "qwen3-embedding" in out
    assert "minority" in out
    assert "EMBED_INPUT_RECIPE" in out


def test_dim_split_reported(_engine, capsys):
    """Different dimensions cannot be compared at all — a harder error."""
    _store(_engine, [
        ("default", "bge-m3-GGUF-Q4_K_M.gguf", 1024, 10),
        ("default", "some-small-model", 384, 10),
    ])
    probe.run(brief=False)
    assert "MIXED DIMENSIONS" in capsys.readouterr().out


def test_separate_vector_kinds_do_not_cross_contaminate(_engine, capsys):
    """vector_kind partitions spaces by design, so one model per kind is fine."""
    _store(_engine, [
        ("default", "bge-m3-GGUF-Q4_K_M.gguf", 1024, 10),
        ("fallback", "qwen3-embedding:0.6b", 1024, 10),
    ])
    probe.run(brief=True)
    assert "MIXED" not in capsys.readouterr().out


def test_empty_store_is_ok(_engine, capsys):
    _store(_engine, [])
    assert probe.run(brief=True) == 0
    assert "ok" in capsys.readouterr().out.lower()


def test_missing_table_does_not_crash(_engine, capsys):
    """A probe must never crash the doctor run."""
    db = _engine / "agent_memory.db"
    sqlite3.connect(db).close()
    assert probe.run(brief=True) == 0
    assert "unknown" in capsys.readouterr().out.lower()
