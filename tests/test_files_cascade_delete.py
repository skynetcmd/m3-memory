"""corpus_delete(cascade=True) must not leave orphans.

The files schema declares `ON DELETE CASCADE` on leaves / ingestion_runs / facts /
embeddings, and corpus_delete relies on it (it deletes only file_nodes and lets
the cascade do the rest). But SQLite's `foreign_keys` pragma is OFF by default and
is per-connection, so without enabling it on every connection the cascades
silently no-op and deleting a corpus leaves orphaned leaves + ingestion_runs
behind. These pin (a) the pragma is on for a fresh connection and (b) a real
cascade delete removes the descendants. Hermetic — a temp SQLite file, no server.
"""
import sys
from pathlib import Path

_BIN = str(Path(__file__).resolve().parents[1] / "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import files_memory.db as DB  # noqa: E402
from files_memory.config import files_table  # noqa: E402
from files_memory.corpora import corpus_delete  # noqa: E402


def test_new_connection_enables_foreign_keys(tmp_path):
    conn = DB._new_connection(str(tmp_path / "f.db"))
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def _seed(db_path: str, corpus: str) -> None:
    """Insert one file_node -> ingestion_run -> leaf (+embedding) in `corpus`."""
    DB.init_db(db_path)
    with DB._db(db_path) as c:
        c.execute(
            f"INSERT INTO {files_table('file_nodes')}"
            "(uuid, identity_key, filename, filetype, path_absolute, size_bytes,"
            " content_sha256, date_modified, source_host, version_label, corpus_id)"
            " VALUES ('fn1','/x/a.txt','a.txt','text','/x/a.txt',10,'sha',"
            "'2026-01-01','host','ingest-1', ?)",
            (corpus,),
        )
        c.execute(
            f"INSERT INTO {files_table('ingestion_runs')}"
            "(uuid, file_node, run_id, ingester_version, chunker_version, extract_mode)"
            " VALUES ('run1','fn1','walk1','iv','cv','none')",
        )
        c.execute(
            f"INSERT INTO {files_table('leaves')}"
            "(uuid, file_node, ingestion_run, division_type, division_id, text,"
            " text_sha256, char_range_start, char_range_end)"
            " VALUES ('lf1','fn1','run1','window','0','hello','tsha',0,5)",
        )
        c.execute(
            f"INSERT INTO {files_table('leaf_embeddings')}"
            "(leaf_uuid, kind, embedding, embed_model, dim)"
            " VALUES ('lf1','text', X'00', 'm', 1)",
        )


def _counts(db_path: str) -> dict:
    with DB._db(db_path) as c:
        return {
            t: c.execute(f"SELECT COUNT(*) FROM {files_table(t)}").fetchone()[0]
            for t in ("file_nodes", "leaves", "ingestion_runs", "leaf_embeddings")
        }


def test_cascade_delete_leaves_no_orphans(tmp_path):
    db = str(tmp_path / "files.db")
    _seed(db, "smoke")
    assert _counts(db) == {"file_nodes": 1, "leaves": 1, "ingestion_runs": 1, "leaf_embeddings": 1}

    report = corpus_delete("smoke", cascade=True, db_path=db)
    assert report["deleted"]["file_nodes"] == 1
    assert report["deleted"]["leaves"] == 1

    # The cascade must have removed leaves, ingestion_runs AND leaf_embeddings —
    # not just the file_nodes rows the delete statement named.
    assert _counts(db) == {"file_nodes": 0, "leaves": 0, "ingestion_runs": 0, "leaf_embeddings": 0}
