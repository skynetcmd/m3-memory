---
name: doctor
description: Health check — package version, installed payload, chatlog DB row count, per-agent hook state.
---

Step 1 — run `mcp-memory doctor` via the Bash tool:

```
mcp-memory doctor
```

Step 2 — print the full doctor output verbatim (do not summarize the contents into prose; show the user what doctor printed).

Step 3 — beneath the output, add a one-line interpretation:
- if everything reads `[OK]` and hooks are `[on]`: say "all healthy."
- if any line reads `[X]` or `(never)` for last capture: name the specific issue and suggest the fix command (`/m3:install`, `mcp-memory chatlog init --apply-claude`, etc.).
