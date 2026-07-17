---
tool: bin/embed_server_inproc.py
sha1: b11349ac478b
mtime_utc: 2026-07-14T02:42:16.065253+00:00
generated_utc: 2026-07-17T02:18:40.548694+00:00
private: false
---

# bin/embed_server_inproc.py

## Purpose

Shared in-process GPU embedder server — one CUDA context, many thin clients.

WHY: `m3_core_rs.EmbeddedEmbedder` runs the GGUF embedder in-process. Every
process that embeds in-process opens its OWN CUDA context (multi-GB host
reservation) — CUDA contexts do not cross process boundaries, so the MCP memory
server and the cognitive loop each loaded a full copy (~4 GB + ~12 GB ≈ 18 GB on
SkyPC) for one embedder's worth of work. This server loads the embedder ONCE and
serves it over localhost HTTP; clients disable their own tier-1 (M3_EMBED_GGUF
unset + M3_EMBED_GGUF_AUTODETECT=0) and defer to it via M3_EMBED_FALLBACK_URL.

The win is HOST RAM, not latency: one CUDA context instead of one-per-process.
Measured on SkyPC (RTX 5080): a SINGLE small embed is P50 ~33 ms / P95 ~48 ms
via this server vs ~28 ms in-process — the localhost HTTP round-trip adds a few
ms (~10-15% on a single small request), not a rounding error. That per-call cost
AMORTISES across a batch (one round-trip for N vectors), so bulk paths — the
cognitive loop, file ingestion — see negligible overhead. Trade a few ms on
interactive single embeds for ~9-10 GB of host RAM back.

CONTRACT (matches the client tier-2 fallback in bin/memory/embed.py):
    POST /embedding        {"input": [texts...]}  -> {"data": [{"embedding": [...]}, ...]}  (input order)
    POST /v1/embeddings    {"model": ..., "input": str|list} -> OpenAI shape  (compat / tier-3)
    GET  /health           -> {"status", "model", "dim"}
Binary fast-path (high-volume ingestion): send `Accept: application/octet-stream`
to /embedding -> body = little-endian f32, row-major [n, dim], preceded by an
8-byte header (uint32 n, uint32 dim). numpy on both ends; no JSON float bloat.

HARDENING (§6): binds 127.0.0.1 only by default (the embedder is not a LAN
service); a semaphore serialises GPU calls; request size is capped. FAIL-LOUD
(§3): if the embedder can't load, exit non-zero with a clear message rather than
serve garbage. GPU-matrix (§1): the wheel picks CUDA/Metal/Vulkan/CPU; this
server is backend-agnostic — it just loads whatever EmbeddedEmbedder resolves.

Usage:
    python bin/embed_server_inproc.py                 # 127.0.0.1:8082 (the tier-2 default)
    python bin/embed_server_inproc.py --port 8082 --host 127.0.0.1
Model: M3_EMBED_GGUF env, else auto-detected (discover_bge_m3_gguf).

---

## Entry points

- `def main()` (line 420)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--port` |  | `int(os.environ.get('M3_EMBED_SERVER_PORT', '8082'))` |  | int |  |
| `--host` | Bind host. Default 127.0.0.1 (loopback only — the embedder is not a LAN service). Set 0.0.0.0 ONLY deliberately. | `os.environ.get('M3_EMBED_SERVER_HOST', '127.0.0.1')` |  | str |  |
| `--log-file` |  | None |  | str |  |

---

## Environment variables read

- `M3_EMBED_GGUF`
- `M3_EMBED_INTERACTIVE_MAX_TEXTS`
- `M3_EMBED_SERVER_CONCURRENCY`
- `M3_EMBED_SERVER_HOST`
- `M3_EMBED_SERVER_INTERACTIVE_RESERVED`
- `M3_EMBED_SERVER_MAX_BATCH`
- `M3_EMBED_SERVER_PORT`

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `fastapi (FastAPI, Request, Response)`
- `fastapi.responses (JSONResponse)`
- `memory (config)`
- `memory.embed (_EMBED_GGUF_MODEL_TAG, discover_bge_m3_gguf)`
- `numpy`
- `pydantic (BaseModel, Field)`
- `uvicorn`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
