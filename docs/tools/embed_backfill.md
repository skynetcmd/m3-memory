---
tool: bin/embed_backfill.py
sha1: 819e45e5f76f
mtime_utc: 2026-05-04T22:04:47.597993+00:00
generated_utc: 2026-05-04T22:24:29.150267+00:00
private: false
---

# bin/embed_backfill.py

## Purpose

embed_backfill.py — fill in missing embeddings for memory_items rows.

Companion to the M3_OBSERVER_NO_EMBED=1 ingest pattern. When ingest writes
rows without embedding (decoupling write throughput from embedder
throughput), this sweeper scans the DB for rows with no entry in
memory_embeddings and embeds them in batches.

Works on any m3-memory DB — the core memory store, a bench workspace,
a future fresh-ingestion DB, anywhere. Filter by --variant / --type /
--user-id / --scope / --id-prefix / --max-age-days to narrow scope.

Resumable by construction: the WHERE NOT EXISTS query IS the resume
marker. Crash mid-run, re-launch, picks up exactly where it left off.

Cost-free at the embedder side (uses local LLM_ENDPOINTS_CSV /
:8081 / LM Studio routing — no API charges).

Usage:

    # Sweep core memory (default DB) — embeds anything missing
    python bin/embed_backfill.py

    # Alternate workspace, only one variant
    python bin/embed_backfill.py \
        --db memory/other.db \
        --variant my-variant-name

    # Smoke test: 100 rows, dry-run
    python bin/embed_backfill.py --limit 100 --dry-run

    # Sharded sweepers (run multiple instances on disjoint id prefixes)
    python bin/embed_backfill.py --id-prefix 0 --lockfile /tmp/sweep0.lock &
    python bin/embed_backfill.py --id-prefix 1 --lockfile /tmp/sweep1.lock &

Hardening:
  - Per-batch timeout (--timeout-s)
  - Hard runtime cap (--max-runtime-min)
  - Auto-abort after N consecutive batch failures (--max-consecutive-fails)
  - Dim validation (--expected-dim) — won't write malformed embeddings
  - Per-row size cap (--max-row-bytes) — skips oversize content
  - Optional lockfile to prevent two sweepers racing on the same DB

This script is read-mostly + small bulk writes; safe to run alongside
an active enricher in WAL mode (SQLite handles concurrent reads fine).

---

## Entry points

- `def main()` (line 453)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--db` | f'Target DB. Default: $M3_DATABASE or {DEFAULT_DB}' | `Path(os.environ.get('M3_DATABASE', str(DEFAULT_DB)))` |  | Path |  |
| `--variant` | Filter to one variant. Repeatable for OR. | `[]` |  | append |  |
| `--type` | Filter to one memory type. Repeatable for OR. | `[]` |  | append |  |
| `--user-id` | Filter to one user_id. | None |  | str |  |
| `--scope` | Filter to one scope (user/session/agent/org). | None |  | str |  |
| `--id-prefix` | Backfill only rows whose id starts with this hex prefix. Use to shard across multiple sweeper instances. | None |  | str |  |
| `--max-age-days` | Only rows older than N days. Useful when you want to leave fresh writes alone for a window first. | None |  | int |  |
| `--limit` | f"Stop after AT LEAST N successful embeds. The check fires at outer-cycle boundaries, so the actual stop point can overshoot by up to one cycle's fetch (batch_size * concurrency * 4 = {DEFAULT_BATCH_SIZE * DEFAULT_CONCURRENCY * 4} rows at defaults). Used for smoke testing — for strict row caps, also lower --batch-size and --concurrency." | None |  | int |  |
| `--batch-size` | f'Rows per embed call. Default: {DEFAULT_BATCH_SIZE}.' | `DEFAULT_BATCH_SIZE` |  | int |  |
| `--concurrency` | f"Concurrent batches in flight. Default: {DEFAULT_CONCURRENCY}. Cap by your llama-server's --parallel slots." | `DEFAULT_CONCURRENCY` |  | int |  |
| `--connection-refresh` | f'Batches between connection-pool recycle. Default: {DEFAULT_CONN_REFRESH_BATCHES}.' | `DEFAULT_CONN_REFRESH_BATCHES` |  | int |  |
| `--timeout-s` | f'Per-batch embed call timeout. Default: {DEFAULT_TIMEOUT_S}s.' | `DEFAULT_TIMEOUT_S` |  | float |  |
| `--max-runtime-min` | f'Hard kill at N min wall-clock. Default: {DEFAULT_MAX_RUNTIME_MIN}.' | `DEFAULT_MAX_RUNTIME_MIN` |  | int |  |
| `--max-consecutive-fails` | f'Abort after N back-to-back batch fails. Default: {DEFAULT_MAX_CONSEC_FAILS}.' | `DEFAULT_MAX_CONSEC_FAILS` |  | int |  |
| `--max-row-bytes` | f'Skip rows whose content > N bytes. Default: {DEFAULT_MAX_ROW_BYTES} (bge-m3 ctx limit).' | `DEFAULT_MAX_ROW_BYTES` |  | int |  |
| `--expected-dim` | f'Skip embeddings whose dim != N. Default: {DEFAULT_EXPECTED_DIM}. Pass 0 to disable.' | `DEFAULT_EXPECTED_DIM` |  | int |  |
| `--lockfile` | Refuse to start if this file exists; create it on start, delete on clean exit. Use for cron / scheduled sweepers. | None |  | Path |  |
| `--no-augment-anchors` | Skip _augment_embed_text_with_anchors before embed. Default OFF — anchors match memory_write_impl behavior. | `False` |  | store_true |  |
| `--dry-run` | Print plan and counts; don't embed or write. | `False` |  | store_true |  |

---

## Environment variables read

- `M3_DATABASE`

---

## Calls INTO this repo (intra-repo imports)

- `embed_sweep_lib (Counters)`
- `embed_sweep_lib (run_embed_loop)`
- `memory_core`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `str(args.db)`` (line 265)
- `sqlite3.connect()  → `str(db_path)`` (line 128)
- `sqlite3.connect()  → `str(db_path)`` (line 233)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `agent_memory.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
