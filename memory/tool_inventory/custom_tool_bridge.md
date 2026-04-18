---
tool: bin/custom_tool_bridge.py
sha1: c0a316ca4e61
mtime_utc: 2026-04-18T03:45:31.259360+00:00
generated_utc: 2026-04-18T05:16:53.105050+00:00
private: false
---

# bin/custom_tool_bridge.py

## Purpose

MCP server (FastMCP) exposing activity logging, system focus management, research tools (Perplexity/Grok web search), local LLM reasoning, thermal status checks, and decision history queries. Bridges SDK logging, SQLite memory DB, and external APIs.

## Entry points / Public API

- `log_activity(category, detail_a, detail_b, detail_c="None")` — routes AI data to agent_memory.db; marks decision-tagged items with importance=1.0
- `update_focus(summary)` — sets system focus ticker (fixed ID=1)
- `retire_focus()` — clears system focus
- `check_thermal_load()` — returns Protocol #2 status: Nominal/Fair/Serious/Critical
- `query_decisions(keyword="", limit=10)` — searches project_decisions table; Protocol #4 (Search Rule)
- `web_search(query)` — Perplexity sonar-pro → Grok grok-3-latest fallback
- `m3_web_search(query)` — alias for web_search
- `grok_ask(query)` — direct Grok query for real-time X/reasoning data
- `query_local_model(prompt)` — failover LLM selection (localhost → MacBook → SkyPC → GPU VM); extracts reasoning from \<think> tags; archives chains >200 chars

## CLI flags / arguments

_(no CLI surface — invoked as a library/module.)_

## Environment variables read

- `LM_API_TOKEN` — defaults to "lm-studio" if unset (via ctx.get_secret)
- `PERPLEXITY_API_KEY` — web_search primary (Keychain fallback)
- `GROK_API_KEY` — web_search fallback (Keychain fallback)
- `XAI_API_KEY` — grok_ask exclusive (Keychain fallback)

## Calls INTO this repo (intra-repo imports)

- `llm_failover.get_best_llm(client, token)` — endpoint + model selection
- `m3_sdk.M3Context` — SQLite conn, secret mgmt, async client, request_with_retry
- `m3_sdk.StructuredLogger` — format_event helper
- `thermal_utils.get_thermal_status()` — Protocol #2 status

## Calls OUT (external side-channels)

- HTTP POST: https://api.perplexity.ai/chat/completions (sonar-pro, 30s timeout)
- HTTP POST: https://api.x.ai/v1/chat/completions (grok-3-latest, 30s timeout)
- HTTP POST: dynamic LLM endpoints (localhost:8000, etc., 4800s timeout for local models)
- SQLite: agent_memory.db (write: activity log, focus updates; read: decision queries)

## File dependencies

- `memory/agent_memory.db` — project_decisions, system_focus, activity tables
