"""Regression tests for the two bugs surfaced by the 2026-05-17 curator pass.

Bug 1: `memory_search` raised `NameError: name '_batch_cosine' is not defined`
       when `_resolve_mc_callbacks()` raised before the test-shim rebinding
       block could install the local floor bindings.

Bug 2: `memory_dedup` emitted self-pairs where `a == b` with score=1.0 when
       a memory had multiple embedding rows (e.g. v022 dual-embed adds both
       a `default` and `enriched` vector under the same `memory_id`).
"""
from __future__ import annotations

import os
import sys

# conftest.py already puts bin/ on sys.path. Belt-and-suspenders:
_BIN = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)


# ── Bug 1: _batch_cosine NameError ──────────────────────────────────────────

def test_scored_impl_has_floor_bindings_before_callback_resolution():
    """Even if `_resolve_mc_callbacks()` raises, the four locals
    (_embed, _db, _batch_cosine, _query_chroma) must already be bound from
    module globals BEFORE the callback resolution step. Otherwise a
    raised exception leaves them unbound and `LOAD_FAST` at the MMR call
    site (~line 1350) raises NameError.

    We verify the source order: the floor bindings appear earlier in the
    function body than the call to _resolve_mc_callbacks().
    """
    import ast

    import memory.search as ms

    src = open(ms.__file__).read()
    tree = ast.parse(src)
    scored = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "memory_search_scored_impl"
    )

    floor_line = None
    callback_line = None
    for stmt in ast.walk(scored):
        # First Assign to _batch_cosine = globals()["_batch_cosine"]
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and stmt.targets[0].id == "_batch_cosine"
            and isinstance(stmt.value, ast.Subscript)
            and isinstance(stmt.value.value, ast.Call)
            and isinstance(stmt.value.value.func, ast.Name)
            and stmt.value.value.func.id == "globals"
        ):
            if floor_line is None or stmt.lineno < floor_line:
                floor_line = stmt.lineno
        # First Call to _resolve_mc_callbacks()
        if (
            isinstance(stmt, ast.Call)
            and isinstance(stmt.func, ast.Name)
            and stmt.func.id == "_resolve_mc_callbacks"
        ):
            if callback_line is None or stmt.lineno < callback_line:
                callback_line = stmt.lineno

    assert floor_line is not None, (
        "memory_search_scored_impl is missing the floor binding "
        "`_batch_cosine = globals()['_batch_cosine']`. Restore it BEFORE "
        "the call to _resolve_mc_callbacks() or the function will NameError "
        "if callback resolution raises."
    )
    assert callback_line is not None, "missing _resolve_mc_callbacks call"
    assert floor_line < callback_line, (
        f"Floor bindings (line {floor_line}) must appear BEFORE "
        f"_resolve_mc_callbacks() (line {callback_line}). Otherwise a "
        f"raise from the callback leaves the locals unbound and Python's "
        f"LOAD_FAST raises NameError at the bare-name call sites."
    )


def test_callback_resolution_is_wrapped_in_try_except():
    """`_resolve_mc_callbacks()` is best-effort — if it raises, the call
    site must catch and continue, falling through to the floor bindings."""
    import ast

    import memory.search as ms

    src = open(ms.__file__).read()
    tree = ast.parse(src)
    scored = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "memory_search_scored_impl"
    )

    # Find a Try block that wraps a _resolve_mc_callbacks() call.
    wrapped = False
    for stmt in ast.walk(scored):
        if not isinstance(stmt, ast.Try):
            continue
        for inner in ast.walk(stmt):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "_resolve_mc_callbacks"
            ):
                wrapped = True
                break
        if wrapped:
            break
    assert wrapped, (
        "memory_search_scored_impl: _resolve_mc_callbacks() must be wrapped "
        "in a try/except so a raise doesn't leave _batch_cosine etc. unbound."
    )


