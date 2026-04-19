---
tool: bin/bench_longmemeval.py
sha1: 8fe9cf7956a5
mtime_utc: 2026-04-19T02:44:47.961231+00:00
generated_utc: 2026-04-19T02:53:55.340005+00:00
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

- `async def run()` (line 608)
- `def main()` (line 855)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--dataset` |  | `DEFAULT_DATASET` | Loads longmemeval_s_cleaned.json from data/longmemeval/ | Path | Uses provided JSON file path |
| `--limit` | subsample first N instances (0 = all) | `0` | Process all instances | int | Process first N instances only |
| `--skip-ingest` |  | `False` | Runs phase 1 (ingest) + phase 2 (retrieve/judge) | store_true | Skips phase 1, reuses DB from previous run |
| `--no-judge` |  | `False` | Writes hypotheses.jsonl + runs judge | store_true | Writes hypotheses.jsonl only (skip judge scoring) |
| `--k` | top-K retrieved turns per question | `20` | Retrieve top 20 turns per question | int | Retrieve top N turns (boost to 30 for temporal if --smart-retrieval) |
| `--adaptive-k` | Enable elbow trim for adaptive K | `False` | Uses fixed K value | store_true | Applies elbow trim heuristic to reduce K for low-signal |
| `--smart-retrieval` | Enable temporal-aware smart retrieval | `False` | No temporal boost | store_true | Boosts K to 30 for temporal-classified questions |
| `--cluster-size` | episodic expansion: pull +/- N surrounding turns (0 = off) | `5` | Expands hits ±5 turns in same conversation | int | Expands hits ±N turns (0 disables episodic expansion) |
| `--graph-depth` | graph expansion hops from initial hits (0 = off) | `1` | Traverses 1-hop relationships from retrieved items | int | Traverses N-hop graph (0 disables graph expansion) |
| `--generator-model` |  | `os.environ.get('EVAL_GENERATOR_MODEL')` | Reads EVAL_GENERATOR_MODEL env var | str | Model ID for answer generation |
| `--judge-model` |  | `os.environ.get('EVAL_JUDGE_MODEL')` | Reads EVAL_JUDGE_MODEL env var | str | Model ID for answer scoring |
| `--ingest-concurrency` | number of instances to ingest in parallel | `4` | Ingests 4 instances in parallel | int | Ingests N instances in parallel with asyncio.Semaphore |
| `--per-item` | use memory_write_impl per-turn (enables Phase 1 enrichers). Much slower than bulk path; default off. | `False` | Uses memory_write_bulk_impl (fast) | store_true | Uses memory_write_impl per-turn (enables enrichers, slower) |
| `--variant` | tag every ingested row with this variant label | `` | No variant label | str | Tags all ingested items with variant ID |

## Environment variables read

- `EVAL_GENERATOR_MODEL`
- `EVAL_JUDGE_MODEL`

## Calls INTO this repo (intra-repo imports)

- `auth_utils (get_api_key)`
- `memory_core (_db, memory_search_scored_impl, memory_write_bulk_impl, memory_write_impl)`
- `temporal_utils`

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

- `openai (OpenAI)`

## File dependencies (repo paths referenced)

- `longmemeval_s_cleaned.json`
- `results.json`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
