---
tool: bin/auto_route.py
sha1: 1a66b3822d9a
mtime_utc: 2026-04-26T12:39:14.741294+00:00
generated_utc: 2026-05-01T13:05:26.707128+00:00
private: false
---

# bin/auto_route.py

## Purpose

auto_route — multi-signal retrieval branch decider.

Pre-retrieval signals (from query text):
- has_temporal_cues(query): regex on temporal keywords + date patterns
- has_comparison_cues(query): regex on count/aggregation keywords
- count_named_entities(query): count of capitalized multi-word proper-noun phrases

Post-retrieval signals (from candidate list):
- top_1_score(candidates)
- slope_at_3(candidates)
- conv_id_diversity(candidates)

Branch decision (first match wins):
1. temporal  — if has_temporal_cues(query)
2. multi_session — if has_comparison_cues(query) OR conv_id_diversity > threshold
3. sharp — if top_1 > sharp_min AND slope_at_3 > sharp_slope_min
4. entity_anchored — if count_named_entities(query) >= threshold AND auto_entity_graph_enabled
5. default — fallback (no values set; pure pass-through to caller defaults)

API:
- decide_branch(query, candidates, params) -> str (branch name)
- branch_values(branch, params) -> dict[str, Any]  (parameter values for this branch)
- signals_summary(query, candidates) -> dict  (all signals as a dict for capture)
- count_named_entities(query) -> int  (count of proper-noun phrases in query)

---

## Entry points

_(no conventional entry point detected)_

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
