---
name: get
description: Fetch one memory by UUID or short prefix.
argument-hint: <id-or-prefix>
---

Call `m3:memory_get` with `id="$ARGUMENTS"`.

If the id is shorter than 36 chars (a UUID prefix), the tool will resolve it. If the prefix is ambiguous, surface the matches and ask the user to disambiguate.

Display: `id`, `title`, `type`, `scope`, `created_at`, `updated_at`, full `content`. If `metadata` is non-empty, render it as a small table.
