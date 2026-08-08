"""Cognitive-loop files-extraction drain pass — on by default, cheap when no files.

The loop now drains queued file-extraction leaves (extract_mode='queue' ->
extraction_status='pending') via a governor-paced pass, the files analogue of the
entity pass. Its gate MUST be a silent, near-zero-cost no-op when there is no files
store — never creating or migrating one — so installs that don't use file
ingestion pay nothing. Backend/OS-agnostic (SQLite here; PG probes the primary
schema). Hermetic — a temp SQLite file, no server.
"""
import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import m3_cognitive_loop as L  # noqa: E402
from files_memory.extract import has_pending_extraction  # noqa: E402


def test_no_store_is_silent_noop_and_never_creates(tmp_path):
    missing = tmp_path / "no_such_files.db"
    assert has_pending_extraction(str(missing)) is False
    assert not missing.exists()  # the gate must NOT create the store
    assert L.has_files_extract_work(str(missing)) is False


def test_detects_pending_leaf(tmp_path):
    db = tmp_path / "files.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE leaves(uuid TEXT, extraction_status TEXT)")
    c.commit()
    assert has_pending_extraction(str(db)) is False  # exists, none pending
    c.execute("INSERT INTO leaves VALUES('x','pending')")
    c.commit()
    c.close()
    assert has_pending_extraction(str(db)) is True
    assert L.has_files_extract_work(str(db)) is True


def test_partial_db_without_leaves_table_is_no_work(tmp_path):
    db = tmp_path / "partial.db"
    c = sqlite3.connect(db); c.execute("CREATE TABLE other(a)"); c.commit(); c.close()
    assert has_pending_extraction(str(db)) is False  # tolerant, no crash


def test_pass_exists_and_honors_skip(tmp_path, monkeypatch):
    assert hasattr(L, "run_files_extract_pass")
    # skip=True -> the pass must not even consult the gate (fully off).
    called = {"gate": False}
    monkeypatch.setattr(L, "has_files_extract_work",
                        lambda *_a, **_k: called.__setitem__("gate", True) or True)
    args = argparse.Namespace(skip_files_extract=True, files_db=None, limit_per_pass=4)
    asyncio.run(L.run_files_extract_pass(args))
    assert called["gate"] is False  # short-circuited by skip


def test_pass_is_quiet_noop_when_no_store(tmp_path):
    # run_files_extract_pass must return quickly without touching an LLM when
    # there's no files store (gate short-circuits, on-by-default path).
    args = argparse.Namespace(skip_files_extract=False, files_db=str(tmp_path / "absent.db"),
                              limit_per_pass=4)
    asyncio.run(L.run_files_extract_pass(args))  # no exception, no store created
    assert not (tmp_path / "absent.db").exists()
