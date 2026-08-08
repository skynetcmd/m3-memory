---
tool: bin/embed_server.py
sha1: 001803b150a5
mtime_utc: 2026-08-07T23:53:51.896370+00:00
generated_utc: 2026-08-08T14:40:49.824252+00:00
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

---

## Entry points

- `def main()` (line 92)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--model` | HuggingFace model ID | `DEFAULT_MODEL_ID` | Loads Qwen/Qwen3-Embedding-0.6B via SentenceTransformer | str | Loads specified HuggingFace model instead |
| `--port` | Port to serve on | `1234` | Serves /v1/embeddings endpoint on localhost:1234 | int | Serves on specified port |
| `--host` | Host to bind to (default 127.0.0.1; set 0.0.0.0 to serve on LAN) | `getenv_compat('M3_EMBED_SERVER_HOST', 'EMBED_SERVER_HOST', '127.0.0.1')` | Binds only to 127.0.0.1 (localhost only) | str | Binds to specified host (0.0.0.0 for LAN access) |
| `--device` | Device to use (e.g. cuda:0, cpu) | None | Auto-detects: picks cuda:0 if torch.cuda.is_available() else CPU. | str | Forces the SentenceTransformer onto the specified device. |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (getenv_compat)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `fastapi (FastAPI)`
- `m3_core.gpu (torch_device)`
- `pydantic (BaseModel, Field)`
- `sentence_transformers (SentenceTransformer)`
- `uvicorn`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
