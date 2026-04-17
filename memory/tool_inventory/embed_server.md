---
tool: bin/embed_server.py
sha1: d15de239efa1
mtime_utc: 2026-04-17T00:50:00.647616+00:00
generated_utc: 2026-04-17T04:17:01.696291+00:00
private: false
---

# bin/embed_server.py

## Purpose

Local embedding server — OpenAI-compatible /v1/embeddings endpoint.

Uses sentence-transformers to load Qwen3-Embedding-0.6B and serves on
port 1234 so memory_core.py and the test suite can use it without
LM Studio or Ollama.

Usage:
    python bin/embed_server.py                     # default: port 1234
    python bin/embed_server.py --port 9900

## Entry points

- `def main()` (line 91)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--model` | HuggingFace model ID | `DEFAULT_MODEL_ID` |  |  |  |
| `--port` | Port to serve on | `1234` |  | int |  |
| `--host` | Host to bind to | `0.0.0.0` |  |  |  |
| `--device` | Device to use (e.g. cuda:0, cpu) | — |  |  |  |

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

_(none detected)_

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

- `fastapi (FastAPI)`
- `pydantic (BaseModel, Field)`
- `sentence_transformers (SentenceTransformer)`
- `torch`
- `uvicorn`

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
