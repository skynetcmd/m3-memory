---
tool: bin/debug_agent_bridge.py
sha1: d56f4de3dbb8
mtime_utc: 2026-04-18T03:45:31.260359+00:00
generated_utc: 2026-04-18T05:16:53.109110+00:00
private: false
---

# bin/debug_agent_bridge.py

## Purpose

Debug Agent MCP Bridge providing autonomous debugging tools via FastMCP. Offers root cause analysis, execution tracing, log correlation, and structured reporting. Integrates thermal awareness and memory-augmented LLM reasoning.

## Entry points / Public API

- `debug_analyze(error_message, context="", file_path="")` — Root cause analysis with memory-augmented reasoning
- `debug_trace(file_path, function_name="", error_type="")` — Execution flow analysis; reads source and finds callers
- `debug_bisect(test_command, good_commit, bad_commit="HEAD")` — Git bisect placeholder (requires interactive shell)
- `debug_correlate(log_file="", time_range="24h", pattern="")` — Cross-reference logs and decisions
- `debug_history(keyword="", limit=10)` — Search past debugging sessions with hybrid relevance ranking
- `debug_report(title, findings, issue_id="")` — Generate and persist structured debug reports

Entry: `if __name__ == "__main__"` invokes `mcp.run()`

## CLI flags / arguments

_(No CLI surface — invoked as a library/module.)_

## Environment variables read

- `AI_WORKSPACE_DIR` — Workspace root; auto-detects parent of bin/ if unset
- `ORIGIN_DEVICE` — Device identifier for logging; defaults to `platform.node()`
- `LM_API_TOKEN` (via `ctx.get_secret()`) — LM Studio authentication

## Calls INTO this repo (intra-repo imports)

- `embedding_utils.parse_model_size` — Model parameter estimation
- `m3_sdk.M3Context` — Database/logging/HTTP context manager
- `m3_sdk.LM_STUDIO_BASE` — LM Studio base URL constant
- `thermal_utils.get_thermal_status` — Hardware thermal state

## Calls OUT (external side-channels)

- **LM Studio HTTP**: `/v1/chat/completions`, `/v1/models`, `/v1/embeddings` (via `ctx.request_with_retry`)
- **SQLite**: agent_memory.db for debug_reports, chroma_sync_queue, event logging
- **ChromaDB queue**: `chroma_sync_queue` insert for embedding syncs

## File dependencies

- `agent_memory.db` — debug_reports, chroma_sync_queue tables
- Workspace root relative to BASE_DIR (default: codebase root)

## Re-validation

If sha1 above differs from current file, inventory is stale. Re-read the tool, confirm flags/env vars/entry-points/calls, then regenerate.
