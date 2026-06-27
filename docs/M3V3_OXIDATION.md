# M3-v3: Governor, sqlite-vec, and Ingestion Oxidation

This document explains a set of Milestone-4/5 changes to m3-memory and what they
mean for how the system behaves. Three are user-visible capabilities (an adaptive
background **governor**, native **sqlite-vec** vector search, and **parallel
ingestion hashing**); one is an instructive engineering decision (a write-queue
prototype that was measured and **reverted**). Each native fast path has a
pure-Python fallback and is gated by `M3_CORE_RS_DISABLE`, so a missing or old
Rust wheel only makes things slower — never broken.

---

## 1. The Adaptive Background Workload Governor

m3-memory does real background work: de-duplication, PostgreSQL sync, embedding
backfill, chatlog embedding sweeps, files watching, and cognitive-maintenance
loops. The governor decides **how aggressively** that work is allowed to run
right now, based on two live signals:

- **Host load** — the max of CPU / RAM / GPU utilization.
- **Time since the last user interaction** — so background work yields while you
  are actively using the machine and ramps back up once you step away.

It maps those to a pacing mode:

| Mode | When | Effect on background work |
|---|---|---|
| `HALTED` | load ≥ limit threshold, **or** a user interaction in the last 30 s | stop / hold |
| `THROTTLED` | load ≥ initial threshold (but below limit) | run with a long (~10 s) delay between units |
| `TAPERED` | idle 30–60 s | run with a short (~5 s) delay |
| `CONTINUOUS` | idle ≥ 60 s | run freely (~0.1 s spacing) |

Thresholds are user-selectable via `M3_GOVERNOR_INITIAL_THRESHOLD` (default 85)
and `M3_GOVERNOR_LIMIT_THRESHOLD` (default 95). See
[`ENVIRONMENT_VARIABLES.md`](ENVIRONMENT_VARIABLES.md).

### Why a lightweight, non-blocking governor beats cron / nightly / scheduled jobs

The conventional way to run maintenance is a **schedule**: a cron entry, a
nightly job, a "run dedup at 3 AM" timer. The governor is a deliberately
different model, and for a local-first memory system it is strictly better:

- **Schedules are blind to load; the governor is load-aware.** A 3 AM cron job
  fires whether or not you happen to be compiling, gaming, or running a benchmark
  at 3 AM. The governor *never* competes with you for CPU/GPU — it sees the load
  spike and drops to `HALTED`/`THROTTLED` on its own. There is no "the nightly
  job tanked my machine" failure mode.

- **Schedules batch work into a spike; the governor spreads it.** A nightly job
  wakes up and does *everything* at once — a thundering herd of embedding and
  sync work that saturates the disk and GPU for a burst. The governor does the
  same total work as a smooth trickle during idle time, so the system stays
  responsive and the WAL/checkpoint pressure stays bounded (see §10 of
  `DESIGN_PHILOSOPHIES.md` on WAL discipline).

- **Schedules drift and miss; the governor is always current.** If the machine
  is asleep at 3 AM, a cron job is simply skipped and the backlog grows until the
  next window. The governor has no window to miss — it makes a fresh decision
  every time a background loop checks in, so work happens whenever the machine is
  *actually* idle, not whenever the clock says it should be.

- **Schedules need an external scheduler; the governor is in-process.** No
  systemd timer, no Windows Task Scheduler entry, no cron daemon to install,
  keep running, and keep in sync across OSes. The governor ships inside the
  process that does the work. This matters for a tool that must run identically
  on a laptop and inside an air-gapped enclave (`DESIGN_PHILOSOPHIES.md` §1).

- **It is cooperative and non-blocking.** The governor never *preempts* work —
  it tells the next unit of background work to wait (or skip) before it starts.
  An interactive request registers an interaction and the next pacing decision
  immediately backs the background work off. There is no lock held across the
  decision; it is a pure function of `(load, elapsed)`.

In short: **a scheduler asks "what time is it?"; the governor asks "is now a good
time?"** For background maintenance on a machine a human is also using, the
second question is the right one.

### Implementation

The pacing ladder is implemented once, in Rust, as the source of truth
(`m3-governor` crate, exposed as `m3_core_rs.Governor`), and `bin/m3_sdk.py`'s
`get_governor_pacing` calls it with a transparent fall-through to the identical
pure-Python ladder when the native extension is unavailable. The two are verified
to return byte-identical pacing dicts across the full truth table. Oxidizing it
is for single-source-of-truth and future Rust daemons — it is **not** a speed
optimization (the governor runs once per decision, not per row).

