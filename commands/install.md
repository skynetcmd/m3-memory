---
name: install
description: Install or upgrade the m3-memory CLI + system payload.
---

# m3-memory install / upgrade

Step 1 — detect current state. Try in order:

```
mcp-memory --version                       # plain CLI
python -m m3_memory.cli --version           # module form (Windows --user)
```

Step 2 — branch on what step 1 printed.

**Not installed (both commands fail):**
- On Linux / macOS, suggest:
  ```
  curl -fsSL https://raw.githubusercontent.com/skynetcmd/m3-memory/main/install.sh | bash
  ```
- On Windows, suggest:
  ```powershell
  pip install --user m3-memory
  # then add %APPDATA%\Python\Python<NN>\Scripts to user PATH if needed
  ```
  Link them to `https://github.com/skynetcmd/m3-memory/blob/main/docs/install_windows.md` for full instructions.

After install completes, tell the user to re-run `/m3:health` in a NEW Claude Code session (PATH changes don't apply to the current one).

**Already installed (a version printed):**
- On Linux / macOS, run:
  ```
  pipx upgrade m3-memory
  mcp-memory update
  ```
- On Windows, run:
  ```powershell
  pip install --user --upgrade m3-memory
  python -m m3_memory.cli update
  ```
- Then `/m3:health` to verify.

Step 3 — print one-line summary of what action was taken or recommended.