# ── Bug 2: memory_dedup self-pairs ──────────────────────────────────────────

def test_dedup_skips_self_pairs_when_multi_embed_rows_exist():
    """When a memory has 2 embedding rows (dual-embed: default + enriched),
    the SELECT in `memory_dedup_impl` returns it twice. Without the
    self-pair guard the loop emits a (X, X, 1.0) pair. This test stubs
    the DB read to force the multi-row case and confirms no self-pair
    survives."""
    from unittest.mock import MagicMock, patch

    import memory_maintenance as mm

    # Two rows with the SAME memory_id but DIFFERENT embeddings (default+enriched).
    # A real bge-m3 vector is 1024 floats; we use small fake vectors and skip
    # the Rust fast path by making the blobs the wrong size.
    fake_id = "00000000-0000-0000-0000-000000000abc"
    other_id = "00000000-0000-0000-0000-000000000def"
    # Use small bytes so use_rust is False (forces the pure-Python branch).
    fake_blob_a = b"\x01" * 16
    fake_blob_b = b"\x02" * 16
    fake_blob_c = b"\x01" * 16  # identical to A → high cosine with A

    fake_rows = [
        {"memory_id": fake_id,   "embedding": fake_blob_a, "title": "T1"},
        {"memory_id": fake_id,   "embedding": fake_blob_b, "title": "T1"},
        {"memory_id": other_id,  "embedding": fake_blob_c, "title": "T2"},
    ]

    # Mock the DB context manager to return our fake rows.
    fake_db = MagicMock()
    fake_db.execute.return_value.fetchall.return_value = fake_rows
    fake_cm = MagicMock()
    fake_cm.__enter__ = MagicMock(return_value=fake_db)
    fake_cm.__exit__ = MagicMock(return_value=False)

    # Stub _unpack: just turn bytes into a list[float] of length 4 — enough
    # for _cosine to compute something non-zero without needing real
    # 1024-dim vectors.
    def _fake_unpack(b):
        return [float(x) for x in b[:4]]

    # Stub _cosine: return 1.0 for the two identical blobs, lower otherwise.
    def _fake_cosine(a, b):
        return 1.0 if a == b else 0.1

    with patch.object(mm, "_db", return_value=fake_cm), \
         patch.object(mm, "_unpack", side_effect=_fake_unpack), \
         patch.object(mm, "_cosine", side_effect=_fake_cosine), \
         patch.object(mm, "m3_core_rs", None):  # force pure-Python branch
        result = mm.memory_dedup_impl(threshold=0.9, dry_run=True)

    # No group should have a == b.
    for g in result["groups"]:
        assert g["a"] != g["b"], (
            f"self-pair emitted: {g}. Same memory_id appearing on both "
            f"sides of a dup pair indicates multi-embed rows weren't filtered."
        )


def test_dedup_skips_self_pairs_rust_path():
    """Same guard must be on the Rust fast path. We can't easily stub
    `m3_core_rs.cosine_batch_packed_flat`, but we can confirm the source
    has the `if ids[i] == ids[j]: continue` guard in the Rust branch."""
    import inspect

    import memory_maintenance as mm

    src = inspect.getsource(mm.memory_dedup_impl)
    # Find the Rust branch by looking for cosine_batch_packed_flat
    assert "cosine_batch_packed_flat" in src
    # And confirm the self-pair guard is present after it.
    rust_section = src.split("cosine_batch_packed_flat", 1)[1]
    guard_present = (
        "ids[i] == ids[j]" in rust_section
        or "ids[j] == ids[i]" in rust_section
        or "mid_a == mid_b" in rust_section  # if refactored later
    )
    assert guard_present, (
        "Rust dedup branch is missing the self-pair guard "
        "(`if ids[i] == ids[j]: continue` or equivalent). Without it the "
        "scan emits {a: X, b: X, score: 1.0} pairs for memories with "
        "multiple embedding rows."
    )
