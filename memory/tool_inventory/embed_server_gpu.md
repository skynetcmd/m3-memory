---
tool: bin/embed_server_gpu.py
sha1: 078970016f69
mtime_utc: 2026-04-18T03:24:41.215160+00:00
generated_utc: 2026-04-18T16:33:21.632648+00:00
private: true
---

# bin/embed_server_gpu.py

## Purpose

AMD GPU Optimized Embedding Proxy — delegates to llama-server.exe.
Handles <|endoftext|> appending and L2 normalization for Qwen3 GGUF.
Runs on Port 9903 by default.

## Entry points

- `def main()` (line 114)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--port` | Port to serve proxy on | `9903` | Proxy listens on localhost:9903, forwards to llama-server | int | Proxy listens on specified port |
| `--host` | Host to bind to (default 127.0.0.1; set 0.0.0.0 to serve on LAN) | `os.environ.get('EMBED_SERVER_GPU_HOST', '127.0.0.1')` | Binds only to 127.0.0.1 (localhost only) | str | Binds to specified host (0.0.0.0 for LAN access) |

## Environment variables read

- `EMBED_SERVER_GPU_HOST`
- `GGUF_MODEL_PATH`
- `LLAMA_PORT`
- `LLAMA_SERVER_EXE`

## Calls INTO this repo (intra-repo imports)

_(none detected)_

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.Popen()  → `cmd`` (line 95)

**http**

- `httpx.AsyncClient()` (line 55)
- `httpx.Client()` (line 102)


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
