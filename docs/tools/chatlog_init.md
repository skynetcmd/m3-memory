---
tool: bin/chatlog_init.py
sha1: 8eb26c7df507
mtime_utc: 2026-05-01T09:14:47.217939+00:00
generated_utc: 2026-05-01T13:05:26.752279+00:00
private: false
---

# bin/chatlog_init.py

## Purpose

chatlog_init.py — interactive setup CLI for the chat log subsystem.

Guides the user through:
  - Choosing a chatlog DB path (defaults to a dedicated file; set it equal
    to the main DB to keep everything in one place)
  - Enabling host agents and showing wiring instructions
  - Configuring cost tracking and redaction
  - Running migrations and installing schedules
  - Showing Claude Code settings snippet

The prior integrated/separate/hybrid mode selection has been removed: the
same behaviors are now selected by setting the chatlog DB path equal to (or
different from) the main DB. Promote semantics switch automatically based on
path equality.

---

## Entry points

- `def main()` (line 590)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--reconfigure` | Reconfigure even if config exists | `False` | Shows existing config and exits | store_true | Re-runs full setup wizard even if CONFIG_PATH exists |
| `--non-interactive` | Use defaults, skip prompts and post-setup steps | `False` | Interactive setup with all prompts and post-setup steps | store_true | Skips all prompts, uses defaults (mode=separate, cost_tracking=on, redaction=off), skips migrations and schedules |
| `--db-path` | Chat log database path. Default: memory/agent_chatlog.db. Set equal to the main DB (memory/agent_memory.db) to keep all data in a single file. | None | If not provided, prompts via interactive_db_path(mode) or uses DEFAULT_DB_PATH | str | Uses specified path; skips path validation for custom paths in interactive mode |
| `--enable-stop-hook` | Enable per-turn capture via Claude Code's Stop hook in addition to PreCompact. Writes config and prints an updated settings.json snippet. Default is PreCompact-only. | `False` | PreCompact-only hook in Claude Code | store_true | Enables Stop hook; toggles stop_hook config, persists, re-prints settings.json snippet |
| `--disable-stop-hook` | Disable the Stop hook (revert to PreCompact-only capture). | `False` | PreCompact-only hook in Claude Code | store_true | Disables Stop hook; toggles stop_hook config, persists, re-prints settings.json snippet |
| `--apply-claude` | Merge chatlog hooks + statusLine into ~/.claude/settings.json (creates the file if missing, backs up before writing, idempotent). Without this flag, init prints the snippet for manual paste. | `False` |  | store_true |  |
| `--apply-gemini` | Add the SessionEnd chatlog hook to ~/.gemini/settings.json (idempotent, backs up before writing). Requires Gemini CLI to be installed first; the memory MCP entry is written by install-m3. | `False` |  | store_true |  |
| `--capture-mode` | Configure Claude Code Stop-hook policy in non-interactive mode. 'both' / 'stop' enable the Stop hook; 'precompact' / 'none' leave it disabled. Without this flag, non-interactive uses PreCompact-only. | None |  | str |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `chatlog_config (CONFIG_PATH, DEFAULT_DB_PATH, MAIN_DB_PATH, VALID_HOST_AGENTS, ChatlogConfig, CostTrackingSpec, EmbedSweeperSpec, HookSpec, RedactionSpec, resolve_config, save_config)`
- `m3_memory.installer (_fix_npm_global_path)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `[sys.executable, install_script, '--add', 'chatlog-embed-sweep']`` (line 261)
- `subprocess.run()  → `[sys.executable, migrate_script, 'up', '--target', 'chatlog', '-y']`` (line 236)
- `subprocess.run()  → `[sys.executable, migrate_script, 'up', '--target', 'chatlog', '-y']`` (line 714)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- ` section below to add hooks to ~/.claude/settings.json`
- `claude_code_precompact.sh`
- `gemini_cli_onexit.sh`
- `settings.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
