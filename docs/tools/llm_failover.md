---
tool: bin/llm_failover.py
sha1: 719cf39c5dd9
mtime_utc: 2026-05-01T03:34:53.532005+00:00
generated_utc: 2026-05-01T08:48:37.431872+00:00
private: false
---

# bin/llm_failover.py

## Purpose

LLM Failover Module

Cross-machine failover strategy for selecting LLM and embedding models.
Tries endpoints in order: LM Studio (local + remote), then Ollama.
Used by custom_tool_bridge.py and memory_bridge.py.

## Entry points

_(no conventional entry point detected)_

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

- `LLM_ENDPOINTS_CSV`

## Calls INTO this repo (intra-repo imports)

_(none detected)_

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

- `httpx`

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
