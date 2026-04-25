---
name: doctor
description: Health check — package version, installed payload, chatlog DB row count, per-agent hook state.
---

Step 1 — run the doctor command, trying the resolvers below in order. Stop at the first one that returns exit 0; do not run the remaining ones.

```
# 1. Plain CLI, if mcp-memory is on PATH:
mcp-memory doctor

# 2. Module form, works whenever the m3_memory package is importable
#    (catches pip install --user on Windows where the Scripts dir
#    isn't on PATH, and any `pip install -e` dev checkout):
python -m m3_memory.cli doctor

# 3. Repo-local venv (developer case, run from the repo root):
.venv/Scripts/python.exe -m m3_memory.cli doctor   # Windows
.venv/bin/python -m m3_memory.cli doctor           # macOS/Linux
```

Step 2 — print the full doctor output verbatim (no paraphrasing).

Step 3 — append exactly ONE short line of interpretation. Examples:
- `all healthy.`
- `chatlog DB never captured — run /m3:install.`
- `Gemini SessionEnd hook off — run mcp-memory chatlog init --apply-gemini.`

Do not write a paragraph. One line. The user can read the doctor output themselves.
