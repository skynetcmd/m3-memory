# Install on Windows

There's no one-line bash installer for Windows (PowerShell doesn't have `bash`,
and the prerequisites differ enough that the Linux script wouldn't apply
cleanly). Three commands instead:

## Quickstart

Open PowerShell (as your normal user, not admin):

```powershell
# 1. Install prerequisites (one-time admin step — run this in an elevated PowerShell):
winget install -e --id Python.Python.3.12
winget install -e --id Git.Git
winget install -e --id SQLite.SQLite

# 2. As your normal user, install m3-memory:
pip install m3-memory
mcp-memory install-m3 --capture-mode both
mcp-memory doctor
```

That's it. Windows pip doesn't have the PEP 668 issue Linux does, so plain
`pip install` works.

---

## Adding to an MCP client

```powershell
# Claude Code
claude mcp add memory mcp-memory

# Gemini CLI (if you have it installed)
# install-m3's post-install phase auto-writes the entry into
# %USERPROFILE%\.gemini\settings.json — re-run mcp-memory install-m3 if you
# install Gemini CLI later.
```

---

## Common gotchas

- **`mcp-memory: command not found` after `pip install --user`** — `pip install --user`
  puts script shims at `%APPDATA%\Python\Python<NN>\Scripts` (e.g.
  `C:\Users\you\AppData\Roaming\Python\Python314\Scripts`), and that
  directory is NOT on PATH by default on most Windows systems. Two fixes:

  1. **Add it to user PATH** (preferred — survives reboots, applies to new shells):
     ```powershell
     # Run once in PowerShell. Note: ($env:APPDATA + ...) is intentional —
     # do NOT put $env:APPDATA inside double quotes here, the env var must
     # expand BEFORE being written to the registry, otherwise the literal
     # string "$env:APPDATA" gets stored.
     [Environment]::SetEnvironmentVariable(
       "Path",
       ($env:APPDATA + "\Python\Python314\Scripts;" + [Environment]::GetEnvironmentVariable("Path", "User")),
       "User"
     )
     ```
     Adjust `Python314` to your installed Python minor version. Restart
     any open terminal / Claude Code session afterward.

  2. **Use the module form** as a workaround (always works as long as the
     `m3_memory` package is importable):
     ```
     python -m m3_memory.cli doctor
     python -m m3_memory.cli install-m3 --capture-mode both
     ```
     The `/m3:*` slash commands fall back to this automatically when
     `mcp-memory.exe` isn't on PATH.

  Alternative: `pip install` (without `--user`) puts the script at
  `C:\PythonNN\Scripts\` which IS on PATH on most Windows Python installs.
  Requires elevated PowerShell if Python is system-wide.
- **PowerShell vs cmd** — both work; cmd needs the same Scripts dir on PATH.
- **`sqlite3` not on PATH** — winget puts it under
  `%LOCALAPPDATA%\Programs\SQLite`. Add that to PATH for the CLI to be visible.
  Note that Python's stdlib `sqlite3` works regardless, so most m3-memory
  features don't need the CLI.
- **Hooks shipping LF endings on a Windows checkout** — `.gitattributes`
  pins `*.sh` to LF and `*.ps1` to CRLF to keep both platforms working.

---

## Advanced setup

The full homelab walkthrough — Postgres sync, ChromaDB, LM Studio embedding
server, multi-machine federation — lives at
[install_windows_homelab.md](install_windows_homelab.md). Most users
don't need any of that; the quickstart above is enough for a working local
install.

---

## Verifying

```powershell
mcp-memory doctor
```

Should show:
- m3-memory package version + installed payload
- Chatlog DB path + captured row count + last-capture timestamp
- Per-agent hook state for Claude (Stop / PreCompact) and Gemini (SessionEnd)
