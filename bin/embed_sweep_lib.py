"""embed_sweep_lib — shared embed-loop helper for sweeper-style backfill tools.

Two callers consume this:

  bin/embed_backfill.py       — general-purpose, any DB, any variant
  bin/chatlog_embed_sweeper.py — chatlog-specific (type='chat_log'), with
                                 spill-drain + state-file bookkeeping

Both have the same inner shape:

  while not done:
      candidates = fetch_some_unembedded_rows()
      vectors    = await _embed_many(...)
      write_embeddings(...)
      advance the resume cursor

The differences live in *what* they fetch, *what* they write alongside the
embedding row, and *how* they pre-process text before embedding. We expose
those as callbacks so each caller stays in control of its own semantics
while the loop machinery (concurrency, batching, cursor advance, timeouts,
hardening) lives in one place.

History: extracted from bin/embed_backfill.py 2026-05-03 when the chatlog
sweeper migrated to share this helper. See commit message for rationale.
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional


# ── Counters used by both callers ─────────────────────────────────────────
class Counters:
    """Shared counters. Both tools instantiate one and read totals at the end."""

    def __init__(self) -> None:
        self.scanned = 0
        self.embedded = 0
        self.skipped_empty = 0
        self.skipped_oversize = 0
        self.skipped_bad_dim = 0
        self.failed_batches = 0
        self.consecutive_fails = 0
        self.batches_completed = 0
        self.errors_by_class: dict[str, int] = {}

    def record_error(self, exc: Exception) -> None:
        cls = type(exc).__name__
        self.errors_by_class[cls] = self.errors_by_class.get(cls, 0) + 1


# ── Type aliases for the callbacks ────────────────────────────────────────
# fetch_candidates(after_id, limit) -> list of rows. Each row is a 4-tuple:
#   (memory_id, content, title, metadata_json_or_None).
# When after_id is None, returns the first batch sorted by id ASC.
# When limit is reached or no more candidates remain, returns [].
FetchFn = Callable[[Optional[str], int], list[tuple[str, str, str, Optional[str]]]]

# write_embedding(memory_id, vec, model_str, content_hash) -> True if a row
# was newly written, False if INSERT OR IGNORE skipped (race with another
# writer) or any other reason it didn't land. Counters use this to track
# real progress.
WriteFn = Callable[[str, list[float], str, str], bool]

# transform_text(content, metadata_json_or_None) -> str. Lets each caller
# inject pre-embed text augmentation (e.g. _augment_embed_text_with_anchors
# for the general backfill, identity for chatlog).
TransformFn = Callable[[str, Optional[str]], str]

# log(message) — caller's logging hook. Helper writes status lines through
# this so the chatlog tool (logger.info) and the general tool (custom
# stdout writer) keep their own conventions.
LogFn = Callable[[str], None]


def _identity_transform(text: str, _metadata: Optional[str]) -> str:
    return text


# ── The embed loop ────────────────────────────────────────────────────────
async def run_embed_loop(
    *,
    fetch_candidates: FetchFn,
    write_embedding: WriteFn,
    counters: Counters,
    embed_many: Callable[[list[str]], Awaitable[list[tuple[Optional[list[float]], str]]]],
    content_hash_fn: Callable[[str], str],
    transform_text: TransformFn = _identity_transform,
    batch_size: int = 256,
    concurrency: int = 4,
    fetch_multiplier: int = 4,
    timeout_s: float = 60.0,
    deadline_s: Optional[float] = None,
    max_consecutive_fails: int = 5,
    max_row_bytes: int = 32_768,
    expected_dim: Optional[int] = 1024,
    limit: Optional[int] = None,
    log: Optional[LogFn] = None,
) -> None:
    """Drive the embed loop until exhaustion or a stop condition fires.

    Args:
      fetch_candidates: caller-provided. Pulls (after_id, limit) -> rows.
      write_embedding:  caller-provided. Persists one embedding row.
      counters:         shared counters object both caller and helper update.
      embed_many:       memory_core._embed_many (or test double).
      content_hash_fn:  memory_core._content_hash (or test double).
      transform_text:   pre-embed text augmenter; defaults to identity.
      batch_size:       rows per /embeddings call.
      concurrency:      concurrent batches in flight.
      fetch_multiplier: per cycle, fetch batch_size * concurrency * this many
                        rows. Higher = fewer fetch round-trips, more memory.
      timeout_s:        per-batch embed timeout.
      deadline_s:       absolute monotonic time after which the loop stops.
                        None = no deadline. Caller computes (start + budget).
      max_consecutive_fails: abort after this many back-to-back batch fails.
      max_row_bytes:    skip rows whose post-transform text > this size.
      expected_dim:     skip embeddings whose dim != this. None = don't check.
      limit:            stop after AT LEAST this many successful embeds.
                        Checked at outer-cycle boundaries (NOT per-batch),
                        so the actual stop point can overshoot by up to one
                        full cycle's fetch_size = batch_size * concurrency *
                        fetch_multiplier rows. At defaults (256 * 4 * 4 = 4096),
                        a `limit=100` smoke test will embed up to ~4096 rows
                        before stopping. Mental model: "don't START a new
                        cycle past the limit" — not "stop at exactly N." For
                        strict row caps, callers should pair limit with
                        smaller batch_size and concurrency. None = unlimited.
      log:              optional status-line writer. Helper uses for CYCLE /
                        DEADLINE / DRAIN / ABORT lines. If None, no logging.

    Cursor advance: this loop tracks an `after_id` high-water mark and passes
    it back to fetch_candidates each cycle. Skipped rows (oversize, bad-dim,
    failed batch) still satisfy the caller's NOT EXISTS predicate, so without
    forward progress on id we'd reselect them forever. Tracking the highest
    id seen each cycle makes the sweep monotonic.

    Resilience: the loop never raises on a single batch failure. It logs,
    increments counters, and continues. Only consecutive-failure thresholds
    (embedder unreachable) cause an early abort.
    """
    sem = asyncio.Semaphore(concurrency)
    fetch_size = batch_size * concurrency * fetch_multiplier
    cycles = 0
    after_id: Optional[str] = None

    async def _embed_one_batch(batch_rows: list[tuple[str, str, str, Optional[str]]]) -> None:
        async with sem:
            if deadline_s is not None and time.monotonic() > deadline_s:
                return  # caller will see deadline at next outer-loop iter

            # Build text + content_hash for each row, applying skip rules
            items: list[dict] = []
            for r in batch_rows:
                mid, content, title, metadata_json = r
                base_text = (content or title or "").strip()
                if not base_text:
                    counters.skipped_empty += 1
                    continue
                embed_text = transform_text(base_text, metadata_json)
                if len(embed_text.encode("utf-8")) > max_row_bytes:
                    counters.skipped_oversize += 1
                    continue
                items.append({
                    "mid": mid,
                    "text": embed_text,
                    "chash": content_hash_fn(embed_text),
                })

            if not items:
                return

            try:
                texts = [it["text"] for it in items]
                results = await asyncio.wait_for(
                    embed_many(texts), timeout=timeout_s,
                )
            except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
                counters.failed_batches += 1
                counters.consecutive_fails += 1
                counters.record_error(e)
                if log is not None:
                    log(f"BATCH_FAIL: {type(e).__name__}: {str(e)[:200]}")
                return

            n_written = 0
            for it, (vec, model_str) in zip(items, results):
                if vec is None:
                    continue
                if expected_dim is not None and len(vec) != expected_dim:
                    counters.skipped_bad_dim += 1
                    continue
                try:
                    if write_embedding(it["mid"], vec, model_str, it["chash"]):
                        n_written += 1
                except Exception as e:  # noqa: BLE001
                    counters.record_error(e)
                    if log is not None:
                        log(f"WRITE_FAIL: mid={it['mid'][:8]} {type(e).__name__}: {e}")

            counters.embedded += n_written
            counters.batches_completed += 1
            counters.consecutive_fails = 0

    # Outer cycle
    while True:
        if deadline_s is not None and time.monotonic() > deadline_s:
            if log is not None:
                log("DEADLINE: max-runtime reached.")
            break

        if counters.consecutive_fails >= max_consecutive_fails:
            if log is not None:
                log(f"ABORT: {counters.consecutive_fails} consecutive batch failures. "
                    f"Check embedder availability.")
            break

        if limit is not None and counters.embedded >= limit:
            if log is not None:
                log(f"LIMIT_REACHED: {limit}")
            break

        rows = fetch_candidates(after_id, fetch_size)
        counters.scanned += len(rows)

        if not rows:
            if log is not None:
                log("DRAIN: 0 rows pending.")
            break

        # Advance high-water mark BEFORE dispatching: even if every row in
        # this fetch gets skipped, the next fetch queries strictly past
        # these ids. The fetch contract is that rows[-1] holds the largest
        # id (i.e. caller ORDERs BY id ASC).
        after_id = rows[-1][0]

        batches: list[list[tuple[str, str, str, Optional[str]]]] = [
            rows[i:i + batch_size] for i in range(0, len(rows), batch_size)
        ]
        await asyncio.gather(
            *(_embed_one_batch(b) for b in batches), return_exceptions=False,
        )
        cycles += 1

        if log is not None:
            log(
                f"CYCLE {cycles}: scanned={counters.scanned} "
                f"embedded={counters.embedded} "
                f"skipped_empty={counters.skipped_empty} "
                f"skipped_oversize={counters.skipped_oversize} "
                f"skipped_bad_dim={counters.skipped_bad_dim} "
                f"failed_batches={counters.failed_batches}"
            )
