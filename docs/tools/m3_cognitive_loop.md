---
tool: bin/m3_cognitive_loop.py
sha1: c7bbcaf1a6a0
mtime_utc: 2026-05-04T22:27:33.029963+00:00
generated_utc: 2026-05-04T22:28:45.574165+00:00
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

- `def main()` (line 256)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--interval` | f'Seconds between passes (default: {default_interval})' | `default_interval` |  | int |  |
| `--concurrency` | SLM concurrency (default: 2) | `2` |  | int |  |
| `--limit-per-pass` | Max groups/rows per pass (default: 50) | `50` |  | int |  |
| `--profile-entities` | Profile for entities | `entities_local_qwen` |  | str |  |
| `--profile-enrich` | Profile for enrichment | `enrich_local_qwen` |  | str |  |
| `--reflector-threshold` | Min observations before Reflector (default: 5) | `5` |  | int |  |
| `--skip-entities` | Skip entity extraction | `False` |  | store_true |  |
| `--skip-enrich` | Skip enrichment pass | `False` |  | store_true |  |
| `--no-reflect` | Skip reflection pass | `False` |  | store_true |  |

---

## Environment variables read

- `M3_COGNITIVE_LOOP_INTERVAL`

---

## Calls INTO this repo (intra-repo imports)

- `m3_enrich`
- `m3_entities`
- `m3_sdk (M3Context)`
- `memory_core`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

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
