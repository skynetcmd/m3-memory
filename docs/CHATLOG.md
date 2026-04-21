# Chat Log Subsystem: Architecture & Operations Guide

## 1. Overview

The chat log subsystem ingests chat transcripts from Claude Code, Gemini CLI, OpenCode, and Aider into a dedicated memory type `chat_log`. Each message is tagged with full provenance (host agent, provider, model, conversation ID) and optionally embedded for semantic search.

### Database routing (unified model)

Chat logs write to whatever path `chatlog_db_path()` resolves to. Resolution order:

1. `CHATLOG_DB_PATH` env var — explicit chatlog-only override
2. `M3_DATABASE` env var — unified main DB (chatlog shares it)
3. `.chatlog_config.json` `db_path` field
4. Default: `memory/agent_chatlog.db` (separate file)

If the resolved chatlog path **equals** the main memory DB path, the two are a single file and full vector/hybrid search is delegated to the main search impl (what the old "integrated" mode did). If they **differ**, chatlog writes are append-tuned and `chatlog_promote` ATTACHes the main DB to copy rows across (what the old "separate"/"hybrid" modes did). The choice is now implicit in the path, not a configuration enum.

> **Deprecation**: the `CHATLOG_MODE` env var and the `mode` field in `.chatlog_config.json` are ignored (a warning is emitted once per process if `CHATLOG_MODE` is set). To keep everything in a single file, set `M3_DATABASE` and `CHATLOG_DB_PATH` to the same path, or leave `CHATLOG_DB_PATH` unset so it follows `M3_DATABASE`.

### Data Flow

```
┌─────────────────────────────────────────────────────────────────┐
│ Host Agents (Claude Code, Gemini CLI, OpenCode, Aider)         │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                               ▼
                    ┌──────────────────────┐
                    │ Hooks (sh/ps1)       │
                    │ - precompact         │
                    │ - onExit             │
                    │ - session_end        │
                    │ - chat_watcher       │
                    └──────────────┬───────┘
                                   │
                                   ▼
                    ┌──────────────────────────┐
                    │ chatlog_ingest.py        │
                    │ (format parser)          │
                    └──────────────┬───────────┘
                                   │
                                   ▼
              ┌────────────────────────────────────────┐
              │ chatlog_core.py                        │
              │ - async queue (asyncio.Queue)          │
              │ - executemany + lazy embed             │
              │ - spill-to-disk fallback               │
              └────────────┬───────────────────────────┘
                           │
                ┌──────────┴──────────┐
                ▼                     ▼
    ┌──────────────────────┐ ┌──────────────────┐
    │ SQLite chatlog DB    │ │ SQLite main DB   │
    │ (separate/hybrid)    │ │ (integrated)     │
    └──────────────────────┘ └──────────────────┘
                │                     ▲
                │ (hybrid mode only)  │
                └─────────────────────┘
                        promote
```

## 2. Setup

### Let your agent install it

If you already have m3-memory wired up as an MCP server in Claude Code, Gemini CLI, or another agent, the fastest path is to ask the agent to do the install for you:

```
Install the m3-memory chat log subsystem.
```

The agent will run `bin/chatlog_init.py`, pick sensible defaults (separate mode, cost tracking on, redaction off), wire the PreCompact / session-end hook for whichever host it's running inside, install the 30-minute embed sweeper schedule, and smoke-test a write + search round-trip before reporting done. You can interject at any prompt to override defaults.

