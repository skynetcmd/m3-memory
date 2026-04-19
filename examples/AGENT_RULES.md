# M3 Memory — Agent Rules

> Drop this file into your project root (or reference it in your agent config)
> to give your AI agent persistent memory via M3 Memory.

## Rules

1. **Search before answering.** Before responding to any question about past decisions, preferences, project details, or previously discussed facts, call `memory_search` first.

2. **Write immediately.** When you learn something worth remembering — a decision, preference, fact, config detail, or observation — call `memory_write` right away. Don't wait.

3. **Update, don't duplicate.** If a fact changes, use `memory_update` on the existing memory. M3 detects contradictions automatically and preserves history.

4. **Use the right type.** Pick the most specific type: `fact`, `decision`, `preference`, `observation`, `config`, `code`, `task`, `note`, or `auto` (lets the system classify it).

5. **Explore connections.** Use `memory_graph` to traverse related memories when you need broader context (up to 3 hops).

## Core tools

| Tool | When to use |
|------|-------------|
| `memory_search` | Before answering context-dependent questions |
| `memory_write` | To store new facts, decisions, or observations |
| `memory_update` | To correct or refine an existing memory |
| `memory_get` | To retrieve a specific memory by ID |
| `memory_suggest` | Like search, but returns score breakdowns |
| `memory_graph` | To explore connections between memories |
| `memory_link` | To manually connect two related memories |
| `gdpr_forget` | When a user asks you to forget something |

## Quick test

After setup, verify it works:

```
Write a memory: "M3 Memory is active in this project"
Then search for: "M3 Memory"
```

For the full tool reference (all 66 tools, parameters, and behaviors), see [AGENT_INSTRUCTIONS.md](../AGENT_INSTRUCTIONS.md).
