---
tool: bin/embedding_utils.py
sha1: 31089f102771
mtime_utc: 2026-04-06T00:25:00.981103+00:00
generated_utc: 2026-04-18T05:16:53.117559+00:00
private: false
---

# bin/embedding_utils.py

## Purpose

Shared embedding and vector-math utilities for MCP bridges. Consolidates duplicated code from memory_bridge.py and debug_agent_bridge.py:
- Binary packing/unpacking for embedding storage
- Cosine similarity (numpy-accelerated with pure-Python fallback)
- Model size parsing for dynamic model selection
- Change-agent inference from agent_id/model_id hints

## Entry points / Public API

- `sanitize(text: str) -> str` — UTF-8 sanitization with Unicode normalization (M12)
- `pack(floats: list[float]) -> bytes` — Pack floats to binary (4 bytes/float)
- `unpack(blob: bytes) -> list[float]` — Unpack binary to float list
- `cosine(a: list[float], b: list[float]) -> float` — Cosine similarity (numpy or pure-Python)
- `batch_cosine(query: list[float], matrix: list[list[float]]) -> list[float]` — Batch similarity
- `parse_model_size(model_id: str) -> float` — Extract model size in billions
- `parse_model_size_with_id(model_id: str) -> tuple[float, str]` — Size + original ID pair
- `infer_change_agent(agent_id: str, model_id: str, default: str) -> str` — Platform inference

## CLI flags / arguments

_(no CLI surface — invoked as a library/module.)_

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

_(none detected)_

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## File dependencies

- `numpy` (optional; pure-Python fallback if unavailable)
- Standard library: `logging`, `re`, `struct`, `unicodedata`

## Re-validation

If `sha1` differs from current file, inventory is stale. Re-read source, confirm API/env-vars/calls, regenerate via `python bin/gen_tool_inventory.py`.
