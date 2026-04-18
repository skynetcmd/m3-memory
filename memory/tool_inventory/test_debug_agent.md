---
tool: bin/test_debug_agent.py
sha1: 0976552a06cb
mtime_utc: 2026-04-06T00:25:00.989103+00:00
generated_utc: 2026-04-18T05:16:53.229949+00:00
private: false
---

# bin/test_debug_agent.py

## Purpose

End-to-end test suite for debug_agent_bridge.py.

Tests all 6 MCP tools plus helper functions. LLM-dependent tests are
gracefully skipped when LM Studio is offline.

## Entry points

- `async def run()` (line 110)
- `async def main()` (line 287)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `auth_utils (get_api_key)`
- `debug_agent_bridge (_check_thermal, _get_largest_llm_model, _log_to_db, _safe_read_file, debug_analyze, debug_bisect, debug_correlate, debug_history, debug_report, debug_trace)`

## Calls OUT (external side-channels)

**http**

- `httpx.AsyncClient()` (line 56)

**sqlite**

- `sqlite3.connect()  → `DB_PATH`` (line 173)
- `sqlite3.connect()  → `DB_PATH`` (line 181)
- `sqlite3.connect()  → `DB_PATH`` (line 81)
- `sqlite3.connect()  → `DB_PATH`` (line 92)


## Notable external imports

- `httpx`

## File dependencies (repo paths referenced)

- `agent_memory.db`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
