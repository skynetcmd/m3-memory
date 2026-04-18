---
tool: bin/mcp_proxy.py
sha1: 62e2d2ea920f
mtime_utc: 2026-04-18T03:45:31.261360+00:00
generated_utc: 2026-04-18T05:16:53.134280+00:00
private: false
---

# bin/mcp_proxy.py

## Purpose

MCP Tool Execution Proxy v2.0 — catalog-driven dispatch with X-Agent-Id injection and MCP_PROXY_ALLOW_DESTRUCTIVE env gating. OpenAI-compatible server on localhost:9000 that injects MCP tools into Aider/OpenClaw requests and executes tool_calls via direct bridge calls (no MCP transport overhead). Supports multi-backend routing: Anthropic, Google AI, xAI Grok, Perplexity, LM Studio.

## Entry points / HTTP handlers

- `POST /v1/chat/completions` (line 927) — main completion endpoint. Validates X-Agent-Id header, injects MCP tools, runs tool loop up to MAX_TOOL_ROUNDS (default 10), returns JSON or SSE stream.
- `GET /v1/models` (line 882) — lists available models (Claude, Gemini, Grok, Sonar) + LM Studio loaded models. Requires MCP_PROXY_KEY auth.
- `GET /health` (line 856) — health check returning tool counts, backends, allow_destructive flag.
- `if __name__ == "__main__"` (line 1010) — uvicorn entry point.

## CLI flags / arguments

None. Launched via `uvicorn` directly or `python3 ./bin/mcp_proxy.py`. No argparse.

## Environment variables read

- `MCP_PROXY_HOST` — bind address (default: 127.0.0.1)
- `MCP_PROXY_KEY` — Bearer token for auth; if unset, warning logged and requests allowed (local dev mode)
- `MCP_PROXY_ALLOW_DESTRUCTIVE` — "1"/"true"/"yes" to expose destructive catalog tools (memory_delete, chroma_sync, gdpr_*, *_export, *_import, memory_maintenance, memory_set_retention, agent_offline). Default: disabled.
- `LM_STUDIO_BASE` — LM Studio endpoint (default: http://localhost:1234/v1)
- `LM_READ_TIMEOUT` — read timeout in seconds (default: 300)
- `ANTHROPIC_API_KEY` or `sk-ant` — Anthropic auth
- `GEMINI_API_KEY` — Google AI auth
- `XAI_API_KEY` — xAI Grok auth
- `PERPLEXITY_API_KEY` — Perplexity auth
- `LM_STUDIO_API_KEY` or `LM_API_TOKEN` — LM Studio auth

## Calls INTO this repo (intra-repo imports)

- `m3_sdk.M3Context` — secret/key resolution, async client pooling
- `custom_tool_bridge` (lazy, line 168) — 5 protocol tools (log_activity, query_decisions, update_focus, retire_focus, check_thermal_load)
- `debug_agent_bridge` (lazy, line 194) — 6 debug tools (debug_analyze, debug_bisect, debug_trace, debug_correlate, debug_history, debug_report)
- `mcp_tool_catalog` (lazy, line 395) — 44 catalog tools from m3-memory; respects default_allowed and inject_agent_id fields

## Calls OUT (external side-channels)

**http**

- `httpx.AsyncClient()` (line 659) — shared pooled client with keepalive, HTTP/2. Timeout: connect=5s, read=LM_READ_TIMEOUT, write=10s, pool=5s. Max 20 keepalive, 100 total connections.
- `POST {ANTHROPIC_BASE}/messages` (line 695) — Anthropic Messages API with x-api-key, anthropic-version headers
- `POST {base_url}/chat/completions` (line 720-721) — OpenAI-compatible backends (Google AI, Grok, Perplexity, LM Studio)
- `GET {LMSTUDIO_BASE}/models` (line 900) — query LM Studio for loaded models (timeout 2s)

**Dispatch (async executors)**

- `_execute_tool()` (line 472) — routes to legacy bridges or mcp_tool_catalog.execute_tool(); concurrent semaphore = 5; per-tool timeout = 300s
- Tool results appended to message history and fed back in loop

## Notable external imports

- `fastapi` (FastAPI, Request, HTTPException)
- `fastapi.responses` (JSONResponse, StreamingResponse)
- `httpx` (AsyncClient, Timeout, HTTPStatusError, ConnectError, ReadTimeout)
- `uvicorn`
- `pydantic` (BaseModel, Field)

## File dependencies (repo paths referenced)

- Implicit: `custom_tool_bridge.py`, `debug_agent_bridge.py`, `mcp_tool_catalog.py`, `memory_bridge.py` (all in bin/)

## Tool injection & agent identity

- X-Agent-Id header (default "mcp-proxy-client") is injected non-bypassably into catalog tools marked `inject_agent_id=True`
- LLM cannot spoof another agent
- Default allowlist excludes destructive tools; MCP_PROXY_ALLOW_DESTRUCTIVE=1 exposes them

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm all env vars (especially MCP_PROXY_KEY, MCP_PROXY_ALLOW_DESTRUCTIVE, auth tokens), HTTP endpoints (/v1/chat/completions, /v1/models, /health), and backend routing still match, then regenerate via `python bin/gen_tool_inventory.py`.
