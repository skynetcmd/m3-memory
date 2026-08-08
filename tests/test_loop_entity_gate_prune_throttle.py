"""Cognitive-loop efficiency: entity work-gate + prune throttle.

Two fixes for a loop that pegged a CPU core:
  1. `_entity_work_sql` must exclude rows the extractor has recorded terminal in
     entity_extraction_queue ('done' — incl. empty extractions — 'ctx_error',
     'failed' past the cap). Otherwise a turn that extracts to ZERO entities has no
     link row and reads as "work" forever -> has_entity_work never goes False ->
     the idle-backlog-drain re-ticks every ~1s and re-runs every heavy pass.
  2. The CPU-heavy chatlog-prune sweep is throttled to once per --interval via the
     same `_due` min-interval gate the time-driven passes use.
Hermetic — a temp SQLite DB and a temp config dir, no loop, no server.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import m3_cognitive_loop as L  # noqa: E402
import m3_entities as ME  # noqa: E402


def _mk_db(path):
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE memory_items (id INTEGER PRIMARY KEY, type TEXT, content TEXT, is_deleted INT DEFAULT 0);
        CREATE TABLE memory_item_entities (memory_id INTEGER, entity_id INTEGER);
        CREATE TABLE entity_extraction_queue (memory_id INTEGER, status TEXT, attempts INT DEFAULT 0);
    """)
    t = ME.DEFAULT_TYPES[0]  # a type the gate actually counts
    # row 1: fresh, never attempted -> genuine work
    c.execute("INSERT INTO memory_items(id,type,content) VALUES (1,?,'fresh turn')", (t,))
    # row 2: extracted to ZERO entities, recorded 'done' -> NOT work
    c.execute("INSERT INTO memory_items(id,type,content) VALUES (2,?,'empty turn')", (t,))
    c.execute("INSERT INTO entity_extraction_queue(memory_id,status) VALUES (2,'done')")
    # row 3: failed past the attempt cap -> NOT work
    c.execute("INSERT INTO memory_items(id,type,content) VALUES (3,?,'doomed turn')", (t,))
    c.execute("INSERT INTO entity_extraction_queue(memory_id,status,attempts) VALUES (3,'failed',?)",
              (ME.MAX_ENTITY_ATTEMPTS,))
    # row 4: already has an entity link -> NOT work
    c.execute("INSERT INTO memory_items(id,type,content) VALUES (4,?,'linked turn')", (t,))
    c.execute("INSERT INTO memory_item_entities(memory_id,entity_id) VALUES (4,99)")
    c.commit()
    return c


def test_gate_counts_only_genuine_work(tmp_path):
    c = _mk_db(str(tmp_path / "m.db"))
    sql = L._entity_work_sql("memory_items", "memory_item_entities").replace("LIMIT 1", "")
    work_ids = sorted(r[0] for r in c.execute(sql.replace("SELECT 1", "SELECT mi.id")))
    # only row 1 (fresh) is work; 2 (done-empty), 3 (failed-exhausted), 4 (linked) excluded
    assert work_ids == [1]


def test_gate_goes_empty_once_fresh_row_is_done(tmp_path):
    c = _mk_db(str(tmp_path / "m.db"))
    # extractor processes row 1, extracts nothing, marks it done
    c.execute("INSERT INTO entity_extraction_queue(memory_id,status) VALUES (1,'done')")
    c.commit()
    sql = L._entity_work_sql("memory_items", "memory_item_entities")
    assert c.execute(sql).fetchone() is None  # no work left -> loop can go idle


def _probe(conn, items="memory_items", links="memory_item_entities", queue="entity_extraction_queue"):
    return len(L._probe_entity_work(lambda s: conn.execute(s).fetchall(), items, links, queue))


def _mini_db(path, *, with_queue=True, with_status=True):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE memory_items(id INTEGER PRIMARY KEY, type TEXT, content TEXT, is_deleted INT DEFAULT 0)")
    c.execute("CREATE TABLE memory_item_entities(memory_id INTEGER)")
    t = ME.DEFAULT_TYPES[0]
    c.execute("INSERT INTO memory_items(id,type,content) VALUES (1,?,'a'),(2,?,'b')", (t, t))
    if with_queue:
        cols = "memory_id INTEGER" + (", status TEXT, attempts INT DEFAULT 0" if with_status else "")
        c.execute(f"CREATE TABLE entity_extraction_queue({cols})")
    c.commit()
    return c


def test_exclusion_applies_when_queue_status_present(tmp_path):
    c = _mini_db(str(tmp_path / "m.db"))
    c.execute("INSERT INTO entity_extraction_queue(memory_id,status) VALUES (1,'done'),(2,'done')")
    c.commit()
    assert _probe(c) == 0  # both done -> no work (exclusion works, loop can idle)


def test_degrades_to_link_only_when_status_column_missing(tmp_path):
    # Brand-new DB: queue table exists but no status column yet (before first
    # extraction adds it). Must NOT raise, must NOT fail-open incorrectly — it
    # degrades to link-only, which is correct (nothing recorded terminal yet).
    c = _mini_db(str(tmp_path / "m.db"), with_status=False)
    assert _probe(c) > 0  # returns (no exception) -> link-only says there's work


def test_degrades_to_link_only_when_queue_table_missing(tmp_path):
    c = _mini_db(str(tmp_path / "m.db"), with_queue=False)
    assert _probe(c) > 0  # no queue table at all -> link-only, no exception


def test_prune_throttle_due_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "get_m3_config_root", lambda: str(tmp_path), raising=False)
    assert L._due("chatlog_prune", 300.0) is True   # never run -> due
    L._record_pass_run("chatlog_prune")
    assert L._due("chatlog_prune", 300.0) is False  # just ran -> throttled
    assert L._due("chatlog_prune", 0.0) is True     # zero interval -> always due
