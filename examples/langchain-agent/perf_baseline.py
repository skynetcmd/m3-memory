"""Perf baseline for the m3 LangChain surface (§8 budgets, §5 effectiveness gate).

Measures read / write / list / bulk latency of ``m3_memory.langchain.Memory``
against a real (tmp) DB and prints P50/P95/P99. The committed reference numbers
live in ``PERF_BASELINE.md`` next to this file; this script REGENERATES them so a
regression is visible (run it, diff the output against the doc).

Budgets (§8): read P50<5ms / P95<20ms / P99<50ms; a 1000-item batch <2min.

Run:  pip install "m3-memory[langchain]"  &&  python perf_baseline.py
The absolute numbers are hardware-dependent; what's asserted is the BUDGET, not
an exact match — a machine slower than the reference still passes if it's within
the budget ceilings.
"""

from __future__ import annotations

import os
import statistics
import sys
import tempfile
import time

# Allow a repo-checkout run (no editable install): add the repo root so
# `import m3_memory` resolves. Harmless when m3-memory is pip-installed.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Route to a throwaway DB so a baseline run never touches real data.
_TMP = os.path.join(tempfile.gettempdir(), "m3_langchain_perf_baseline.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ.setdefault("M3_DATABASE", _TMP)
os.environ.setdefault("M3_CHATLOG_DB_PATH", _TMP)

from m3_memory.langchain import Memory  # noqa: E402

# Budget ceilings (§8). Read budgets are asserted; write is reported (it does
# inline embedding + contradiction detection, a different cost class).
READ_P50_MS, READ_P95_MS, READ_P99_MS = 5.0, 20.0, 50.0
BULK_ITEMS, BULK_CEILING_S = 1000, 120.0


def _pct(xs: list[float], p: int) -> float:
    if len(xs) < 2:
        return xs[0] if xs else 0.0
    return statistics.quantiles(xs, n=100)[p - 1]


def _timed(fn, n: int) -> list[float]:
    out = []
    for i in range(n):
        t = time.perf_counter()
        fn(i)
        out.append((time.perf_counter() - t) * 1000.0)
    return out


def main() -> int:
    m = Memory(user_id="perf")
    # Warm up: first call migrates the DB + loads the embedder (excluded).
    m.add("warmup fact")
    m.get_all()

    write = _timed(lambda i: m.add(f"fact number {i} about topic {i % 7}"), 50)
    read = _timed(lambda i: m.search(f"topic {i % 7}", limit=5), 50)
    listing = _timed(lambda i: m.get_all(limit=50), 30)

    print(f"WRITE  ms  P50={statistics.median(write):6.2f}  "
          f"P95={_pct(write, 95):6.2f}  P99={_pct(write, 99):6.2f}")
    print(f"READ   ms  P50={statistics.median(read):6.2f}  "
          f"P95={_pct(read, 95):6.2f}  P99={_pct(read, 99):6.2f}")
    print(f"LIST   ms  P50={statistics.median(listing):6.2f}  "
          f"P95={_pct(listing, 95):6.2f}")

    # Bulk throughput — one coalesced write of BULK_ITEMS.
    items = [f"bulk item {i} content text here" for i in range(BULK_ITEMS)]
    t = time.perf_counter()
    m.add(items)
    bulk_s = time.perf_counter() - t
    print(f"BULK   {BULK_ITEMS}-add: {bulk_s * 1000:.0f}ms total, "
          f"{bulk_s / BULK_ITEMS * 1000:.2f}ms/item")

    # Assert the read budget + bulk ceiling (the effectiveness gate).
    ok = True
    checks = [
        ("read P50", statistics.median(read), READ_P50_MS),
        ("read P95", _pct(read, 95), READ_P95_MS),
        ("read P99", _pct(read, 99), READ_P99_MS),
        (f"bulk {BULK_ITEMS}", bulk_s, BULK_CEILING_S),
    ]
    print()
    for name, got, ceiling in checks:
        passed = got <= ceiling
        ok = ok and passed
        unit = "s" if name.startswith("bulk") else "ms"
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}: {got:.2f}{unit} "
              f"<= {ceiling}{unit}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