Skip to [§3 Daily Operations](#3-daily-operations) once it finishes.

### Manual install

Run the interactive setup wizard:

```bash
python bin/chatlog_init.py
```

This prompts you for:
- Storage mode (integrated, separate, hybrid)
- Custom DB path (if not separate)
- Which host agents to enable
- Cost tracking (on by default)
- Redaction settings (off by default)
- Whether to install the embed sweeper schedule

The configuration is saved to `memory/.chatlog_config.json`.

### Wiring Host Agent Hooks

Each hook ingests chat logs when the agent exits or checkpoints. Absolute paths are required.

#### Claude Code

Claude Code offers two capture triggers:

| Trigger      | Fires                                  | Default | When you want it                             |
| ------------ | -------------------------------------- | ------- | -------------------------------------------- |
| `PreCompact` | Only when the context is about to compact | **on**  | Always — this is the sensible default.      |
| `Stop`       | After every assistant turn              | off     | You want per-turn captures (at the cost of a Python spawn per turn). |

`PreCompact` alone is enough for most users: it fires whenever the session compacts **and** captures the full transcript up to that point. The `Stop` hook is opt-in because it fires per turn; the per-session UUID cursor in `chatlog_ingest.py` prevents duplicate rows, but each invocation still spawns Python and reads the transcript.

**Selecting the trigger**: the Stop hook is controlled by `host_agents.claude-code.stop_hook` in `memory/.chatlog_config.json`. Toggle it via:

```bash
python bin/chatlog_init.py --enable-stop-hook    # capture per-turn + at compact
python bin/chatlog_init.py --disable-stop-hook   # revert to PreCompact-only (default)
```

The toggle writes the config and prints an updated `~/.claude/settings.json` snippet for you to copy-paste (the subsystem does not auto-edit `settings.json`).

**Wiring**: add to `~/.claude/settings.json` (PreCompact only — remove the `Stop` block if `stop_hook=false`):

```json
{
  "hooks": {
    "PreCompact": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "powershell -NoProfile -ExecutionPolicy Bypass -File C:\\absolute\\path\\to\\m3-memory\\bin\\hooks\\chatlog\\claude_code_precompact.ps1"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "powershell -NoProfile -ExecutionPolicy Bypass -File C:\\absolute\\path\\to\\m3-memory\\bin\\hooks\\chatlog\\claude_code_precompact.ps1"
          }
        ]
      }
    ]
  }
}
```

On macOS/Linux, swap the `command` for `bash /absolute/path/to/m3-memory/bin/hooks/chatlog/claude_code_precompact.sh`.

Both events route through the **same** hook script; it reads `hook_event_name` from the Claude Code envelope and stamps rows with `variant="pre_compact"` or `variant="stop"` so you can distinguish them later. The event names here (`PreCompact`, `Stop`) are the canonical names from the [Claude Code hooks reference](https://code.claude.com/docs/en/hooks.md) — not the older `preCompaction` spelling seen in some earlier drafts.

### 2. Host Agent Wiring

Each host agent needs to be told to call the `m3-memory` ingest hook. The `chatlog_init.py` script provides specific instructions for each agent you enable.

#### Claude Code

Claude Code uses hooks in `~/.claude/settings.json`. The `chatlog_init.py` will print a JSON snippet you can copy-paste.

Both events (`PreCompact` and `Stop`) route through the **same** hook script; it reads `hook_event_name` from the Claude Code envelope and stamps rows with `variant="pre_compact"` or `variant="stop"` so you can distinguish them later.

#### Gemini CLI

Gemini CLI offers a single `SessionEnd` trigger that fires when the CLI exits (for any reason).

Register the hook in `~/.gemini/settings.json` using the absolute path to the repo's hook script (do **not** copy the script, as it needs to find `bin/chatlog_ingest.py` relative to the repo root):

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "powershell -NoProfile -ExecutionPolicy Bypass -File C:\\path\\to\\m3-memory\\bin\\hooks\\chatlog\\gemini_cli_onexit.ps1"
          }
        ]
      }
    ]
  }
}
```

On macOS/Linux, use the `.sh` wrapper:

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/bin/sh /path/to/m3-memory/bin/hooks/chatlog/gemini_cli_onexit.sh"
          }
        ]
      }
    ]
  }
}
```

The wrapper encodes the CLI's exit `reason` into the row `variant` (`session_end_exit`, `session_end_clear`, `session_end_logout`, etc.) so you can tell an intentional quit from a `/clear` or a forced logout later.

#### OpenCode

Copy the hook to OpenCode's session-end hook directory:

```bash
cp /absolute/path/to/m3-memory/bin/hooks/chatlog/opencode_session_end.sh ~/.opencode/hooks/session_end
chmod +x ~/.opencode/hooks/session_end
```

#### Aider

Start the long-running watcher (typically in tmux or systemd):

```bash
python /absolute/path/to/m3-memory/bin/hooks/chatlog/aider_chat_watcher.sh <repo-root>
```

This polls `.aider.chat.history.md` every 30 seconds and sends new messages to the ingest pipeline.

### Installing the Embed Sweeper Schedule

The embed sweeper runs every 30 minutes, embedding chat logs written with `embed=False` and draining spill-to-disk files:

```bash
python bin/install_schedules.py --add chatlog-embed-sweep
```

### Optional: Wiring Status Line into Claude Code

