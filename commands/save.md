---
name: save
description: Auto-classified save — picks type/scope automatically, confirms before writing.
argument-hint: <content>
---

# Suggested save

You're about to write to m3-memory. **Do not call memory_write yet.** First propose a plan:

1. Look at the content the user gave you (`$ARGUMENTS`) plus the last few turns of context.
2. Pick the most appropriate `type` from this list:
   - `decision` — choices made with a why
   - `fact` — verifiable assertion about the world
   - `preference` — user's stated like / dislike / convention
   - `note` — informal observation, doesn't fit other types
   - `task` — actionable item with state
   - `reference` — pointer to external doc / URL / location
   - `knowledge` — durable understanding of how something works
   - `observation` — what you noticed during a session
   - `summary` — distilled takeaway from a longer thread
3. Pick `scope` (default: `user`) — most personal facts/preferences are user-scoped; project knowledge often isn't.
4. Suggest a 1-line `title` if missing.
5. Show the user the proposed `{type, scope, title, content}` and ask for a single `y` to write, or any other key to abort.
6. On `y`: call `m3:memory_write` with those fields.
7. On abort: say "skipped" and stop.

Reasoning for the choices is fine to include but keep it short — the user wants speed, not a treatise.
