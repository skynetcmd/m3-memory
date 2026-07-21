---
name: m3-health
description: Health check — package version, installed payload, chatlog DB row count, per-agent hook state.
---
# M3 Health

## When to Use
Use this skill when the user runs a system health check or when you want to diagnose issues with the m3-memory configuration, CLI executables, background services, or databases.

## Instructions
Step 1 — run the doctor command, trying the resolvers below in order. Stop at the first one that returns exit 0; do not run the remaining ones.

```bash
# 1. Plain CLI, if mcp-memory is on PATH:
mcp-memory doctor

# 2. Module form, works whenever the m3_memory package is importable:
python -m m3_memory.cli doctor

# 3. Repo-local venv (developer case, run from the repo root):
.venv/Scripts/python.exe -m m3_memory.cli doctor   # Windows
.venv/bin/python -m m3_memory.cli doctor           # macOS/Linux
```

Step 2 — print the full doctor output verbatim (no paraphrasing).

Step 3 — append exactly ONE short line of interpretation. Examples:
- `all healthy.`
- `chatlog DB never captured — run /m3:install.`
- `Antigravity SessionEnd hook off — run mcp-memory chatlog init --apply-gemini.`

Do not write a paragraph. One line. The user can read the doctor output themselves.

Step 4 — if (and only if) doctor reported a repairable problem (a stale/dead agent
config path, a duplicate bridge, a disabled plugin, or a "run doctor --fix" hint),
OFFER to auto-repair. `doctor --fix` repoints dead config paths, de-duplicates MCP
registrations, and re-syncs agent hooks — it is non-destructive to data. Run the
same resolver that worked above, with `--fix`:

```bash
mcp-memory doctor --fix
# or: python -m m3_memory.cli doctor --fix
```

Then re-run plain `doctor` to confirm the fix took. If doctor was already all
healthy, do NOT run `--fix`.
