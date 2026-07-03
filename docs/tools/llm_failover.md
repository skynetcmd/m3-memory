---
tool: bin/llm_failover.py
sha1: bc7562747833
mtime_utc: 2026-07-02T21:51:11.644462+00:00
generated_utc: 2026-07-03T20:00:03.458711+00:00
private: false
---

# bin/llm_failover.py

## Purpose

LLM Failover Module

Cross-machine failover strategy for selecting LLM and embedding models.
Tries endpoints in order: LM Studio (local + remote), then Ollama.
Used by custom_tool_bridge.py and memory_bridge.py.

---

## Entry points

_(no conventional entry point detected)_

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `M3_EMBED_DISCOVERY_NEG_TTL`
- `M3_LLM_CONNECT_TIMEOUT`
- `M3_LLM_URL`

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (getenv_compat)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `httpx`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
