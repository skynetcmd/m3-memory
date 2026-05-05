---
tool: bin/m3_enrich_batch_parallel.py
sha1: db701d0ba7a4
mtime_utc: 2026-05-05T13:41:31.179470+00:00
generated_utc: 2026-05-05T13:54:32.090017+00:00
private: false
---

# bin/m3_enrich_batch_parallel.py

## Purpose

m3_enrich_batch_parallel — launch N pipelined batch workers against
disjoint shards of a conv-list, staggered so each worker's first-slice
submit is offset by --start-offset-s seconds from the previous worker's
first-slice submit.

Use this when one m3_enrich_batch.py worker can't keep Anthropic's batch
tier saturated (typical: at slice_size=500 with 5-21 min batch wallclocks,
a single worker spends most of its time waiting on Anthropic). Multiple
workers running in parallel against disjoint conv-list shards keep more
batches in flight on the provider side, with the only local contention
being SQLite's WAL writer lock (which serializes claims and ingests but
runs at ~ms scale, not minutes).

The --start-offset-s flag controls the gap between the START of one
worker (i.e. process spawn time) and the FIRST SUBMIT of the next
worker — accounting for enumeration time. With the patched fast
enumeration (~20s for 19K-key bucket), 120s gives each worker ~100s
of headroom to finish enumeration + first submit before the next one
starts hitting the bench DB.

Usage:
    python bin/m3_enrich_batch_parallel.py \
        --workers 3 \
        --start-offset-s 120 \
        --profile enrich_google_gemini \
        --core --core-db memory/your-corpus.db \
        --source-variant your-variant \
        --target-variant your-target \
        --source-conv-list .scratch/full-list.txt \
        --slice-size 500

The conv-list is sharded round-robin across workers. Each worker logs to
logs/<base>_worker<N>_<ts>.log. PIDs are reported up front for kill-by-PID.

---

## Entry points

- `def main()` (line 182)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--workers` | Number of parallel workers. Default 3. | `3` |  | int |  |
| `--start-offset-s` | Seconds between START of worker N and START of worker N+1. Default 120. | `120` |  | int |  |
| `--profile` |  | — |  | str |  |
| `--profile-path` |  | None |  | str |  |
| `--core` |  | `False` |  | store_true |  |
| `--core-db` |  | — |  | str |  |
| `--source-variant` |  | None |  | str |  |
| `--target-variant` |  | None |  | str |  |
| `--source-conv-list` | Will be sharded round-robin across workers. | — |  | str |  |
| `--slice-size` |  | `500` |  | int |  |
| `--poll-interval-s` |  | `60.0` |  | float |  |
| `--max-wait-s` |  | `24 * 3600` |  | float |  |
| `--budget-usd` | Per-worker budget cap. Total spend can be up to workers × budget_usd. | None |  | float |  |
| `--embed-url` | Pin observation embeds to this URL on every worker (passed through as --embed-url to each). Default discovery prefers LMS :1234 (1-slot); set this to the multi-slot llama.cpp endpoint (e.g. http://127.0.0.1:8081/v1) to avoid throttling 3-worker ingest through a single slot. Env: M3_EMBED_URL. | `os.environ.get('M3_EMBED_URL')` |  | str |  |
| `--embed-model` | Model id for the override endpoint. See m3_enrich_batch.py --embed-model. Env: M3_EMBED_MODEL. | `os.environ.get('M3_EMBED_MODEL')` |  | str |  |
| `--shard-dir` | Directory for shard files. Default: .scratch/ | None |  | str |  |
| `--log-base` | Log basename. Default: gemini_parallel_<ts> | None |  | str |  |

---

## Environment variables read

- `M3_EMBED_MODEL`
- `M3_EMBED_URL`

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.Popen()  → `cmd`` (line 119)


---

## Notable external imports

- `shlex`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
