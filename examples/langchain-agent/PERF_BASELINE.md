# m3 LangChain surface — performance baseline

**Reads land in under a millisecond** (P50 ~0.85 ms) — ~6× under budget, and no
network hop because m3 runs in-process. Writing 1,000 memories takes ~1.5 s, ~75×
under the ceiling. The numbers below are the committed reference; regenerate them
with [`perf_baseline.py`](perf_baseline.py) and a regression shows as a budget
FAIL or a materially worse percentile.

## Budgets (§8 DESIGN_PHILOSOPHIES)

| Metric | Budget | Asserted by the script |
|---|---|---|
| Read (search) P50 | < 5 ms | ✅ |
| Read (search) P95 | < 20 ms | ✅ |
| Read (search) P99 | < 50 ms | ✅ |
| Bulk 1000-item write | < 120 s | ✅ |

Write latency (single `.add`) is **reported, not budget-asserted** — a single
write does inline embedding + contradiction detection, a different cost class
than read. Batch writes amortize the embedding and are the fast path.

## Reference run (2026-07-14)

Local dev machine (Windows, in-process native embedder). Absolute numbers are
hardware-dependent; the **budgets** are what's enforced — a slower machine still
passes if it's under the ceilings.

| Operation | P50 | P95 | P99 |
|---|---|---|---|
| **Read** (`search`, k=5) | ~0.85 ms | ~1.3 ms | ~1.9 ms |
| **List** (`get_all`, deterministic) | ~0.3 ms | ~0.4 ms | — |
| **Write** (single `add`) | ~16 ms | ~40 ms | ~48 ms |
| **Bulk** (1000-item `add`) | ~1.3–1.6 s total (~1.5 ms/item) | | |

Reads land ~6× under the P50 budget and ~25× under P99; the 1000-item bulk write
is ~75× under its ceiling. The single-write cost is dominated by inline embedding
+ contradiction detection — use a list `.add([...])` (one coalesced
`memory_write_bulk`) when writing many at once.

## Why these hold
- **In-process, no HTTP/proxy hop** — the adapter calls m3 impls directly on one
  persistent event-loop thread (pool + embedder affinity preserved, §8).
- **Native tier-1 embedder** on the write path (no network round-trip).
- **Deterministic listing** (`get_all`) reads rows directly, independent of
  embedding state — sub-millisecond and never blocks on vector backfill.