Add a status line command to `~/.claude/settings.json` to show chat log health in the Claude Code status bar:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python /absolute/path/to/m3-memory/bin/chatlog_status_line.py"
  }
}
```

This displays a quiet indicator (no output when healthy) and a short warning tag if any anomalies are detected (regex errors, silent hook, spill files, queue depth >80%, embed backlog).

## 3. Daily Operations

### Check Subsystem Status

```bash
python bin/chatlog_status.py
```

Returns JSON (or human-readable format with `--json`):

```json
{
  "mode": "hybrid",
  "effective_db_path": "/path/to/agent_chatlog.db",
  "main_db_path": "/path/to/agent_memory.db",
  "chatlog_rows": 1250,
  "main_chat_log_rows": 45,
  "chatlog_without_embed": 203,
  "queue_depth": 12,
  "spill_files": 0,
  "queue_spill_count": 0,
  "last_flush_at": "2026-04-18T14:30:45Z",
  "last_embed_sweep": "2026-04-18T14:00:30Z",
  "redaction_enabled": false,
  "host_agents": {
    "claude-code": {"enabled": true, "last_seen": "2026-04-18T14:35:10Z"},
    "gemini-cli": {"enabled": false},
    "opencode": {"enabled": true, "last_seen": "2026-04-18T14:32:00Z"},
    "aider": {"enabled": false}
  }
}
```

### Search Chat History

Use the `chatlog_search` MCP tool:

```python
# MCP call
chatlog_search(
    query="how to deploy kubernetes",
    k=5,
    model_filter="claude-opus-4-7",
    conversation_filter=None
)
```

Returns scored results with full metadata. In separate/hybrid mode, FTS5 search is fast; in integrated mode, vector search includes embeddings if available.

### Promote a Useful Conversation

In hybrid mode, move or copy a chat log entry to the main `agent_memory.db`:

```python
# MCP call
chatlog_promote(
    conversation_id="conv-abc123",
    memory_id="mem-xyz789",
    copy=True  # True = copy, False = move
)
```

Result is inserted into main DB with type `chat_log`; original remains in separate DB (if copy=True).

### Cost Report

Aggregate tokens and costs by model or date:

```bash
python bin/chatlog_core.py --cost-report [--group-by model_id|host_agent|date]
```

Returns:

```json
{
  "summary": {
    "total_tokens_in": 1245600,
    "total_tokens_out": 89340,
    "total_cost_usd": 14.23
  },
  "by_model": {
    "claude-opus-4-7": {
      "tokens_in": 500000,
      "tokens_out": 45000,
      "cost_usd": 9.75
    },
    "gemini-2.5-flash": {
      "tokens_in": 745600,
      "tokens_out": 44340,
      "cost_usd": 4.48
    }
  }
}
```

Cost computation uses a built-in price table; if provider/model not in table, cost_usd is null (no fake zeros).

## 4. Redaction

Redaction is **OFF by default** (local-first system; opt-in by choice). When enabled, chat content is scanned with pre-compiled regex patterns and secrets are replaced with `[REDACTED:<group>]`.

### Enable Redaction

Option A: During init:

```bash
python bin/chatlog_init.py
# Answer "yes" to "Enable redaction?"
```

Option B: After the fact (via MCP tool or CLI):

```bash
python bin/chatlog_core.py --set-redaction true \
  --patterns api_keys,bearer_tokens,jwt,github_tokens
