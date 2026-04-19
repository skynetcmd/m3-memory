---
tool: bin/chatlog_init.py
sha1: 1a40d84d611c
mtime_utc: 2026-04-18T22:29:31.722838+00:00
generated_utc: 2026-04-19T00:39:15.961981+00:00
private: false
---

# bin/chatlog_init.py

## Purpose

chatlog_init.py — interactive setup CLI for the chat log subsystem.

Guides the user through:
  - Choosing a mode (separate, integrated, or hybrid)
  - Setting DB path (if separate/hybrid)
  - Enabling host agents and showing wiring instructions
  - Configuring cost tracking and redaction
  - Running migrations and installing schedules
  - Showing Claude Code settings snippet

## Entry points

- `def main()` (line 291)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--reconfigure` | Reconfigure even if config exists | `False` | Shows existing config and exits | store_true | Re-runs full setup wizard even if CONFIG_PATH exists |
| `--non-interactive` | Use defaults, skip prompts and post-setup steps | `False` | Interactive setup with all prompts and post-setup steps | store_true | Skips all prompts, uses defaults (mode=separate, cost_tracking=on, redaction=off), skips migrations and schedules |
| `--mode` | Deployment mode (separate, integrated, hybrid) | None | If not provided, prompts interactively via interactive_mode() | str | Uses specified mode; ignored in interactive flow if --non-interactive is absent |
| `--db-path` | Database path (for separate/hybrid mode) | None | If not provided, prompts via interactive_db_path(mode) or uses DEFAULT_DB_PATH | str | Uses specified path; skips path validation for custom paths in interactive mode |

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `chatlog_config (CONFIG_PATH, DEFAULT_DB_PATH, MAIN_DB_PATH, VALID_HOST_AGENTS, VALID_MODES, ChatlogConfig, CostTrackingSpec, EmbedSweeperSpec, HookSpec, RedactionSpec, resolve_config, save_config)`

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `[sys.executable, install_script, '--add', 'chatlog-embed-sweep']`` (line 227)
- `subprocess.run()  → `[sys.executable, migrate_script, 'up', '--target', 'chatlog', '-y']`` (line 202)


## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
