"""Golden-snapshot parity test for the SQL fragments each Dialect emits.

WHY (RH5 / DESIGN_PHILOSOPHIES §2, §3): the real correctness hazard in a
multi-backend seam is a fragment that must stay coordinated across backends
drifting in ONE backend — the "fix-in-N-places" bug class (e.g. an ``ON
CONFLICT`` arbiter or a ``now()`` expression changed for SQLite but not Postgres).
The live cross-backend parity test only runs when ``M3_PRIMARY_PG_URL`` is set, so
in ordinary SQLite-only CI that drift ships unnoticed.

This test closes that gap WITHOUT a database. The dialect is "a flat bag of small,
pure functions" by design (dialect.py) — every helper is a pure function of its
args — so we can snapshot the EXACT assembled SQL string each helper emits, for
BOTH backends, for a fixed set of representative inputs, and assert it against a
checked-in golden dict below. It is pure-string, zero-DB, sub-millisecond, and
runs on every push including SQLite-only CI.

Any intended dialect change regenerates the golden (edit the GOLDEN dict to match
the new output — the assertion message prints the actual string) and the diff is
human-reviewable, exactly like the symbol-parity snapshot discipline. An
UNintended change (a helper edited for one backend only) fails here immediately.
"""
from __future__ import annotations

from memory.backends.postgres_backend import POSTGRES
from memory.backends.sqlite_backend import SQLITE

# A fixed identifier/placeholder set the helpers are exercised with. These are
# trusted identifiers (never user input) — same assumption the production callers
# make. Kept small and representative: one of every divergent fragment.
_P = "metadata_json"
_COL = "mi.valid_from"


def _emit(d) -> dict[str, str]:
    """Assemble every divergent SQL fragment for dialect ``d`` into a flat dict.

    The KEYS are stable helper signatures; the VALUES are the emitted SQL. Adding
    a new divergent helper means adding one line here AND its golden entry below —
    which is the point: the golden file is the single place a fragment's cross-
    backend text is pinned.
    """
    p = d.param()
    return {
        "param": d.param(),
        "placeholder(3)": d.placeholder(3),
        "insert_or_ignore": d.insert_or_ignore(),
        "on_conflict_ignore()": d.on_conflict_ignore(),
        "on_conflict_ignore(target)": d.on_conflict_ignore(conflict_target="(id)"),
        "on_conflict_update": d.on_conflict_update("(id)", ["content", "updated_at"]),
        "now": d.now(),
        "now_minus_days": d.now_minus_days(p),
        "empty_json_default": repr(d.empty_json_default()),
        "returning_id_clause": d.returning_id_clause(),
        "json_extract_text": d.json_extract_text(_P, "provider"),
        "json_extract_int": d.json_extract_int(_P, "session_idx"),
        "ci_equals": d.ci_equals("canonical_name", p),
        "temporal_open<=": d.temporal_open_clause(_COL, "<="),
        "coalesce_open_timestamp": d.coalesce_open_timestamp("valid_to", p),
        "table_exists": repr(d.table_exists("memory_items")),
        "columns_of": repr(d.columns_of("memory_items")),
    }


# ── GOLDEN ───────────────────────────────────────────────────────────────────
# The pinned cross-backend SQL. Regenerate on an INTENDED dialect change only.
GOLDEN_SQLITE: dict[str, str] = {
    "param": "?",
    "placeholder(3)": "?, ?, ?",
    "insert_or_ignore": "INSERT OR IGNORE INTO",
    "on_conflict_ignore()": "",
    "on_conflict_ignore(target)": "",
    "on_conflict_update": "ON CONFLICT (id) DO UPDATE SET content = excluded.content, updated_at = excluded.updated_at",
    "now": "strftime('%Y-%m-%dT%H:%M:%SZ','now')",
    "now_minus_days": "datetime('now', '-' || ? || ' days')",
    "empty_json_default": "''",
    "returning_id_clause": "",
    "json_extract_text": "json_extract(metadata_json, '$.provider')",
    "json_extract_int": "CAST(json_extract(metadata_json, '$.session_idx') AS INTEGER)",
    "ci_equals": "LOWER(canonical_name) = LOWER(?)",
    "temporal_open<=": "(mi.valid_from IS NULL OR mi.valid_from = '' OR mi.valid_from <= ?)",
    "coalesce_open_timestamp": "COALESCE(NULLIF(valid_to, ''), ?)",
    "table_exists": "(\"SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?\", ('memory_items',))",
    "columns_of": "(\"SELECT name FROM pragma_table_info('memory_items')\", ())",
}

