---
tool: bin/chatlog_ingest.py
sha1: 69ade5c04e4f
mtime_utc: 2026-04-18T15:51:47.878968+00:00
generated_utc: 2026-04-18T16:33:21.601606+00:00
private: false
---

# bin/chatlog_ingest.py

## Purpose

chatlog_ingest.py — single-entry-point CLI for ingesting host-agent chat logs.

Normalizes logs from claude-code, gemini-cli, opencode, and aider into canonical
format and writes to the chat log subsystem via chatlog_core.chatlog_write_bulk_impl.

Usage:
  python bin/chatlog_ingest.py --format {claude-code,gemini-cli,opencode,aider,auto} [--watch DIR] [--conversation-id ID] [input-file]
  - Reads stdin when no input file given.
  - --watch mode: polls a directory for new/updated log files, keeps a cursor at
    memory/.chatlog_ingest_cursor.json (atomic rename on update), debounces 500ms.
    Exits cleanly on SIGTERM or KeyboardInterrupt.

## Entry points

- `async def main()` (line 380)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--format` | Log format | `auto` | Auto-detects format from first 4KB of data | choice | Parses as specified format (claude-code/gemini-cli/opencode/aider) |
| `--watch` | Poll directory for new log files | None (process single file) | Reads input_file or stdin once | str | Polls DIR every 500ms for new .jsonl/.md/.json files |
| `--conversation-id` | Override conversation_id | `` (derive from filename or stdin hash) | Derives from input filename using blake2b hash | str | Uses specified conversation_id for all ingested items |
| `--model` | Override model_id (aider) | `` | Uses model_id from parsed data | str | Replaces model_id for items with unknown/missing model |
| `input_file` | Input file (stdin if omitted) | — (read from stdin) | Reads from stdin | positional | Reads specified file instead of stdin |

## Environment variables read

- `USER`
- `USERNAME`

## Calls INTO this repo (intra-repo imports)

- `chatlog_core`

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

- `platform`

## File dependencies (repo paths referenced)

- `.chatlog_ingest_cursor.json`
- `.json`
- `.md`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
