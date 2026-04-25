---
name: m3:search
description: Hybrid search across memory (FTS5 + semantic vector + MMR diversity rerank).
argument-hint: <query>
---

Use the `m3-memory:memory_search` MCP tool with `query="$ARGUMENTS"` and `k=8`. Default scope.

After it returns, present the results as a numbered list:

```
1. <title> — <type> — score=<float>
   <first 200 chars of content>
   id: <uuid-prefix>
```

If the user wants to drill into one, suggest `/m3:get <id-prefix>`.
If results look stale or contradictory, suggest `/m3:forget <id>` for stale entries or `/m3:save <correct-fact>` to add the new state (contradiction detection will supersede automatically).
