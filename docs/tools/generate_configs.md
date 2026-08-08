---
tool: bin/generate_configs.py
sha1: 58487d1efe55
mtime_utc: 2026-08-07T23:53:52.048400+00:00
generated_utc: 2026-08-08T14:40:49.863483+00:00
private: false
---

# bin/generate_configs.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--install-claude` | Merge hooks+statusLine into ~/.claude/settings.json | `False` |  | store_true |  |
| `--settings-path` | Override target settings.json path | None |  | str |  |
| `--yes` | Apply without prompting | `False` |  | store_true |  |
| `--dry-run` | Show the diff but write nothing | `False` |  | store_true |  |
| `--keep-status-line` | Don't replace an existing custom status line (default is to adopt m3's statusline-command.sh, preserving the prior one to a timestamped sidecar file) | `False` |  | store_true |  |

---

## Environment variables read

- `M3_CONFIG_ROOT`
- `M3_EMBED_GGUF`
- `M3_ENGINE_ROOT`
- `M3_MEMORY_ROOT`

---

## Calls INTO this repo (intra-repo imports)

- `chatlog_config`
- `m3_memory.embedder_admin (seed_shared_config)`
- `m3_memory.installer (bin_dir)`
- `m3_sdk (get_m3_config_root, get_m3_engine_root, get_m3_root)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `difflib`
- `ntpath`
- `posixpath`

---

## File dependencies (repo paths referenced)

- `.aider.conf.yml`
- `.mcp.json`
- `Merge hooks+statusLine into ~/.claude/settings.json`
- `claude-settings.json`
- `gemini-settings.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
