---
name: m3-get
description: Fetch one memory by UUID or short prefix.
---
# M3 Get

## When to Use
Use this skill when you need to retrieve the full, detailed record of a single memory by its UUID or a short ID prefix.

## Instructions
Call `m3:memory_get` with `id="$ARGUMENTS"`.

If the id is shorter than 36 chars (a UUID prefix), the tool will resolve it. If the prefix is ambiguous, surface the matches and ask the user to disambiguate.

Display: `id`, `title`, `type`, `scope`, `created_at`, `updated_at`, full `content`. If `metadata` is non-empty, render it as a small table.
