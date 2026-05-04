---
tool: bin/m3_enrich_batch.py
sha1: 68852877970a
mtime_utc: 2026-05-04T21:54:28.136904+00:00
generated_utc: 2026-05-04T22:24:29.267634+00:00
private: false
---

# bin/m3_enrich_batch.py

## Purpose

m3-enrich-batch — async/batch variant of bin/m3_enrich.py.

Submits all eligible conversations as ONE batch via the provider's batch
API (currently Anthropic /v1/messages/batches), waits for completion,
then ingests results into memory_items + enrichment_groups using the
same state-machine discipline as the live worker.

Why: ~50% off list pricing in exchange for async wallclock (typically
5-60 minutes for the batch to complete on Anthropic).

Limitations vs the live m3_enrich.py:
  - Async: each slice submits, polls, ingests, then the next slice
    submits. Auto-splits via runner.max_batch_size when the request
    list exceeds the provider's per-batch ceiling.
  - Backends supported: anthropic (native /v1/messages/batches),
    openai-shim Gemini Developer API (/v1beta/models/<m>:batchGenerateContent).
    Other openai-shim providers (real OpenAI, xAI) raise
    NotImplementedError until their batch runner is added.
  - Crash recovery: batch_ids are persisted to enrichment_runs.notes
    under a structured "batches" array. A re-launch with
    --resume-run <enrichment_runs.id> picks up any batches that haven't
    been ingested yet, polls them, and ingests.

Usage:
  python bin/m3_enrich_batch.py \
      --profile enrich_anthropic_haiku \
      --core --core-db memory/your-corpus.db \
      --source-variant your-source-variant \
      --target-variant your-target-variant \
      --source-conv-list .scratch/some_convolist.txt \
      --track-state --resume \
      --skip-preflight --yes

Or to resume polling/ingesting a previously-submitted run:
  python bin/m3_enrich_batch.py \
      --profile enrich_anthropic_haiku \
      --core-db memory/your-corpus.db \
      --resume-run <enrichment_runs.id>

Status:  Phase E worker. Pairs with batch_runner.py (provider abstraction).

---

## Entry points

- `def main()` (line 967)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--profile` | profile name from config/slm/<name>.yaml. Supported backends: anthropic, openai (Gemini OAI shim only). | — |  | str |  |
| `--profile-path` | Override profile YAML path. Default: config/slm/<profile>.yaml | None |  | str |  |
| `--core` | Process the core memory DB. | `False` |  | store_true |  |
| `--core-db` | Path to the SQLite DB. | — |  | str |  |
| `--source-variant` |  | None |  | str |  |
| `--target-variant` | Required for new runs; ignored for --resume-run (target_variant is read from the existing run row). | None |  | str |  |
| `--resume-run` | Resume an existing enrichment_runs row by id. Skips enumeration + claim + submit; goes straight to poll and ingest for any batches in notes.batches that are not yet ingested. Re-derives group_meta from rows still in_progress under this run_id. The remaining args (--profile, --core-db, --source-variant) must match the original run. | None |  | str |  |
| `--source-conv-list` | File path: newline-list or JSON array of group_keys to filter. | None |  | str |  |
| `--track-state` | Use enrichment_groups state machine. Always on for batch. | `True` |  | store_true |  |
| `--resume` | Resume mode (only claims pending/failed groups). Always on for batch. | `True` |  | store_true |  |
| `--limit` | Cap number of conversations submitted (smoke testing). | None |  | int |  |
| `--poll-interval-s` | Seconds between batch poll requests. Default 30. | `30.0` |  | float |  |
| `--max-wait-s` | Max seconds to wait for batch completion. Default 86400 (24h). | `24 * 3600` |  | float |  |
| `--slice-size` | Override runner.max_batch_size. Use to fit Gemini's Tier-1 enqueued-tokens cap (3M) — at ~5,600 tok/req set this to ~500 for 16k-input convos. | None |  | int |  |
| `--budget-usd` | Hard cap on total run cost in USD. The worker checks after each slice ingest; if cumulative cost exceeds the cap, the run aborts cleanly (claims released, remaining slices NOT submitted). Use to prevent runaway spend on a misconfigured conv-list. | None |  | float |  |
| `--skip-preflight` |  | `True` |  | store_true |  |
| `--yes`, `-y` |  | `True` |  | store_true |  |
| `--dry-run` | Preview chunks; release claims; do not submit to Anthropic. | `False` |  | store_true |  |

---

## Environment variables read

- `COMPUTERNAME`

---

## Calls INTO this repo (intra-repo imports)

- `auth_utils (get_api_key)`
- `batch_runner (BatchRequest, make_runner)`
- `batch_runner (make_runner)`
- `enrichment_state`
- `m3_enrich (_load_conv_list, _query_eligible_groups)`
- `run_observer`
- `slm_intent (_parse_profile)`

---

## Calls OUT (external side-channels)

**http**

- `httpx.AsyncClient()` (line 525)
- `httpx.AsyncClient()` (line 740)

**sqlite**

- `sqlite3.connect()  → `db_path`` (line 120)


---

## Notable external imports

- `httpx`

---

## File dependencies (repo paths referenced)

- `Override profile YAML path. Default: config/slm/<profile>.yaml`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
