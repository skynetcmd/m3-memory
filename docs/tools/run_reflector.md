---
tool: bin/run_reflector.py
sha1: 8ffebb974f27
mtime_utc: 2026-04-28T01:32:25.748225+00:00
generated_utc: 2026-04-28T15:48:17.530891+00:00
private: false
---

# bin/run_reflector.py

## Purpose

Phase D Mastra-style Reflector drainer.

Pulls eligible (user_id, conversation_id) groups from reflector_queue,
loads their existing + new observations from memory_items, calls the
Reflector SLM (qwen/qwen3-8b on LM Studio /v1/messages by default per
config/slm/reflector_local.yaml), parses {observations, supersedes}
output, and translates the supersedes list into memory_link_impl rows
with relationship_type='supersedes'.

m3's existing _check_contradictions does the embedding-based detection
on writes; the Reflector adds an LLM-based pass that catches semantic
contradictions the embedding similarity might miss (different wording,
different attributes).

Modes:
  - Drain mode (default): work through reflector_queue with backoff.
  - Force mode (--force-conversation CID): trigger Reflector immediately
    on a single conversation, bypass queue. Useful for tests.

Status: Phase D Task 4. Pairs with config/slm/reflector_local.yaml.

## Entry points

- `def main()` (line 335)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--concurrency` | Concurrent Reflector SLM calls. | `2` |  | int |  |
| `--batch` | Queue-mode batch size per invocation. Default 50. | `50` |  | int |  |
| `--force-conversation` | Bypass queue: run Reflector on this conversation_id right now. Useful for tests. | None |  | str |  |
| `--force-user` | Required when --force-conversation is set: the user_id to scope the observation lookup. | None |  | str |  |

## Environment variables read

- `REFLECTOR_PROFILE`

## Calls INTO this repo (intra-repo imports)

- `auth_utils (get_api_key)`
- `memory_core`
- `slm_intent (load_profile)`

## Calls OUT (external side-channels)

**http**

- `httpx.AsyncClient()` (line 281)
- `httpx.AsyncClient()` (line 322)


## Notable external imports

- `httpx`

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
