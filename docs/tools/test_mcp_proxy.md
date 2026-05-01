---
tool: bin/test_mcp_proxy.py
sha1: f4828f297b84
mtime_utc: 2026-04-21T20:59:44.495160+00:00
generated_utc: 2026-05-01T13:05:27.092410+00:00
private: false
---

# bin/test_mcp_proxy.py

## Purpose

test_mcp_proxy.py — End-to-end proxy test suite
================================================
Tests the MCP Tool Execution Proxy (localhost:9000) with:
  T1 — Health check (all 5 backends listed)
  T2 — Claude via proxy (tool call execution verified)
  T3 — Gemini via proxy (tool call execution verified)
  T4 — aider-claude non-interactive (subprocess, exit 0)

Usage:
  # Start proxy first:
  bash ~/m3-memory/bin/start_mcp_proxy.sh --background
  # Then run:
  python3 ~/m3-memory/bin/test_mcp_proxy.py

---

## Entry points

- `async def main()` (line 233)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `ANTHROPIC_API_KEY`

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (resolve_db_path)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `['security', 'find-generic-password', '-s', 'ANTHROPIC_API_KEY', '-w']`` (line 181)
- `subprocess.run()  → `cmd`` (line 209)

**http**

- `httpx.AsyncClient()` (line 63)
- `httpx.AsyncClient()` (line 86)

**sqlite**

- `sqlite3.connect()  → `DB_PATH`` (line 74)


---

## Notable external imports

- `httpx`

---

## File dependencies (repo paths referenced)

- `{WORKSPACE}/.aider.conf.yml`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
