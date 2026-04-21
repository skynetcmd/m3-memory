---
tool: bin/test_debug_agent.py
sha1: d698fd7e3504
mtime_utc: 2026-04-21T20:59:41.985179+00:00
generated_utc: 2026-04-21T21:22:27.218306+00:00
private: false
---

# bin/test_debug_agent.py

## Purpose

End-to-end test suite for debug_agent_bridge.py.

Tests all 6 MCP tools plus helper functions. LLM-dependent tests are
gracefully skipped when LM Studio is offline.

## Entry points

- `async def run()` (line 114)
- `async def main()` (line 291)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `auth_utils (get_api_key)`
- `debug_agent_bridge (_check_thermal, _get_largest_llm_model, _log_to_db, _safe_read_file, debug_analyze, debug_bisect, debug_correlate, debug_history, debug_report, debug_trace)`
- `m3_sdk (resolve_db_path)`

## Calls OUT (external side-channels)

**http**

- `httpx.AsyncClient()` (line 60)

**sqlite**

- `sqlite3.connect()  → `DB_PATH`` (line 177)
- `sqlite3.connect()  → `DB_PATH`` (line 185)
- `sqlite3.connect()  → `DB_PATH`` (line 85)
- `sqlite3.connect()  → `DB_PATH`` (line 96)


## Notable external imports

- `httpx`

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
