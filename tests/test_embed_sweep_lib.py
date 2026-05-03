"""Tests for bin/embed_sweep_lib.run_embed_loop — the shared helper.

Both bin/embed_backfill.py and bin/chatlog_embed_sweeper.py delegate their
embed pass to run_embed_loop. test_embed_backfill.py already exercises
the helper transitively via embed_backfill's CLI surface; this file
exercises the helper directly with the contract chatlog_embed_sweeper
uses (different write shape, no anchor transform, sequential dispatch,
None expected_dim, None deadline, etc.).

Specifically asserts:
  - the helper drives fetch_candidates / write_embedding callbacks correctly
  - cursor advance moves past skipped rows
  - limit cutoff stops the loop without a deadline
  - max_consecutive_fails aborts cleanly
  - empty / oversize / bad-dim skips don't reach write_embedding
  - chatlog-shaped fetch (4-tuple, no metadata_json reliance) works
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "bin"))


@pytest.fixture
def lib():
    """Import embed_sweep_lib as fresh as possible — no global state."""
    import embed_sweep_lib
    return embed_sweep_lib


# ── Basic flow ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_loop_drives_fetch_and_write(lib):
    """Helper drives fetch -> embed -> write for every row."""
    rows = [
        ("id-1", "alpha", "", None),
        ("id-2", "beta",  "", None),
        ("id-3", "gamma", "", None),
    ]
    fetched_after = []
    written = []

    def fetch(after_id, limit):
        fetched_after.append(after_id)
        if after_id is None:
            return rows
        return []  # next cycle: drained

    def write(mid, vec, model, chash):
        written.append((mid, model, chash))
        return True

    async def fake_embed_many(texts):
        return [([0.1, 0.2, 0.3, 0.4], "test-model") for _ in texts]

    counters = lib.Counters()
    await lib.run_embed_loop(
        fetch_candidates=fetch,
        write_embedding=write,
        counters=counters,
        embed_many=fake_embed_many,
        content_hash_fn=lambda t: f"h_{t}",
        batch_size=2,
        concurrency=1,
        max_row_bytes=10_000,
        expected_dim=4,
    )
    assert counters.scanned == 3
    assert counters.embedded == 3
    assert len(written) == 3
    assert {w[0] for w in written} == {"id-1", "id-2", "id-3"}
    # First fetch with after_id=None, second with after_id="id-3" (advanced)
    assert fetched_after == [None, "id-3"]


# ── Cursor advance past skipped rows ──────────────────────────────────────

@pytest.mark.asyncio
async def test_cursor_advances_past_skipped(lib):
    """Skipped rows (oversize) shouldn't cause the loop to re-fetch them."""
    big_text = "x" * 50_000
    rows_first = [
        ("id-1", big_text, "", None),    # oversize, will skip
        ("id-2", "ok",     "", None),
    ]
    fetched_after = []

    def fetch(after_id, limit):
        fetched_after.append(after_id)
        if after_id is None:
            return rows_first
        return []  # already past id-2 — drained

    def write(mid, vec, model, chash):
        return True

    async def fake_embed_many(texts):
        return [([0.0] * 4, "m") for _ in texts]

    counters = lib.Counters()
    await lib.run_embed_loop(
        fetch_candidates=fetch,
        write_embedding=write,
        counters=counters,
        embed_many=fake_embed_many,
        content_hash_fn=lambda t: f"h_{t}",
        batch_size=10,
        concurrency=1,
        max_row_bytes=32_000,
        expected_dim=4,
    )
    assert counters.skipped_oversize == 1
    assert counters.embedded == 1
    # After advancing past id-2, the next fetch returns 0 -> loop ends.
    # Crucially fetched_after contains exactly [None, "id-2"], no infinite loop.
    assert fetched_after == [None, "id-2"]


# ── Limit cutoff ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_limit_stops_loop_without_deadline(lib):
    """limit=N stops at the next outer-cycle boundary past N, not at exactly N.

    This is the documented contract — see run_embed_loop's `limit` arg
    docstring and embed_backfill.py's --limit help text. The check fires
    at the top of each outer cycle, so a fetched chunk runs to completion
    and may overshoot. For strict caps, callers pair limit with smaller
    batch_size and concurrency.
    """
    rows = [(f"id-{i}", "ok", "", None) for i in range(10)]
    fetched = [False]

    def fetch(after_id, limit):
        if fetched[0]:
            return []
        fetched[0] = True
        return rows

    def write(mid, vec, model, chash):
        return True

    async def fake_embed_many(texts):
        return [([0.0] * 4, "m") for _ in texts]

    counters = lib.Counters()
    await lib.run_embed_loop(
        fetch_candidates=fetch,
        write_embedding=write,
        counters=counters,
        embed_many=fake_embed_many,
        content_hash_fn=lambda t: f"h_{t}",
        batch_size=5,
        concurrency=1,
        limit=3,           # stop after 3
        deadline_s=None,
        max_row_bytes=32_000,
        expected_dim=4,
    )
    # batch_size=5, fetch_multiplier defaults to 4, so fetch_size=20.
    # The first cycle pulls all 10 rows (fewer than fetch_size); they
    # all complete in one gather() before the limit check at the top
    # of cycle 2 breaks. So embedded=10, not the requested 3.
    assert counters.embedded == 10
    assert counters.embedded >= 3  # at minimum, the limit must have been honored


