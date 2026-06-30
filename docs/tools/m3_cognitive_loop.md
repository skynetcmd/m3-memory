---
tool: bin/m3_cognitive_loop.py
sha1: b2a7ca7709e5
mtime_utc: 2026-06-30T21:32:48.329659+00:00
generated_utc: 2026-06-30T22:19:18.304960+00:00
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

- `def main()` (line 556)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--interval` | Seconds between passes (default: 300) | `300` |  | int |  |
| `--background` | Run in background (fire and forget) | `False` |  | store_true |  |
| `--log-file` | Append logging to this file (scheduled-task / service mode). Survives the Windows pythonw re-exec. | None |  | str |  |
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
| `--skip-consolidate` | Skip the belief-consolidation pass | `False` |  | store_true |  |
| `--consolidate-threshold` | Min same-type group size before consolidating (default: 50) | `50` |  | int |  |
| `--consolidate-stale-days` | Only consolidate items older than N days (default: 7) | `7` |  | int |  |
| `--consolidate-source-type` | Episodic source memory type to roll up (default: observation) | `observation` |  | str |  |
| `--skip-chatlog-prune` | Skip the chatlog noise-prune pass | `False` |  | store_true |  |
| `--chatlog-prune-threshold` | Min aged prune-eligible chat_log rows before a sweep (default: 2000) | `2000` |  | int |  |
| `--chatlog-prune-fresh-days` | Keep noise newer than N days untouched (default: 14) | `14.0` |  | float |  |
| `--chatlog-prune-days` | Soft-delete aged noise older than N days (default: 45) | `45.0` |  | float |  |
| `--chatlog-prune-max-actions` | Max decay+prune writes per cycle (default: 5000; 0 = no cap). Caps one pass so a backlog drains across cycles instead of blocking the heartbeat. | `5000` |  | int |  |

---

## Environment variables read

- `CHATLOG_DB_PATH`
- `M3_CHATLOG_PRUNE_AUTO`
- `M3_DATABASE`
- `M3_EMBED_MODEL`
- `M3_EMBED_URL`
- `M3_GOVERNOR_THROTTLED_LIMIT`

---

## Calls INTO this repo (intra-repo imports)

- `chatlog_config`
- `chatlog_prune`
- `consolidate_beliefs`
- `m3_enrich`
- `m3_entities`
- `m3_sdk (M3Context, ensure_governor_config, get_governor_pacing, resolve_db_path)`
- `slm_intent (load_profile)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.Popen()  → `argv`` (line 59)


---

## Notable external imports

- `atexit`
- `ctypes`
- `types (SimpleNamespace)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
