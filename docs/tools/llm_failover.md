---
tool: bin/llm_failover.py
sha1: d11ebcc78a46
mtime_utc: 2026-08-08T12:37:08.144890+00:00
generated_utc: 2026-08-08T14:40:49.890257+00:00
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

- `LM_API_TOKEN`
- `M3_EMBED_DISCOVERY_NEG_TTL`
- `M3_LLM_CONNECT_TIMEOUT`
- `M3_LLM_URL`

---

## Calls INTO this repo (intra-repo imports)

- `auth_utils (get_api_key)`
- `m3_sdk (getenv_compat)`

---

## Calls OUT (external side-channels)

**http**

- `httpx.get()  → `f"{endpoint.rstrip('/')}/models"`` (line 256)


---

## Notable external imports

- `httpx`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
