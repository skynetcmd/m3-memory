---
tool: bin/embed_server_gpu.py
sha1: 991d7cb97921
mtime_utc: 2026-04-17T01:46:21.084758+00:00
generated_utc: 2026-04-17T04:17:01.697848+00:00
private: true
---

# bin/embed_server_gpu.py

## Purpose

AMD GPU Optimized Embedding Proxy — delegates to llama-server.exe.
Handles <|endoftext|> appending and L2 normalization for Qwen3 GGUF.
Runs on Port 9903 by default.

## Entry points

- `def main()` (line 109)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--port` |  | `9903` |  | int |  |

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

_(none detected)_

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.Popen()  → `cmd`` (line 90)

**http**

- `httpx.AsyncClient()` (line 50)
- `httpx.Client()` (line 97)


## Notable external imports

- `fastapi (FastAPI)`
- `httpx`
- `numpy`
- `pydantic (BaseModel, Field)`
- `uvicorn`

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