```

Or use the MCP tool:

```python
chatlog_set_redaction(
    enabled=True,
    patterns=["api_keys", "bearer_tokens", "jwt", "aws_keys", "github_tokens"],
    redact_pii=False,  # Separate toggle for PII (emails, SSNs, etc.)
    store_original_hash=True  # Keep SHA-256 of pre-scrub content
)
```

### Pattern Groups

Built-in patterns (can be mixed and matched):

- `api_keys`: Anthropic, OpenAI, Google, XAI, Azure API keys
- `bearer_tokens`: "Bearer ..." style tokens
- `jwt`: JWT tokens (ey...)
- `aws_keys`: AWS access/secret keys
- `github_tokens`: GitHub PAT and OAuth tokens
- `pii`: Email addresses, phone numbers, SSNs (if `redact_pii=true`)

Add custom regex patterns:

```python
chatlog_set_redaction(
    enabled=True,
    patterns=["api_keys", "bearer_tokens"],
    custom_regex=[
        r"my_secret_\d+",
        r"password\s*=\s*['\"]?([^'\"]+)['\"]?"
    ]
)
```

### Re-scrub Existing Rows

If you turn redaction on after chat logs already exist, use the CLI to re-scrub:

```bash
python bin/chatlog_core.py --rescrub [--since 2026-04-01T00:00:00Z]
```

This updates all rows (or those since a date) with the current redaction policy. Original hashes are preserved in metadata for auditing.

## 5. Observability

### Status Summary

```bash
python bin/chatlog_status.py
```

Single call (<50ms) returns:
- Mode and DB paths
- Row counts (main and separate)
- Queue depth and spill file count
- Last flush and embed sweep timestamps
- Redaction state
- Per-host-agent status (enabled, last seen)

### Anomaly-Only Status Line

The status line (if wired into Claude Code) is quiet when healthy and displays a short warning tag when any of these fire:

1. Regex compilation error in redaction patterns
2. Silent hook (host agent enabled but no activity for >60 min)
3. Spill files present (queue overflowed to disk)
4. Queue depth >80% of max (backpressure building)
5. Embed backlog >20% of rows (embeddings not keeping up)

### State File

`memory/.chatlog_state.json` tracks:

```json
{
  "last_flush_at": "2026-04-18T14:30:45Z",
  "last_flush_count": 87,
  "last_embed_sweep": "2026-04-18T14:00:30Z",
  "last_embed_count": 156,
  "queue_spill_count": 0,
  "spill_files": [],
  "redaction_errors": [],
  "silent_hooks": []
}
```

Atomically written by the flush loop, sweeper, and hooks.

## 6. Architecture Notes

### Zero-Latency Writes

Messages enqueued to an `asyncio.Queue` (default max 20,000 rows). The flush loop drains on size (default 200 rows) or time (default 1.5 seconds), then writes to SQLite with `executemany`.

Embedding is lazy by default (`embed=False` at write time). The embed sweeper runs every 30 minutes, picks up unbedded rows, and embeds in batches using the shared embed server.

### Spill-to-Disk Fallback

If the queue fills (backpressure), rows are written to `memory/chatlog_spill/YYYYMMDD.jsonl` (one JSONL file per day). The sweeper drains spill files on the next run, ensuring no loss.

### Provenance Tracking

Every row has four required fields (rejects writes missing any):

- `host_agent`: Client identifier (claude-code, gemini-cli, opencode, aider)
- `provider`: LLM provider (anthropic, google, openai, xai, deepseek, mistral, meta, other, local)
- `model_id`: Model name or version
- `conversation_id`: Unique chat thread ID

Additional fields in metadata_json:

- `role`: user, assistant, system, tool
- `tokens_in`, `tokens_out`: Token counts (from hook or client-side)
- `cost_usd`: USD cost (computed client-side or server-side)
- `latency_ms`: Round-trip latency
- `source_file`: Original log file (for ingest tracing)

### Promotion (Separate/Hybrid Modes)

`chatlog_promote` uses SQLite `ATTACH DATABASE` to cross-DB copy or move rows. Relationships and metadata are preserved; embed vectors are lazy-embedded in the main DB on next sweeper run.

## 7. Schema Reference

### memory_items

Chat log entries live in `memory_items` with `type = 'chat_log'`. Key columns:

| Column | Type | Notes |
|--------|------|-------|
| id | TEXT | UUID primary key |
| type | TEXT | Always 'chat_log' for chat log entries |
| title | TEXT | First 80 chars of content (optional) |
| content | TEXT | Full message body (up to 50,000 chars) |
| metadata_json | TEXT | Role, provider, model, cost, tokens, etc. |
| agent_id | TEXT | chat_log host_agent identifier |
| model_id | TEXT | Model name |
| conversation_id | TEXT | Chat thread ID (required) |
| created_at | TEXT | ISO 8601 timestamp |
| updated_at | TEXT | Last edit timestamp |
| content_hash | TEXT | SHA-256 of original content (pre-redaction) |
| scope | TEXT | Usually 'agent' (local-first) |
| source | TEXT | 'agent' (ingest source) |
| is_deleted | INTEGER | 0 = live, 1 = soft-deleted |

### Indexes

Migration 002 adds:

- `idx_chatlog_type_created`: `(type, created_at) WHERE is_deleted = 0`
- `idx_chatlog_conversation`: `(conversation_id, created_at) WHERE type = 'chat_log'`
- Composite index on `(conversation_id, created_at)` for fast conversation replay

### memory_embeddings

One row per embedded chat log:

| Column | Type | Notes |
|--------|------|-------|
| id | TEXT | UUID primary key |
| memory_id | TEXT | Foreign key to memory_items.id |
| embedding | BLOB | 1024-dim float32 vector |
| embed_model | TEXT | Usually 'jina-embeddings-v5' |
| dim | INTEGER | Always 1024 |
| created_at | TEXT | ISO 8601 timestamp |

## 8. Troubleshooting

### "Writes feel slow"

Check queue depth and spill:

```bash
python bin/chatlog_status.py --json | jq '.queue_depth, .spill_files'
```

- If `queue_depth` is consistently >5000, increase `queue_flush_rows` in config (e.g., 500).
- If spill files are present, the embed sweeper is backed up; check sweeper logs.

### "Search returns nothing"

In separate/hybrid mode, search is FTS5-only (no vector embeddings). Check:

1. Mode: `python bin/chatlog_status.py | grep mode`
2. Row count: `python bin/chatlog_status.py | grep chatlog_rows`
3. Embed backlog: `chatlog_status.py | grep without_embed`

If backlog is high, wait for the next embed sweep or manually run:

```bash
python bin/chatlog_embed_sweeper.py --force
```

### "Promote failed"

Verify DB paths and schema:

```bash
python bin/migrate_memory.py status --target main
python bin/migrate_memory.py status --target chatlog
```

If migrations are out of sync, run:

```bash
python bin/migrate_memory.py migrate --target chatlog
python bin/migrate_memory.py migrate --target main
```

### "I want to turn it all off"

Disable all host agents in the config:

```bash
python bin/chatlog_init.py --disable-all
```

Or edit `memory/.chatlog_config.json`:

```json
{
  "host_agents": {
    "claude-code": {"enabled": false},
    "gemini-cli": {"enabled": false},
    "opencode": {"enabled": false},
    "aider": {"enabled": false}
  }
}
```

Remove the embed sweeper schedule:

```bash
python bin/install_schedules.py --remove chatlog-embed-sweep
```

The DBs and config remain intact for future re-enablement.

### "Redaction is too noisy" or "I need more/fewer patterns"

Adjust the redaction config:

```bash
python bin/chatlog_core.py --set-redaction true \
  --patterns api_keys,github_tokens \
  --no-pii
