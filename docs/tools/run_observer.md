---
tool: bin/run_observer.py
sha1: 5394feadb954
mtime_utc: 2026-04-28T15:49:02.670270+00:00
generated_utc: 2026-04-28T15:49:05.318422+00:00
private: false
---

# bin/run_observer.py

## Purpose

Phase D Mastra-style Observer drainer.

Pulls eligible (user_id, conversation_id) groups from observation_queue,
builds a JSON multi-turn block, calls the Observer SLM (qwen/qwen3-8b on
LM Studio /v1/messages by default per config/slm/observer_local.yaml),
parses {observations: [...]} output, and writes type='observation' rows
with three-date metadata:

  observation_date  → memory_items.created_at (when assistant logged it)
  referenced_date   → memory_items.valid_from (when fact is about)
  relative_date     → metadata_json.relative_date (audit-only)
  supersedes_hint   → metadata_json.supersedes_hint (Reflector input)
  confidence        → metadata_json.confidence

Usage modes:
  - Drain mode (default): work through the observation_queue, retrying with
    backoff. Used by the production CLI (`m3 observe-pending`) and the
    bench harness.
  - Variant mode (--source-variant + --target-variant): bench-style
    one-shot enrichment over a corpus snapshot, like run_fact_enrichment.py
    does for fact_enriched. Skips the queue entirely; pulls all eligible
    conversations from --source-variant.

Status: Phase D Task 3. Pairs with config/slm/observer_local.yaml.

## Entry points

- `def main()` (line 564)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--source-variant` | Variant-mode: pull conversations from this variant. When set, drains the entire variant; ignores observation_queue. | None |  | str |  |
| `--target-variant` | Variant tag for emitted observation rows. Empty = production default (NULL). | `` |  | str |  |
| `--limit` | Cap source rows in variant mode (for smokes). | None |  | int |  |
| `--concurrency` | Concurrent Observer SLM calls. | `4` |  | int |  |
| `--qids-file` | Optional JSON file with a list of ids; scopes variant-mode work to those ids only. | None |  | str |  |
| `--batch` | Queue-mode batch size per invocation. Default 100. | `100` |  | int |  |

## Environment variables read

- `OBSERVER_PROFILE`

## Calls INTO this repo (intra-repo imports)

- `auth_utils (get_api_key)`
- `memory_core`
- `slm_intent (load_profile)`

## Calls OUT (external side-channels)

**http**

- `httpx.AsyncClient()` (line 443)
- `httpx.AsyncClient()` (line 484)


## Notable external imports

- `httpx`

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
