"""Piping is only useful if output composes and the pipeline can complete.

Universal stdin made every tool pipeable, which turned two latent defects into
blockers for things a user would obviously now try:

  1. ⚠ OUTPUT WAS DOUBLE-ENCODED. `--as_records` builds a real
     {count, items[], query} object and serialises it; the CLI then json.dumps'd
     that STRING, producing JSON-inside-JSON. `| jq '.items[].id'` returned
     nothing, silently, because jq was handed a string.

  2. ⚠ memory_grade WAS UNREACHABLE FROM THE CLI. Access stamps are batched by
     a 0.25s async task that lives for the lifetime of an event loop. The MCP
     server has one; a CLI invocation exits first, so a shell search stamped
     nothing — and `last_accessed_at` is what authorises a grade, so every
     piped verdict came back `outside_feedback_window` no matter how fast it
     was sent.

Underneath (2) was the more serious one: the flusher's UPDATE carried literal
`?` placeholders, which PostgreSQL rejects, and the caller swallowed the error
at debug level. On PG `last_accessed_at` was NEVER written and `access_count`
never moved, so reinforcement had nothing to read and grading could never open
its window. See test_chatlog_pg_live / test_memory_dynamics_pg_live for the
backend-live half; this file pins the seam usage and the CLI behaviour.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

_REPO = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_BIN = os.path.join(_REPO, "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)


def _run(args, stdin_text=None, timeout=180):
    """Invoke the WORKING-TREE cli, never the installed `m3` on PATH."""
    return subprocess.run(
        [sys.executable, "-m", "m3_memory.cli", *args],
        input=stdin_text, capture_output=True, text=True,
        cwd=_REPO, timeout=timeout,
    )


# ── output composes ──────────────────────────────────────────────────────────

def test_as_records_is_not_double_encoded():
    """⚠ THE DEFECT. One json.loads must reach the object, not a string.

    A consumer that has to parse twice is not composable: every jq filter, every
    `| python -c`, every hook silently sees a string where it expected records.
    """
    r = _run(["memory", "memory_search", "--query", "postgres", "--k", "2",
              "--as_records"])
    assert r.returncode == 0, r.stderr[-400:]
    payload = json.loads(r.stdout)
    assert isinstance(payload, dict), (
        f"stdout parsed to {type(payload).__name__}, not an object — the result "
        f"is still JSON inside JSON"
    )
    assert "items" in payload and isinstance(payload["items"], list)


def test_a_prose_result_is_still_valid_json():
    """§3: unwrapping must not break the 'stdout is always JSON' contract.

    Only a result that genuinely parses as an object/array is unwrapped; a
    human-readable string stays a JSON string so the pipe never sees raw prose.
    """
    r = _run(["memory", "memory_search", "--query", "postgres", "--k", "1"])
    assert r.returncode == 0, r.stderr[-400:]
    payload = json.loads(r.stdout)          # must not raise
    assert isinstance(payload, str), (
        "a prose result was emitted unwrapped, so stdout is no longer "
        "guaranteed-parseable"
    )


def test_ids_can_be_extracted_without_scraping_text():
    """The point of the change: field access, not regex over a rendered blob.

    Seeds its own row rather than assuming a corpus — the sandbox DB is empty,
    and a test that skips on "no results" would go green on a broken
    projection.
    """
    marker = "zeta-pipeline-marker-" + os.urandom(4).hex()
    w = _run(["memory", "memory_write", "--content", marker,
              "--type", "note", "--title", marker])
    assert w.returncode == 0, w.stderr[-300:]

    r = _run(["memory", "memory_search", "--query", marker, "--k", "3",
              "--as_records"])
    assert r.returncode == 0, r.stderr[-400:]
    items = json.loads(r.stdout)["items"]
    assert items, "the seeded memory was not returned"
    for it in items:
        assert it.get("id"), f"record without an id: {sorted(it)}"


# ── the pipeline completes ───────────────────────────────────────────────────

def test_a_cli_search_stamps_access_so_a_grade_can_land():
    """⚠ THE BLOCKER. Without a synchronous drain the process exits before the
    0.25s batcher ticks, so nothing is stamped and every grade is rejected as
    stale — the feature is simply unreachable from a shell.

    Asserts the stamp on the ids the search ACTUALLY RETURNED. An earlier
    version of this check picked a row by a LIKE filter and then looked for a
    stamp on it; the ranked query never returned that row, so it read as "still
    broken" when the drain was working.
    """
    import memory_core as mc

    marker = "stamp-probe-" + os.urandom(4).hex()
    assert _run(["memory", "memory_write", "--content", marker,
                 "--type", "note", "--title", marker]).returncode == 0

    r = _run(["memory", "memory_search", "--query", marker,
              "--k", "3", "--as_records"])
    assert r.returncode == 0, r.stderr[-400:]
    ids = [i["id"] for i in json.loads(r.stdout)["items"]]
    assert ids, "the seeded memory was not returned — nothing to assert on"

    with mc._db() as db:
        ph = ",".join("?" * len(ids))
        rows = db.execute(
            f"SELECT id, last_accessed_at FROM memory_items WHERE id IN ({ph})",
            ids,
        ).fetchall()

    unstamped = [r0[0][:8] for r0 in rows if not r0[1]]
    assert not unstamped, (
        f"{len(unstamped)} returned memory/ies carry no last_accessed_at after "
        f"a CLI search ({unstamped}); memory_grade cannot accept a verdict for "
        f"them, so the search|grade pipeline is dead from the shell"
    )


def test_search_then_grade_composes_end_to_end():
    """The pipeline a user would now obviously write."""
    marker = "grade-probe-" + os.urandom(4).hex()
    assert _run(["memory", "memory_write", "--content", marker,
                 "--type", "note", "--title", marker]).returncode == 0

    search = _run(["memory", "memory_search", "--query", marker,
                   "--k", "2", "--as_records"])
    assert search.returncode == 0, search.stderr[-400:]
    items = json.loads(search.stdout)["items"]
    assert items, "the seeded memory was not returned — nothing to grade"

    grades = json.dumps({"grades": [
        {"memory_id": i["id"], "verdict": "helpful"} for i in items
    ]})
    graded = _run(["memory", "memory_grade", "--json-file", "-"],
                  stdin_text=grades)
    assert graded.returncode == 0, graded.stderr[-400:]
    out = json.loads(graded.stdout)
    assert out["applied"] is True, (
        f"grades did not apply: {out.get('reason')} / {out.get('observed')}"
    )
    assert out["graded"] == len(items)


# ── the access flusher goes through the seam ─────────────────────────────────

def test_the_access_flusher_does_not_hardcode_placeholders():
    """⚠ POSTGRES REJECTED THIS SQL OUTRIGHT, AND THE ERROR WAS SWALLOWED.

    `SET last_accessed_at = ?` is a syntax error on PG, and the caller logs at
    debug, so on that backend the column was never written and access_count
    never moved. Everything downstream died quietly with it: reinforcement had
    no signal to read, and memory_grade's window could never open.

    §10a — the call site expresses intent and the dialect renders the SQL.
    """
    import inspect

    from memory import db as mdb

    src = inspect.getsource(mdb._flush_access_batch)
    assert "_d.param()" in src or "_p" in src, (
        "the access flusher no longer resolves its placeholder from the dialect"
    )
    assert 'last_accessed_at = ?' not in src, (
        "the access flusher hardcodes a '?' placeholder again — PostgreSQL "
        "rejects it and the failure is swallowed at debug level"
    )


def test_flush_is_a_no_op_when_nothing_is_pending():
    """Called on every CLI invocation, including ones that read nothing."""
    from memory import db as mdb

    mdb._access_pending.clear()
    assert mdb.flush_access_stamps_now() == 0


def test_flush_never_raises_into_the_caller():
    """A failed stamp must not fail the command that produced the answer."""
    from memory import db as mdb

    mdb._access_pending.clear()
    mdb._access_pending.add("no-such-id-" + "0" * 20)
    mdb.flush_access_stamps_now()  # must not raise
    assert not mdb._access_pending, "pending set was not drained"


# ── every search path records retrieval ──────────────────────────────────────

def test_all_three_search_paths_stamp_through_one_owner():
    """⚠ THE MOST PRECISE RESULTS WERE THE LEAST LIKELY TO BE STAMPED.

    A search can be answered by three paths, two of which RETURN EARLY: the FTS
    exact-phrase short-circuit, the no-embedder fallback, and the main ranked
    path. Only the last stamped, and it filtered on `"bm25_score" in row` —
    which the short-circuit's rows do not carry. So an exact title match was
    returned without recording that it had been seen, and grading it then failed
    as stale.

    Asserts the structural property rather than re-deriving it: one owner, and
    no path hand-rolling its own id extraction.
    """
    import inspect

    from memory import search as S

    src = inspect.getsource(S)
    assert src.count("_enqueue_access_stamps(") == 1, (
        "more than one call site extracts ids for stamping — the copies drifted "
        "last time, leaving the short-circuit path unstamped"
    )
    # And every early return routes through the owner.
    assert src.count("_stamp_retrieved(") >= 4, (
        "a search path returns without stamping; the short-circuit and the "
        "FTS-only fallback both answer in full and must record retrieval"
    )


def test_an_exact_phrase_hit_is_stamped(tmp_path):
    """The concrete case: search by exact title, then confirm it is gradeable."""
    marker = "exactphrase-" + os.urandom(4).hex()
    assert _run(["memory", "memory_write", "--content", marker,
                 "--type", "note", "--title", marker]).returncode == 0

    # An exact-substring query >3 chars takes the short-circuit path.
    r = _run(["memory", "memory_search", "--query", marker, "--k", "1",
              "--as_records"])
    assert r.returncode == 0, r.stderr[-400:]
    items = json.loads(r.stdout)["items"]
    assert items, "the exact-phrase query returned nothing"

    graded = _run(["memory", "memory_grade", "--json-file", "-"],
                  stdin_text=json.dumps({"grades": [
                      {"memory_id": items[0]["id"], "verdict": "helpful"}]}))
    out = json.loads(graded.stdout)
    assert out["applied"] is True, (
        f"an exact-phrase hit could not be graded: {out.get('reason')} — the "
        f"short-circuit path is not recording retrieval"
    )