```

Or toggle off:

```bash
python bin/chatlog_core.py --set-redaction false
```

Existing redacted content is not un-redacted (hashes are stored for audit). To change policy retroactively, use `--rescrub`.

## 9. Development & Debugging

### Testing the Ingest Pipeline

The ingest CLI reads a real transcript file (not stdin); to smoke-test, point it at one of the fixture transcripts under `tests/fixtures/`:

```bash
python bin/chatlog_ingest.py \
  --format claude-code \
  --transcript-path tests/fixtures/claude_code_sample.jsonl \
  --variant test
```

Or for Gemini:

```bash
python bin/chatlog_ingest.py \
  --format gemini-cli \
  --transcript-path tests/fixtures/gemini_session_sample.json \
  --variant test
```

A per-session UUID cursor at `memory/.chatlog_ingest_cursor.json` makes re-runs idempotent; delete the session's entry if you want to re-ingest the same transcript.

Check that it landed:

```bash
python bin/chatlog_status.py
```

### Checking Hook Logs

Hook stdout/stderr are logged to:
- Claude Code: `~/.claude/logs/`
- Gemini CLI: `~/.gemini/logs/` (if configured)
- OpenCode: `~/.opencode/logs/`
- Aider: tmux session or systemd journal

To debug a hook manually, feed it a real envelope on stdin:

```bash
echo '{"session_id":"debug","transcript_path":"/abs/path/to/session.jsonl","hook_event_name":"PreCompact","cwd":"."}' \
  | bash /path/to/m3-memory/bin/hooks/chatlog/claude_code_precompact.sh
```

On Windows:

```powershell
Get-Content -Raw envelope.json |
  powershell -NoProfile -ExecutionPolicy Bypass -File ...\claude_code_precompact.ps1
```

### Inspecting Spill Files

```bash
ls memory/chatlog_spill/
cat memory/chatlog_spill/20260418.jsonl | head -3 | jq .
```

Each line is a complete chat message in JSON format.

### Resetting the Subsystem (for testing)

```bash
# Soft reset: clear rows but keep schema
sqlite3 memory/agent_chatlog.db "DELETE FROM memory_items WHERE type='chat_log';"

# Hard reset: drop and re-bootstrap
rm memory/agent_chatlog.db memory/.chatlog_state.json
python bin/migrate_memory.py migrate --target chatlog
```

Then re-ingest:

```bash
python bin/chatlog_ingest.py --format claude-code < /path/to/test.jsonl
```
