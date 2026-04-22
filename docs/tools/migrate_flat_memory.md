---
tool: bin/migrate_flat_memory.py
sha1: 943ef6d47ced
mtime_utc: 2026-04-21T20:45:50.811588+00:00
generated_utc: 2026-04-21T21:26:01.928047+00:00
private: false
---

# bin/migrate_flat_memory.py

## Purpose

migrate_flat_memory.py — one-way ETL from flat-file / SQLite agent memory
into the m3-memory MCP server.

Supported sources:
    claude    ~/.claude/projects/<slug>/memory/*.md  (YAML frontmatter)
    gemini    ~/.gemini/GEMINI.md                    (## Gemini Added Memories bullets)
    openclaw  ~/.openclaw/memory/main.sqlite         (read-only, chunks table)
    rules     CLAUDE.md / GEMINI.md / AGENTS.md / CONVENTIONS.md  (opt-in)

Idempotent: each source item gets a stable `source_key` stored in metadata.
Re-runs skip items whose `source_key` already exists in m3-memory.

Verification: after writing, each new memory is round-tripped through
memory_get_impl — content and SHA-256 hash must match before it counts as
migrated. Verified items are listed for manual cleanup at the end; this
script never deletes source files.

Usage:
    python bin/migrate_flat_memory.py --dry-run
    python bin/migrate_flat_memory.py
    python bin/migrate_flat_memory.py --sources claude,gemini
    python bin/migrate_flat_memory.py --include-rules
    python bin/migrate_flat_memory.py --claude-project-slug C--Users-bhaba-m3-memory

## Entry points

- `async def run()` (line 470)
- `def main()` (line 611)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--sources` | Comma-separated list of sources. Valid: claude, gemini, openclaw, rules. Default: claude,gemini,openclaw | `claude,gemini,openclaw` | Migrates from claude, gemini, and openclaw sources (rules excluded). | str | Scans only specified sources; rules included only if explicitly set. |
| `--include-rules` | Also import CLAUDE.md / GEMINI.md / AGENTS.md / CONVENTIONS.md as type=preference. These are behavioral rules loaded by each agent's system prompt — importing them makes them searchable in m3-memory but does NOT replace the source files. | `False` | Rules files not migrated. | store_true | Adds "rules" to source list; imports behavior rule files as type=preference. |
| `--claude-project-slug` | Restrict Claude source to a single project slug under ~/.claude/projects/. Default: all projects. | None | Scans all projects under ~/.claude/projects/. | str | Restricts Claude scanner to single project; bypasses project enumeration. |
| `--dry-run` | Discover + plan but don't write. | `False` | Discovers items, dedupes, prompts for confirmation, then writes to m3-memory. | store_true | Prints plan but skips all writes; no confirmation prompt required. |
| `-y`, `--yes` | Skip confirmation prompt. | `False` | Confirms migration interactively before writing. | store_true | Bypasses confirmation prompt; proceeds directly to write phase. |
| `-v`, `--verbose` | DEBUG logging. | `False` | INFO-level logs to stderr. | store_true | Sets log level to DEBUG; verbose output for development/debugging. |
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None | Falls back to M3_DATABASE env then memory/agent_memory.db. | str | Routes all DB reads/writes against PATH for this run. |

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (add_database_arg)`
- `memory_core`
- `memory_core (_db)`

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `uri`` (line 308)


## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

- `.aider.conf.yml`
- `AGENTS.md`
- `CLAUDE.md`
- `CONVENTIONS.md`
- `GEMINI.md`
- `MEMORY.md`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
