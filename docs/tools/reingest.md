---
tool: benchmarks/locomo/reingest.py
sha1: 3f8d1fbd5f82
mtime_utc: 2026-04-21T20:02:02.909000+00:00
generated_utc: 2026-05-01T13:05:27.176511+00:00
private: true
---

# benchmarks/locomo/reingest.py

## Purpose

Re-ingest LOCOMO samples with explicit variant tags.

Supports multiple variants in a single invocation so they share the in-process
LLM content-hash cache (memory_core._AUTO_TITLE_CACHE / _AUTO_ENTITIES_CACHE).
Running heuristic_c1c4 + llm_v1 + llm_only together means every unique turn's
LLM title / entities are computed once and reused.

Per-variant config is keyed by variant name in VARIANT_PRESETS. A variant
expressed on the CLI that isn't in presets is treated as heuristic-only.

---

## Entry points

- `async def main()` (line 41)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--samples` | Sample IDs to ingest (default: the four Phase 1 convs). | `['conv-26', 'conv-30', 'conv-41', 'conv-42']` | Ingests the four Phase 1 samples. | str | Ingests specified sample IDs (space-separated). |
| `--variants` | 'Variant names to produce in one process. Known presets: ' + ', '.join(VARIANT_PRESETS.keys()) + '. Unknown names default to heuristic-only.' | `['heuristic_c1c4']` | Produces heuristic_c1c4 variant only. | str | Produces specified variant(s); unknown names default to heuristic-only. |
| `--features` | Comma-separated metadata.features override. Empty uses variant defaults. | `` | Uses variant's default features. | str | Overrides feature list for all ingested items. |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `bench_locomo`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `locomo10.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
