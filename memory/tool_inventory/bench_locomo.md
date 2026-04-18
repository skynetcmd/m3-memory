---
tool: bin/bench_locomo.py
sha1: dd445cc496a8
mtime_utc: 2026-04-18T14:52:11.799641+00:00
generated_utc: 2026-04-18T16:33:21.572043+00:00
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
| `--dataset` |  | `DEFAULT_DATASET` | Loads locomo10.json from data/locomo/ | Path | Uses provided JSON file path |
| `--limit-samples` |  | `0` | Process all samples | int | Process first N samples only |
| `--limit-questions` |  | `0` | Process all questions | int | Stop after N questions total |
| `--skip-ingest` |  | — | Ingests & links data in phase 1 | store_true | Skips ingest, starts at phase 2 (retrieve) |
| `--ingest-only` |  | — | Runs ingest + retrieve + judge | store_true | Stops after phase 1 (ingest) |
| `--k` |  | `40` | Retrieve top 40 items from search | int | Retrieve top N items (adaptive: +20 for temporal, -10 for adversarial) |
| `--cluster-size` |  | `5` | No episodic expansion | int | Expand hits ±N turns in same conversation |
| `--graph-depth` |  | `1` | Single-hop graph expansion | int | Traverse N hops in relationship graph |
| `--generator-model` |  | `os.environ.get('EVAL_GENERATOR_MODEL')` | Reads EVAL_GENERATOR_MODEL env var | str | Model ID for answer generation |
| `--judge-model` |  | `os.environ.get('EVAL_JUDGE_MODEL')` | Reads EVAL_JUDGE_MODEL env var | str | Model ID for scoring answers |
| `--openai-base-url` | Custom base URL for OpenAI-compatible API (e.g. MCP proxy or LM Studio) | None | Uses official OpenAI/Anthropic endpoint | str | Routes generator to custom provider |
| `--variant` | Pipeline identifier passed to bulk-insert and enrichers for A/B tracking. | `` | No pipeline variant label | str | Tags all ingested items with variant ID |
| `--verbose` | Dump full msg objects per question into run.log | — | Logs only status/errors | store_true | Logs full OpenAI message objects |

## Environment variables read

- `EVAL_GENERATOR_MODEL`
- `EVAL_JUDGE_MODEL`
- `OPENAI_API_KEY`

## Calls INTO this repo (intra-repo imports)

- `auth_utils (get_api_key)`
- `memory_core`
- `memory_core (memory_search_scored_impl)`
- `memory_core (memory_write_bulk_impl, memory_link_impl, _db)`
- `temporal_utils`

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

- `openai (OpenAI)`

## File dependencies (repo paths referenced)

- `locomo10.json`
- `results.json`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
