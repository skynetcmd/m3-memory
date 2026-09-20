"""Both backends are first-class BY CONTRACT (§0.4) — so hold the test estate to it.

⚠ WHY THIS FILE EXISTS. The memory-dynamics release shipped seven new test
suites, all SQLite-only, for a change that was almost entirely new SQL. Two real
PostgreSQL defects were sitting under that green run:

  1. pg_058 added three columns to memory_items and not to chat_log_items. On PG
     the chatlog is a CLONE table in the same database; on SQLite it is a
     separate FILE using the same table names, so the SQLite migration reaches
     both stores and the PG one reached half.
  2. The forensic read never projected importance_raw on any backend, because
     the extra-column allowlist existed in THREE copies and only one knew about
     the new column. A forensic search silently ranked on the decayed value.

Neither is exotic. Both are the §0.4 hermeticity trap: "a green SQLite-only run
proves nothing about portability."

These tests are STRUCTURAL -- they run without a cluster and assert that the
coverage exists, not that it passes. The PG lane asserts that it passes.
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = _ROOT / "tests"
_PG_MIGRATIONS = _ROOT / "memory" / "migrations" / "postgres"


def _dialect_classes():
    """(SqliteDialect, PostgresDialect) — the two that must stay in step."""
    import sys
    sys.path.insert(0, str(_ROOT / "bin"))
    from memory.backends.postgres_backend import PostgresDialect
    from memory.backends.sqlite_backend import SqliteDialect
    return (SqliteDialect, PostgresDialect)


def _pg_live_sources() -> str:
    return "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in _TESTS.glob("*_pg_live.py")
    )


def test_a_pg_migration_that_adds_columns_also_touches_the_chatlog_clone():
    """⚠ THE pg_058 DEFECT, generalised.

    On PostgreSQL the chatlog tables are CLONES living in the same database
    (pg_043), so `ALTER TABLE memory_items ADD COLUMN` reaches exactly half the
    store. On SQLite the chatlog is a separate file carrying the same names, so
    the identical statement reaches both -- which is precisely why this is easy
    to miss when the SQLite migration is written first.

    A migration that adds a column to a table WITH a chat_log_* clone must
    either name the clone too, or say in a comment why the clone is exempt.
    test_schema_parity_pg_live proves it at runtime; this catches it without a
    cluster, at authoring time.
    """
    # Core tables that have a chat_log_* clone on PG. Mirrors
    # dialect._CHATLOG_TABLES -- imported rather than retyped where possible.
    import sys
    sys.path.insert(0, str(_ROOT / "bin"))
    from memory.backends.dialect import _CHATLOG_TABLES

    # ⚠ CUMULATIVE, NOT PER-FILE. A gap closed by a LATER migration is not a
    # live defect -- pg_041 missed chat_log_extraction_queue and pg_049 repaired
    # it; pg_048 missed chat_log_entities and pg_050 repaired it. Flagging those
    # would be a false alarm about already-fixed history, and §3 counts a false
    # alarm as a violation in its own right. What matters is the END STATE: by
    # the last migration, every column on a cloned core table is on its clone.
    pairs = dict(_CHATLOG_TABLES.values())          # core -> chat_log_*
    added: dict[str, set] = {}                      # table -> columns added

    col_re = re.compile(
        r"ALTER TABLE\s+([A-Za-z_][\w]*)\s+ADD COLUMN(?:\s+IF NOT EXISTS)?\s+([A-Za-z_][\w]*)",
        re.I,
    )
    for path in sorted(_PG_MIGRATIONS.glob("pg_*.up.sql")):
        sql = path.read_text(encoding="utf-8", errors="replace")
        body = "\n".join(
            ln for ln in sql.splitlines() if not ln.lstrip().startswith("--")
        )
        for table, col in col_re.findall(body):
            added.setdefault(table.lower(), set()).add(col.lower())

    offenders = []
    for core, clone in pairs.items():
        for col in sorted(added.get(core.lower(), set())):
            if col not in added.get(clone.lower(), set()):
                offenders.append(f"{core}.{col} was never added to {clone}")

    assert not offenders, (
        "a column added to a cloned core table never reached its chat_log_* "
        "clone. On PostgreSQL the chatlog is a CLONE in the same database, so "
        "the chatlog half of the store is missing it; on SQLite the chatlog is "
        "a separate FILE using the same names, so the identical statement "
        "reaches both — which is why this is easy to miss.\n  "
        + "\n  ".join(offenders)
        + "\n(pg_058 is the most recent instance; pg_049 and pg_050 are the "
          "earlier repairs.)"
    )


def test_the_memory_dynamics_feature_has_pg_coverage():
    """The specific gap this file was written after.

    Not a general "every feature needs a PG suite" rule -- that would be noise.
    These are the constructs whose SQL differs per backend, so a SQLite-only
    assertion genuinely proves nothing about them.
    """
    src = _pg_live_sources()
    missing = [
        name for name, probe in (
            ("the decay floor", "_ORDINARY_FLOOR"),
            ("the graded floor", "_GRADED_FLOOR"),
            ("reinforcement", "access_count"),
            ("the grading window (age_minutes_lt)", "memory_grade_impl"),
            ("the forensic projection", "importance_raw"),
        ) if probe not in src
    ]
    assert not missing, (
        f"no *_pg_live.py suite exercises: {missing}. Each of these renders "
        f"different SQL per backend, so a SQLite-only test asserts nothing "
        f"about PostgreSQL (§0.4)."
    )


def test_age_minutes_lt_is_implemented_on_every_backend():
    """A dialect primitive with one implementation is a portability bug waiting
    for the second backend to hit it. Both must define it, and neither may
    inherit a NotImplementedError base."""
    for dialect_cls in _dialect_classes():
        assert "age_minutes_lt" in vars(dialect_cls), (
            f"{dialect_cls.__name__} does not implement age_minutes_lt — it "
            f"would inherit the base, which is not portable SQL"
        )
        rendered = dialect_cls().age_minutes_lt("last_accessed_at", "5")
        assert "last_accessed_at" in rendered and rendered.strip(), (
            f"{dialect_cls.__name__}.age_minutes_lt rendered nothing usable"
        )


def test_the_two_backends_render_the_window_differently():
    """A shared implementation would mean one of them is wrong.

    SQLite has no interval arithmetic and PG has no julianday(); if these two
    strings ever match, the primitive has been collapsed to whatever the author
    happened to be testing on.
    """
    sq, pg = (c().age_minutes_lt("ts", "5") for c in _dialect_classes())
    assert sq != pg, (
        "both backends render the feedback window identically — one of them is "
        "running the other's SQL"
    )
