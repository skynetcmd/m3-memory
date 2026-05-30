---
name: m3-forget
description: Delete a memory permanently. Asks for confirmation first.
---
# M3 Forget

## When to Use
Use this skill when the user wants to delete a specific memory from their store. Always verify and confirm the deletion to prevent accidental loss of important information.

## Instructions
# Confirm before delete

Step 1 — fetch the target via `m3:memory_get` with `id="$ARGUMENTS"`. Show the user:

```
About to delete:
  id:    <uuid>
  title: <title>
  type:  <type>
  content: <first 200 chars>
```

Step 2 — ask: "Type the first 8 chars of the id to confirm deletion, anything else to abort."

Step 3 — on match, call `m3:memory_delete` with the full id. On mismatch, abort and say "skipped — id mismatch."

If the user wants a softer alternative, mention `m3:gdpr_forget` (Article 17 — sets a tombstone instead of hard delete) or just writing a superseding memory via `/m3:save`.
