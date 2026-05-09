---
tool: bin/m3_cognitive_loop.py
sha1: 3272ce41604b
mtime_utc: 2026-05-07T03:32:14.556216+00:00
generated_utc: 2026-05-09T13:54:34.296979+00:00
private: false
---

# bin/m3_cognitive_loop.py

## Purpose

m3_cognitive_loop — The autonomous heartbeat of m3-memory.

This script unifies the Observer, Reflector, and Entity Extractor into a
single continuous "live" pipeline. It monitors the core memory and chatlog
DBs for new content and automatically performs:
  1. Entity Extraction (Linking facts into the knowledge graph)
  2. Observation Extraction (Extracting high-signal user-facts/preferences)
  3. Reflection (Merging/superseding facts, resolving contradictions)
  4. Temporal Resolution (Normalizing relative dates like 'yesterday')

Usage:
  python bin/m3_cognitive_loop.py --interval 60  # Run every 60 seconds

When M3_AUTO_ENRICH is ON, this replaces the need for separate cron jobs
for m3_enrich and m3_entities.

---

## Entry points

- `def main()` (line 295)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--interval` | Seconds between passes (default: 300) | `300` |  | int |  |
| `--background` | Run in background (fire and forget) | `False` |  | store_true |  |
| `--concurrency` | SLM concurrency (default: 2) | `2` |  | int |  |
| `--limit-per-pass` | Max groups/rows per pass (default: 50) | `50` |  | int |  |
| `--database` | Core Memory DB path (Env: M3_DATABASE) | None |  | str |  |
| `--chatlog-db` | Chatlog DB path (Env: CHATLOG_DB_PATH) | None |  | str |  |
| `--profile-entities` | Profile for entities | `entities_local_qwen` |  | str |  |
| `--profile-enrich` | Profile for enrichment | `enrich_local_qwen` |  | str |  |
| `--reflector-threshold` | Min observations before Reflector (default: 5) | `5` |  | int |  |
| `--skip-entities` | Skip entity extraction | `False` |  | store_true |  |
| `--skip-enrich` | Skip enrichment pass | `False` |  | store_true |  |
| `--no-reflect` | Skip reflection pass | `False` |  | store_true |  |

---

## Environment variables read

- `CHATLOG_DB_PATH`
- `M3_DATABASE`

---

## Calls INTO this repo (intra-repo imports)

- `chatlog_config`
- `m3_enrich`
- `m3_entities`
- `m3_sdk (M3Context, resolve_db_path)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.Popen()  → `argv`` (line 55)


---

## Notable external imports

- `atexit`
- `ctypes`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
