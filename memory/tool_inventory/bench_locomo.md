---
tool: bin/bench_locomo.py
sha1: 395aafe2917b
mtime_utc: 2026-04-17T04:16:50.784481+00:00
generated_utc: 2026-04-17T04:17:01.667215+00:00
private: false
---

# bin/bench_locomo.py

## Purpose

Dialog-QA benchmark runner for m3-memory.

Loads the configured dialog-QA dataset, bulk-ingests every conversation turn
into m3-memory scoped by sample_id, then for each question retrieves
the top-K most relevant turns and asks a generator LLM to answer.
A separate judge LLM scores the answer (model configured by --judge-model
or the EVAL_JUDGE_MODEL env var).

Includes:
- Episodic Cluster Expansion (+/- N turns)
- Knowledge Graph Linking (Obs/Sum -> Evidence)
- Graph Expansion (1-hop traversal of retrieved hits)
- Temporal Resolution (relative dates -> absolute)

## Entry points

- `async def run()` (line 452)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--dataset` | Benchmark data file path | locomo10.json | Load default dataset | Path | Use custom dataset |
| `--limit-samples` | Cap dataset to N samples | 0 (unlimited) | Process all samples | int | Truncate dataset early |
| `--limit-questions` | Stop after N total questions | 0 (unlimited) | Answer all questions | int | Early termination |
| `--skip-ingest` | Skip phase 1 ingestion | — | Ingest then retrieve | store_true | Skip straight to retrieval |
| `--ingest-only` | Stop after phase 1 | — | Run full pipeline | store_true | Exit after ingestion |
| `--k` | Top-K retrieved hits baseline | 40 | Retrieve 40 items | int | Adjust retrieval count |
| `--cluster-size` | Episodic expansion turns | 5 | Expand ±5 turns per hit | int | Adjust context window |
| `--graph-depth` | Link traversal hops | 1 | Follow 1-hop edges | int | Deepen graph expansion |
| `--generator-model` | Generator LLM identifier | EVAL_GENERATOR_MODEL env | No default set | string | Specify answer model |
| `--judge-model` | Judge LLM identifier | EVAL_JUDGE_MODEL env | No default set | string | Specify scoring model |
| `--openai-base-url` | Custom API endpoint | — | Use official OpenAI | string | Route to local proxy |
| `--verbose` | Log full message objects | — | Minimal logging | store_true | Debug mode with details |

## Environment variables read

- `EVAL_GENERATOR_MODEL`
- `EVAL_JUDGE_MODEL`
- `OPENAI_API_KEY`

## Calls INTO this repo (intra-repo imports)

- `auth_utils (get_api_key)` (retrieve API keys from vault fallback)
- `memory_core` (bulk item ingestion, graph linking, semantic search)
- `memory_core (memory_write_bulk_impl, memory_link_impl, _db)` (core persistence and relationships)
- `memory_core (memory_search_scored_impl)` (vector search with recency bias)
- `temporal_utils` (date parsing and relative expression resolution)

## Calls OUT (external side-channels)

- `gen_client.chat.completions.create()` (OpenAI SDK; generator model inference)
- `judge_client.chat.completions.create()` (OpenAI SDK; answer scoring inference)

## Notable external imports

- `openai (OpenAI)` (instantiated as gen_client and judge_client for LLM calls)

## File dependencies (repo paths referenced)

- `data/locomo/locomo10.json` (benchmark dataset; conversation turns, Q&A, metadata)
- `.scratch/locomo_run_{timestamp}/hypotheses.jsonl` (model answers; one per line)
- `.scratch/locomo_run_{timestamp}/results.json` (summary accuracy by category)
- `.scratch/locomo_run_{timestamp}/run.log` (detailed per-question logs)

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
