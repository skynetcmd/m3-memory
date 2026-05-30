---
name: m3-search
description: Hybrid memory search — FTS5 + semantic vector + MMR diversity rerank.
---
# M3 Search

## When to Use
Use this skill when the user wants to search their memories, find information they previously saved, check past conventions or decisions, or resolve queries involving prior context.

## Instructions
Call the `m3:memory_search` MCP tool with `query="$ARGUMENTS"` and `k=8`.

Render the results as a numbered list:

```
1. <title> — <type> — score=<float>
   <first 200 chars of content>
   id: <uuid-prefix>
```

If the user wants to drill into one, suggest `/m3:get <id-prefix>`.
If results look stale or contradictory, suggest `/m3:forget <id>` for stale entries or `/m3:save <correct-fact>` to add the new state (contradiction detection will supersede automatically).
