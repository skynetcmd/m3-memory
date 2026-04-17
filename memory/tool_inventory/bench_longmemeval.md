---
tool: bin/bench_longmemeval.py
sha1: c4a7e8c28672
mtime_utc: 2026-04-17T04:16:43.577376+00:00
generated_utc: 2026-04-17T04:17:01.676572+00:00
private: false
---

# bin/bench_longmemeval.py

## Purpose

Long-session QA benchmark runner for m3-memory.

Loads the configured long-session dataset, bulk-ingests every conversation turn
into m3-memory scoped by question_id (so each instance has its own isolated
haystack), then for each question retrieves the top-K most relevant turns and
asks a generator LLM to answer. A separate judge LLM scores the answer using
the per-task judge prompts (model configured by --judge-model or
EVAL_JUDGE_MODEL).

Retrieval pipeline:
  1. Hybrid search (FTS5 BM25 + vector cosine, fused with MMR re-ranking)
  2. Graph expansion (1-hop traversal of knowledge graph from initial hits)
  3. Episodic cluster expansion (+/- N surrounding turns from same session)
  4. Timeline-aware answer prompt for temporal reasoning

Routes embeddings through `memory_write_bulk_impl` / `_embed_many` and expects
llama-server on http://localhost:8081/v1 (override with LLM_ENDPOINTS_CSV).

Usage:
    python bin/bench_longmemeval.py                         # full dataset
    python bin/bench_longmemeval.py --limit 20              # subsample
    python bin/bench_longmemeval.py --skip-ingest           # reuse already-loaded DB
    python bin/bench_longmemeval.py --no-judge              # write hypotheses only
    python bin/bench_longmemeval.py --cluster-size 0 --graph-depth 0  # ablation: hybrid only

Artifacts go to .scratch/longmemeval_run_<timestamp>/:
    hypotheses.jsonl   one line per question
    results.json       aggregate accuracy + per-type breakdown
    run.log            progress/errors

## Entry points

- `async def run()` (line 581)
- `def main()` (line 821)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--dataset` | path to benchmark dataset | `longmemeval_s_cleaned.json` | Loads default dataset from data/ | Path | Uses custom dataset location |
| `--limit` | subsample first N instances (0 = all) | `0` | Processes entire dataset | int | Processes only first N items |
| `--skip-ingest` | reuse DB without re-ingesting | — | Ingest all turns into DB | store_true | Skips ingest phase |
| `--no-judge` | write hypotheses without judging | — | Runs judge phase | store_true | Outputs only hypotheses |
| `--k` | top-K retrieved turns per question | `20` | Retrieves top 20 turns | int | Adjusts retrieval set size |
| `--adaptive-k` | Enable elbow trim for adaptive K | — | Disabled | store_true | Trims K at elbow points |
| `--smart-retrieval` | Enable temporal-aware smart retrieval | — | Disabled | store_true | Boosts K for temporal questions |
| `--cluster-size` | episodic expansion: pull +/- N surrounding turns (0 = off) | `5` | Expands +/- 5 turns per hit | int | Sets episodic expansion radius |
| `--graph-depth` | graph expansion hops from initial hits (0 = off) | `1` | 1-hop graph traversal | int | Sets knowledge graph depth |
| `--generator-model` | LLM model for answer generation | env: `EVAL_GENERATOR_MODEL` | Must be set or errors | str | Uses specified generator model |
| `--judge-model` | LLM model for answer judging | env: `EVAL_JUDGE_MODEL` | Must be set if not --no-judge | str | Uses specified judge model |
| `--ingest-concurrency` | number of instances to ingest in parallel | `4` | 4 parallel ingests | int | Sets parallelism for ingest phase |

## Environment variables read

- `EVAL_GENERATOR_MODEL`
- `EVAL_JUDGE_MODEL`

## Calls INTO this repo (intra-repo imports)

- `auth_utils (Vault/keyring credential lookup for OPENAI_API_KEY)`
- `memory_core (Bulk ingestion, hybrid search, graph traversal, embedding)`
- `temporal_utils (Date parsing, temporal anchor resolution)`

## Calls OUT (external side-channels)

- `OpenAI SDK` (`client.chat.completions.create()` for answer generation and judging)

## Notable external imports

- `openai (OpenAI)`

## File dependencies (repo paths referenced)

- `data/longmemeval/longmemeval_s_cleaned.json` (Benchmark dataset input)
- `.scratch/longmemeval_run_<timestamp>/hypotheses.jsonl` (Question-level predictions and retrieval results)
- `.scratch/longmemeval_run_<timestamp>/results.json` (Aggregate accuracy and per-type breakdown)
- `.scratch/longmemeval_run_<timestamp>/run.log` (Progress and error logs)

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
