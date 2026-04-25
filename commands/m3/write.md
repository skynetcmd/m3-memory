---
name: m3:write
description: Direct memory_write — explicit, no auto-classification. Use /m3:save for context-aware writes.
argument-hint: <content>
---

Call `m3-memory:memory_write` with `content="$ARGUMENTS"`, `type="note"`, `scope="user"`.

Report the resulting id and any contradiction events (memory_write returns supersede info if the new memory invalidates an older one).

If the user wanted a different type (decision, fact, preference, etc.), suggest `/m3:save` for the auto-classified path.