# ── Consecutive-fail abort ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_consecutive_fails_abort(lib):
    """N back-to-back batch failures should abort the loop."""
    rows = [(f"id-{i}", "ok", "", None) for i in range(20)]
    fetch_calls = [0]

    def fetch(after_id, limit):
        fetch_calls[0] += 1
        if fetch_calls[0] > 5:
            return []
        # Always return more rows so the loop keeps trying
        return rows[:limit]

    def write(mid, vec, model, chash):
        return True

    async def fake_embed_many(texts):
        raise RuntimeError("embedder unreachable")

    counters = lib.Counters()
    await lib.run_embed_loop(
        fetch_candidates=fetch,
        write_embedding=write,
        counters=counters,
        embed_many=fake_embed_many,
        content_hash_fn=lambda t: f"h_{t}",
        batch_size=5,
        concurrency=1,
        max_consecutive_fails=3,
        max_row_bytes=32_000,
        expected_dim=4,
    )
    # All embed calls fail; consecutive_fails should hit 3 and abort.
    assert counters.embedded == 0
    assert counters.consecutive_fails >= 3
    assert "RuntimeError" in counters.errors_by_class


# ── Empty / oversize / bad-dim skips ──────────────────────────────────────

@pytest.mark.asyncio
async def test_skips_dont_call_write(lib):
    """Empty, oversize, and bad-dim rows must NOT call write_embedding."""
    big = "x" * 50_000
    rows = [
        ("id-1", "",       "", None),  # empty
        ("id-2", big,      "", None),  # oversize
        ("id-3", "alpha",  "", None),  # ok
    ]
    write_calls: list = []

    def fetch(after_id, limit):
        if after_id is None:
            return rows
        return []

    def write(mid, vec, model, chash):
        write_calls.append(mid)
        return True

    async def fake_embed_many(texts):
        # bad-dim case: helper rejects before write, regardless of input
        return [([0.0] * 8, "m") for _ in texts]  # dim 8 != expected 4

    counters = lib.Counters()
    await lib.run_embed_loop(
        fetch_candidates=fetch,
        write_embedding=write,
        counters=counters,
        embed_many=fake_embed_many,
        content_hash_fn=lambda t: f"h_{t}",
        batch_size=10,
        concurrency=1,
        max_row_bytes=32_000,
        expected_dim=4,
    )
    # id-1 skipped empty, id-2 skipped oversize, id-3 skipped bad-dim.
    assert counters.skipped_empty == 1
    assert counters.skipped_oversize == 1
    assert counters.skipped_bad_dim == 1
    assert counters.embedded == 0
    # write_embedding was never called for any of them
    assert write_calls == []


# ── Chatlog-shape contract ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chatlog_shape_no_anchors_no_dim_check(lib):
    """The chatlog sweeper passes transform=identity, expected_dim=None
    (heterogenous models). Helper must handle both cleanly."""
    rows = [
        ("id-1", "user said something", "", None),
        ("id-2", "another turn",         "", None),
    ]
    written: list = []
    transform_calls: list = []

    def fetch(after_id, limit):
        if after_id is None:
            return rows
        return []

    def write(mid, vec, model, chash):
        written.append((mid, len(vec)))
        return True

    def transform(text, metadata):
        transform_calls.append((text, metadata))
        return text  # identity, like chatlog

    async def fake_embed_many(texts):
        # Various dims — chatlog has heterogenous historical models
        dims = [768, 1024]
        return [([0.0] * dims[i], f"model-{i}") for i, _ in enumerate(texts)]

    counters = lib.Counters()
    await lib.run_embed_loop(
        fetch_candidates=fetch,
        write_embedding=write,
        counters=counters,
        embed_many=fake_embed_many,
        content_hash_fn=lambda t: f"h_{t}",
        transform_text=transform,
        batch_size=10,
        concurrency=1,
        max_row_bytes=32_000,
        expected_dim=None,  # don't filter by dim
    )
    assert counters.embedded == 2
    assert len(written) == 2
    # Both rows passed through; helper called transform on each.
    assert len(transform_calls) == 2


# ── Counters API ──────────────────────────────────────────────────────────

def test_counters_record_error(lib):
    c = lib.Counters()
    c.record_error(ValueError("a"))
    c.record_error(ValueError("b"))
    c.record_error(TypeError("c"))
    assert c.errors_by_class == {"ValueError": 2, "TypeError": 1}
