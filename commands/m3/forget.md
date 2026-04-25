---
name: m3:forget
description: Delete a memory permanently. Asks for confirmation first.
argument-hint: <id-or-prefix>
---

# Confirm before delete

Step 1 — fetch the target via `m3-memory:memory_get` with `id="$ARGUMENTS"`. Show the user:

```
About to delete:
  id:    <uuid>
  title: <title>
  type:  <type>
  content: <first 200 chars>
```

Step 2 — ask: "Type the first 8 chars of the id to confirm deletion, anything else to abort."

Step 3 — on match, call `m3-memory:memory_delete` with the full id. On mismatch, abort and say "skipped — id mismatch."

If the user wants a softer alternative, mention `m3-memory:gdpr_forget` (Article 17 — sets a tombstone instead of hard delete) or just writing a superseding memory via `/m3:save`.
