# M3 Memory — Agent Rules

> Drop this file into your project root (or reference it in your agent config)
> to give your AI agent persistent memory via M3 Memory.

## Rules

1. **Search before answering.** Before responding to any question about past decisions, preferences, project details, or previously discussed facts, call `memory_search` first.

2. **Write immediately.** When you learn something worth remembering — a decision, preference, fact, config detail, or observation — call `memory_write` right away. Don't wait.

3. **Update, don't duplicate.** If a fact changes, use `memory_supersede` on the existing memory — it closes the old fact and links the new one, preserving history. Use `memory_update` only to correct a typo, metadata, or refresh fields (it overwrites in place).

4. **Use the right type.** Pick the most specific type: `fact`, `decision`, `preference`, `observation`, `config`, `code`, `task`, `note`, `summary`, `plan`, `knowledge`, or `auto` (lets the system classify it). Other types exist for specialized workflows — see the full list in [AGENT_INSTRUCTIONS.md](https://github.com/skynetcmd/m3-memory/blob/main/docs/AGENT_INSTRUCTIONS.md).

5. **Explore connections.** Use `memory_graph` to traverse related memories when you need broader context (up to 3 hops).

6. **Look up entities directly.** When a user asks about a specific person, place, or thing, try `entity_search` and `entity_get` for cleaner results than free-text `memory_search`.

---

## Core tools

| Tool | When to use |
|------|-------------|
| `memory_search` | Before answering context-dependent questions |
| `memory_write` | To store new facts, decisions, or observations |
| `memory_supersede` | When a fact has changed — replaces the old memory with a new one, keeping history |
| `memory_update` | To fix a typo, metadata, or refresh fields on an existing memory (in place) |
| `memory_get` | To retrieve a specific memory by ID |
| `memory_suggest` | Like search, but returns score breakdowns |
| `memory_graph` | To explore connections between memories |
| `memory_link` | To manually connect two related memories |
| `entity_search` / `entity_get` | To look up people, places, or things by name |
| `memory_delete` | When a user asks you to forget a specific memory |
| `gdpr_forget` | Only when a user asks to erase ALL their data — hard-deletes everything for that `user_id` |
| `gdpr_export` | When a user asks for a portable copy of their memories |

---

## Quick test

After setup, verify it works:

```
Write a memory: "M3 Memory is active in this project"
Then search for: "M3 Memory"
```

For the full tool reference (all 100+ tools, parameters, and behaviors), see [AGENT_INSTRUCTIONS.md](https://github.com/skynetcmd/m3-memory/blob/main/docs/AGENT_INSTRUCTIONS.md). The full inventory with parameter schemas lives at [docs/MCP_TOOLS.md](https://github.com/skynetcmd/m3-memory/blob/main/docs/MCP_TOOLS.md).
