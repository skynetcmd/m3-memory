---
tool: bin/chatlog_ingest.py
sha1: 50ea1c9e7633
mtime_utc: 2026-04-25T00:09:43.010303+00:00
generated_utc: 2026-04-26T10:12:31.931751+00:00
private: false
---

# bin/chatlog_ingest.py

## Purpose

chatlog_ingest.py — CLI that reads a host-agent transcript file and writes
canonical chat-log rows via chatlog_core.chatlog_write_bulk_impl.

Invoked by host-agent hooks (Claude Code PreCompact/Stop, Gemini SessionEnd, etc.),
which receive a JSON envelope from the host and forward the transcript path as
--transcript-path. Parsers target the real on-disk transcript schemas, not a
hypothetical canonical format.

CLI:
  python bin/chatlog_ingest.py --format {claude-code,gemini-cli}
                               --transcript-path FILE
                               [--session-id ID] [--variant LABEL]

A per-session cursor at memory/.chatlog_ingest_cursor.json records which
message ids / indices have been ingested so re-invoking on the same transcript
(e.g. Stop hook every turn) stays idempotent.

## Entry points

- `async def main()` (line 396)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--format` | Transcript format / host agent | — | Auto-detects format from first 4KB of data | str | Parses as specified format (claude-code/gemini-cli/opencode/aider) |
| `--transcript-path` | Path to the transcript file on disk | — | Reads transcript file at specified path | str | Uses the provided file path as transcript source |
| `--session-id` | Override conversation_id (defaults to parsed sessionId) | `` | Uses conversation_id parsed from transcript | str | Overrides conversation_id; used if transcript lacks sessionId |
| `--variant` | Provenance tag (e.g. pre_compact, stop, session_end, test) | None | No variant tag added to rows | str | Tags each ingested row with provided variant value |
| `--db` | Deprecated: chatlog-only override. Prefer --database. Sets CHATLOG_DB_PATH for the duration of the process. | None | Deprecated alias for --database / CHATLOG_DB_PATH | str | Deprecated alias for --database / CHATLOG_DB_PATH |
| `--spill-dir` | Override spill directory for this run (dev smoke tests). Prevents stale spill files from polluting production. | None | Uses configured spill directory | str | Routes spill writes to PATH instead of configured dir (dev smoke tests only) |
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None | Falls back to M3_DATABASE env then memory/agent_memory.db | str | Routes this run against PATH for all DB reads/writes |

## Environment variables read

- `USER`
- `USERNAME`

## Calls INTO this repo (intra-repo imports)

- `chatlog_config`
- `chatlog_core`
- `m3_sdk (add_database_arg)`

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

- `platform`

## File dependencies (repo paths referenced)

- `.chatlog_ingest_cursor.json`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