---

## 2. Native vector search via `sqlite-vec`

m3-memory's semantic search already detects and uses the
[`sqlite-vec`](https://github.com/asg017/sqlite-vec) extension when it is present
(`bin/sqlite_pragmas.py` loads it; `bin/memory/search.py` has a `vec0` query
path), falling back to the Python/NumPy cosine path when it is absent. The M3-v3
change makes this a **declared, installable capability**:

```
pip install "m3-memory[vector]"
```

The `vector` extra pulls the `sqlite-vec` PyPI wheel, which ships the
platform-native extension binary — so there is nothing to compile or bundle. With
it installed, vector similarity runs inside SQLite (native `vec0` functions)
instead of being post-processed in Python, which keeps large candidate sets in
the database engine rather than marshalling rows out to Python (`DESIGN_PHILOSOPHIES.md`
§4: no Python-side aggregation of large result sets).

It remains **optional by design**: core m3-memory runs fully without it, and the
Python cosine path is the graceful fallback.

---

## 3. Parallel ingestion hashing

When the files-memory subsystem reviews a corpus for staleness, it must decide
whether each file changed since it was last ingested. The fast check is mtime;
when mtime moved, it confirms with a content SHA-256. On a large corpus that is a
lot of file reads and hashes.

The change adds `file_content_sha256_batch(paths)` (`bin/files_memory/identity.py`),
which routes the whole batch through the native `m3_core_rs.hash_files` — a
`rayon`-parallel read + hash with the GIL released — and falls back to a per-file
Python loop when the extension is unavailable. The digests are byte-identical to
the existing single-file `file_content_sha256`. The staleness review
(`files_staleness_review`) now collects every mtime-changed candidate in a
pre-pass and hashes them in one batch instead of serially inside the
classification loop. Behavior is identical; the hashing is parallel.

**Measured:** native batch hashing is **6.5–7× faster** than the serial Python
loop on 500–1000 files (see [`OXIDATION_BENCHMARKS.md`](OXIDATION_BENCHMARKS.md)
and m3-core-rs `docs/BENCHMARKS.md`). The single-file path stays Python on
purpose — for one small hash, `hashlib` (already C) beats paying the FFI
crossing. The directory walk itself (`m3_core_rs.fs_walk`) is also available and
output-parity-verified against `os.walk`; its benefit is syscall-bound, so it is
about removing per-entry Python overhead on very large trees rather than a fixed
multiplier.

---

## 4. The write-queue that was measured and reverted

A `WriteQueueDaemon` was prototyped to "scale concurrent write performance" by
coalescing individual `memory_write` calls into batched commits. It was
**reverted** after benchmarking — and the reason is worth keeping, because it is
a clean example of `DESIGN_PHILOSOPHIES.md` §5 (a pre-registered threshold, and
not shipping a feature the benchmark contradicts).

The short version: an **in-process** queue serializes only the writes inside one
Python process, but the `database is locked` contention it was meant to fix is a
**multi-process** phenomenon (the MCP server, a CLI, and a migration sweeper each
on their own connection). An in-process queue cannot coordinate separate
processes, so it never reaches the slow case — and for the intra-process case it
*can* touch, SQLite WAL on the single pooled connection already commits hundreds
of rows in milliseconds, so the queue's aggregation window only added latency.

What actually works for the real (multi-process) contention is already in
m3-memory: `PRAGMA busy_timeout=30000` turns lock errors into polite waits, and
the existing `memory_write_bulk_impl` / `memory_write_batch_impl` batch their
commits (~50× faster than per-row under contention in the benchmark). So the
genuinely useful path for bulk ingest is **the bulk-write APIs**, not a queue.
Full benchmark table and analysis:
[`../v3/m3_v3_phase_c_rust_oxidation_plan.md`](../v3/m3_v3_phase_c_rust_oxidation_plan.md).

---

## Fallback & kill-switch

Every native path above keeps its pure-Python implementation as the fallback,
gated by a single env var:

```
M3_CORE_RS_DISABLE=1    # force the pure-Python path for every oxidized operation
```

A missing, stale, or disabled Rust wheel makes m3-memory slower, never broken.
See [`ENVIRONMENT_VARIABLES.md`](ENVIRONMENT_VARIABLES.md) for the full list of
governor and oxidation env vars, and `m3 doctor` (`oxidation_probe`) to check
which native paths are live in your installed wheel.