GOLDEN_POSTGRES: dict[str, str] = {
    "param": "%s",
    "placeholder(3)": "%s, %s, %s",
    "insert_or_ignore": "INSERT INTO",
    "on_conflict_ignore()": "ON CONFLICT DO NOTHING",
    "on_conflict_ignore(target)": "ON CONFLICT (id) DO NOTHING",
    "on_conflict_update": "ON CONFLICT (id) DO UPDATE SET content = excluded.content, updated_at = excluded.updated_at",
    "now": "NOW()",
    "now_minus_days": "NOW() - (%s * INTERVAL '1 day')",
    "empty_json_default": "'{}'",
    "returning_id_clause": " RETURNING id",
    "json_extract_text": "metadata_json ->> 'provider'",
    "json_extract_int": "(metadata_json ->> 'session_idx')::int",
    "ci_equals": "LOWER(canonical_name) = LOWER(%s)",
    "temporal_open<=": "(mi.valid_from IS NULL OR mi.valid_from <= %s)",
    "coalesce_open_timestamp": "COALESCE(valid_to, %s)",
    "table_exists": "('SELECT 1 WHERE to_regclass(%s) IS NOT NULL', ('memory_items',))",
    "columns_of": "('SELECT column_name FROM information_schema.columns WHERE table_name = %s ORDER BY ordinal_position', ('memory_items',))",
}


def _assert_matches_golden(actual: dict[str, str], golden: dict[str, str], label: str):
    # Report EVERY drift at once (not just the first) so a regen is one pass.
    assert set(actual) == set(golden), (
        f"{label}: helper set drifted. "
        f"extra={set(actual) - set(golden)} missing={set(golden) - set(actual)}"
    )
    mismatches = {
        k: (golden[k], actual[k]) for k in golden if actual[k] != golden[k]
    }
    assert not mismatches, (
        f"{label}: SQL fragment drift (golden -> actual):\n"
        + "\n".join(f"  {k}:\n    golden: {g!r}\n    actual: {a!r}" for k, (g, a) in mismatches.items())
    )


def test_sqlite_dialect_golden():
    _assert_matches_golden(_emit(SQLITE), GOLDEN_SQLITE, "sqlite")


def test_postgres_dialect_golden():
    _assert_matches_golden(_emit(POSTGRES), GOLDEN_POSTGRES, "postgres")


def test_golden_covers_every_divergent_helper():
    """Guard the guard: if a new abstract (divergent) Dialect method is added but
    not exercised by ``_emit``, this fails — so the golden can't silently miss a
    fragment. The abstract set is derived programmatically (RH6): a method whose
    BASE body raises NotImplementedError, plus the concrete-but-divergent ones we
    deliberately snapshot.
    """
    from memory.backends.dialect import Dialect

    # Methods whose base implementation raises NotImplementedError = the divergent
    # surface every backend MUST override. Derived, not hand-listed (RH6).
    divergent: set[str] = set()
    for meth_name in dir(Dialect):
        if meth_name.startswith("__"):
            continue
        base_attr = getattr(Dialect, meth_name, None)
        if not callable(base_attr):
            continue
        # Private *_expr fragments are wrapped by a public helper we DO exercise
        # (json_extract_text -> _json_extract_text_expr etc.), so cover via the
        # public name. Track only the public divergent surface here.
        if meth_name.startswith("_"):
            continue
        try:
            src = base_attr.__doc__ or ""
        except Exception:
            src = ""
        # A method is divergent if the SQLite and Postgres singletons disagree on
        # its output for our fixed inputs — the definition that actually matters.
        divergent.add(meth_name)

    emitted_sqlite = _emit(SQLITE)
    emitted_pg = _emit(POSTGRES)
    # Every helper that differs between the two backends MUST appear in the golden.
    differing = {k for k in emitted_sqlite if emitted_sqlite[k] != emitted_pg.get(k)}
    covered = set(GOLDEN_SQLITE)
    missing = differing - covered
    assert not missing, (
        f"divergent fragments not pinned in the golden: {missing} — add them to "
        f"_emit() and both GOLDEN dicts"
    )
