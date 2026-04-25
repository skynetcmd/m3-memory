---
name: status
description: Chatlog subsystem status — row counts, queue depth, spill, last capture, hook health.
---

Step 1 — run via the Bash tool, trying these resolvers in order. Stop at the first that returns exit 0:

```
mcp-memory chatlog status                                # 1. plain CLI
python -m m3_memory.cli chatlog status                    # 2. module form (Windows --user case)
.venv/Scripts/python.exe -m m3_memory.cli chatlog status  # 3. repo venv (Windows)
.venv/bin/python -m m3_memory.cli chatlog status          # 3. repo venv (macOS/Linux)
```

Step 2 — print the table verbatim.

Step 3 — append exactly ONE line of interpretation: capture rate, hook health, or any explicit warning the table reported.
