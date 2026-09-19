---
tool: bin/mcp_proxy.py
sha1: 9a50dcb4f166
mtime_utc: 2026-09-19T02:10:06.579773+00:00
generated_utc: 2026-09-19T02:10:11.241247+00:00
private: false
---

# bin/mcp_proxy.py

## Purpose

MCP Tool Execution Proxy  v2.0
==============================
OpenAI-compatible server on localhost:9000.

Purpose
-------
Aider has no native MCP support, and neither do assorted custom HTTP clients.
This proxy sits between them and the actual model, injecting MCP tools into every
request and executing tool_calls by calling m3 functions directly — no MCP
transport overhead.

OpenClaw is NOT in that list any more: every release since 2026.3.22 speaks MCP
natively, and `m3 setup` wires it directly (setup_wizard._wire_openclaw). Hermes
loads m3 through a file-based plugin, not this proxy. Keep the proxy for the
OpenAI-shape clients that genuinely have no MCP transport.

Tool source
-----------
m3-memory catalog tools from mcp_tool_catalog.TOOLS:
   memory_*, agent_*, task_*, conversation_*, notifications_*, etc.

Two further sources existed until 2026-09-17: 5 "Operational Protocol" tools
from custom_tool_bridge and 6 debug_* tools from debug_agent_bridge. Neither
bridge was part of m3 — both came from the author's pre-release workstation
setup — and both were deleted. The proxy now serves only m3's own catalog.

Default allowlist excludes destructive catalog tools (memory_delete,
gdpr_*, *_export, *_import, memory_maintenance, memory_set_retention, agent_offline).
Set MCP_PROXY_ALLOW_DESTRUCTIVE=1 to expose them.

Agent identity is taken from the X-Agent-Id request header (default
"mcp-proxy-client"). Catalog tools marked inject_agent_id receive this value
non-bypassably: the LLM cannot substitute a DIFFERENT agent_id in its tool
arguments than the one the transport presented.

⚠ That is a CONSISTENCY guarantee, not authentication. Agent identity in m3 is
SELF-ASSERTED BY DESIGN — m3 is a collaborative local-first substrate, and
nothing issues or verifies a per-agent credential:

  * `_check_auth` validates ONE shared master token (MCP_PROXY_KEY). It answers
    "may you use this proxy", never "are you who you say you are". Unset, it
    logs a warning and allows everything.
  * X-Agent-Id is whatever the client writes. Anyone holding the one token can
    set it to any value.
  * `agent_register` stores a name and issues no secret.

So inject_agent_id defends against ACCIDENT — a confused-deputy slip, an LLM
inventing an id mid-call, one agent reading another's inbox by mistake. It does
not defend against INTENT, and this docstring previously claimed it did ("the
LLM cannot spoof another agent"), which overstated the security model. A false
assurance in a security note is worse than no note (§3: a warning that is not
true trains readers to trust the wrong thing).

Treat the trust boundary as the machine and the proxy token, not the agent name.
If you need per-agent authentication, it does not exist yet — do not infer it
from inject_agent_id.

Routing
-------
  model contains "claude"   →  Anthropic Messages API  api.anthropic.com
  model contains "gemini"   →  Google AI (OAI-compat)  generativelanguage.googleapis.com
  model contains "grok"     →  xAI (OAI-compat)        api.x.ai
  model contains "sonar"    →  Perplexity (OAI-compat) api.perplexity.ai
  everything else           →  LM Studio (OAI-compat)  localhost:1234

Tool loop
---------
  Requests are forwarded with MCP tools injected.
  If the model returns tool_calls, they are executed in parallel, results
  fed back, and the loop repeats — up to MAX_TOOL_ROUNDS.
  The final tool-call-free response is returned to the client.

Usage
-----
  python3 ./bin/mcp_proxy.py
  # or
  bash ./bin/start_mcp_proxy.sh

Configure clients:
  Aider aider-local:  --openai-api-base http://localhost:9000/v1
  Aider + Claude:     --model openai/claude-sonnet-4-6 --openai-api-base http://localhost:9000/v1

OpenClaw is NOT listed here any more. It speaks MCP natively since 2026.3.22 and
is wired directly by `m3 setup` (see setup_wizard._wire_openclaw), so it no longer
needs this proxy. The removed line also pointed at a `claw-proxy` shell function
that has never existed in config/zshrc.example -- following it led nowhere.

---

## Entry points

- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `LM_STUDIO_BASE`

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (M3Context)`
- `m3_sdk (acquire_or_exit)`
- `m3_sdk (ensure_utf8)`
- `m3_sdk (getenv_compat)`
- `mcp_tool_catalog`
- `memory_bridge`

---

## Calls OUT (external side-channels)

**http**

- `httpx.AsyncClient()` (line 524)


---

## Notable external imports

- `fastapi (FastAPI, HTTPException, Request)`
- `fastapi.responses (JSONResponse, StreamingResponse)`
- `httpx`
- `pydantic (BaseModel, Field)`
- `uvicorn`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
