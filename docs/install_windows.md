# Install on Windows

There's no one-line bash installer for Windows (PowerShell doesn't have `bash`,
and the prerequisites differ enough that the Linux script wouldn't apply
cleanly). Three commands instead:

## Quickstart

**Step 1 — Prerequisites** (elevated PowerShell, right-click → Run as administrator, once):

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id Git.Git
winget install -e --id SQLite.SQLite
```

> **Microsoft Store Python?** The Store installs a `python3.exe` stub that
> blocks some installs. Use the winget version above — it puts a real
> `python.exe` on PATH.

**Step 2 — Install m3** (normal user PowerShell, not elevated):

```powershell
# Recommended: pipx isolates m3 and manages PATH automatically
pip install --user pipx
pipx ensurepath
# Open a new terminal so PATH refreshes, then:
pipx install m3-memory
m3 setup                              # one-command wizard
```

**Prefer plain pip?**

```powershell
pip install --user m3-memory
m3 setup
```

> **`m3` not found after `pip install --user`?** See the gotchas section
> below — pip puts `m3.exe` in a Scripts folder that isn't on PATH by
> default. `pipx` handles this automatically.

> **Tool catalog stays small in your context.** m3 ships 87 MCP tools but
> groups them into 8 domains (memory, chatlog, files, entity, agent, tasks,
> conversations, admin). Only ~6 essentials load at MCP startup
> (~2,400 tokens vs ~16,100 if all 87 loaded eagerly). The agent pulls in a
> domain on demand — just say "load the files tools" and it does. Set
> `M3_TOOLS_LAZY=0` to disable.

---

## Adding to an MCP client

`m3 setup` wires every agent it detects on PATH. If you skipped the wizard or
add an agent later, run these by hand:

```powershell
# Claude Code
claude mcp add memory m3

# Gemini CLI (auto-wired by m3 setup; re-run if Gemini was installed AFTER m3)
m3 chatlog init --apply-gemini
```

### Claude Code plugin install

```
/plugin marketplace add skynetcmd/m3-memory
/plugin install m3@skynetcmd
```

> **No GitHub SSH key?** The `owner/repo` shorthand uses SSH. If you get
> "Premature close" or "ERR_STREAM_PREMATURE_CLOSE", use the HTTPS URL:
> ```
> /plugin marketplace add https://github.com/skynetcmd/m3-memory
> /plugin install m3@skynetcmd
> ```

---

## Embedder (Tier-2 service — optional but recommended)

The **Tier-1 in-process GGUF embedder** is active from the moment m3 starts —
no extra steps. The **Tier-2 embed server** (port 8082, Windows Service)
improves cold-start performance but is optional. M3 works fully without it.

### Install the binary first

```powershell
m3 embedder install-gpu   # installs prebuilt wheel — no Rust needed for CPU
```

Despite the name, this works on CPU-only machines. On NVIDIA machines it
autodetects CUDA; on others it falls back to CPU.

### Register as a Windows Service (requires elevation)

```powershell
# Elevated PowerShell (right-click → Run as administrator):
m3 embedder install
```

Verify from any terminal:

```powershell
m3 doctor   # shows Tier-1 / Tier-2 status and embed roundtrip latency
```

### No admin rights? Use Task Scheduler instead

```powershell
$gguf     = "$env:USERPROFILE\.m3-memory\_assets\models\bge-m3-Q4_K_M.gguf"
$action   = New-ScheduledTaskAction `
                -Execute "powershell.exe" `
                -Argument "-WindowStyle Hidden -Command `"& { `$env:M3_EMBED_GGUF='$gguf'; m3-embed-server }`"" `
                -WorkingDirectory "$env:USERPROFILE"
$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0
Register-ScheduledTask -TaskName "m3-embed-server" `
    -Action $action -Trigger $trigger -Settings $settings `
    -RunLevel Limited -Force
```

`RunLevel Limited` means no elevation required. The task starts at every login
and survives reboots. To remove: `Unregister-ScheduledTask -TaskName "m3-embed-server"`.

Or run the server manually for the current session only:

```powershell
$env:M3_EMBED_GGUF = "$env:USERPROFILE\.m3-memory\_assets\models\bge-m3-Q4_K_M.gguf"
Start-Process -WindowStyle Hidden -FilePath "m3-embed-server" `
    -RedirectStandardOutput "$env:TEMP\m3-embed.log" `
    -RedirectStandardError  "$env:TEMP\m3-embed.log"
```

---

## Common gotchas

- **`m3: command not found` after `pip install --user`** — `pip install --user`
  puts script shims at `%APPDATA%\Python\Python<NN>\Scripts`, which is NOT
  on PATH by default. Three fixes — pick one:

  1. **Use pipx instead** (recommended — handles PATH automatically):
     ```powershell
     pip install --user pipx && pipx ensurepath
     # Open a new terminal, then:
     pipx install m3-memory
     ```

  2. **Add the Scripts dir to your user PATH** (survives reboots):
     ```powershell
     # Detect the right Scripts path automatically:
     $scripts = python -c "import sysconfig; print(sysconfig.get_path('scripts', 'nt_user'))"
     [Environment]::SetEnvironmentVariable(
         "Path",
         ($scripts + ";" + [Environment]::GetEnvironmentVariable("Path", "User")),
         "User"
     )
     # Open a new terminal afterward.
     ```

  3. **Use the module form** as a fallback (always works when the package
     is importable):
     ```powershell
     python -m m3_memory.cli doctor
     python -m m3_memory.cli setup
     ```
     The `/m3:*` slash commands fall back to this automatically when
     `m3.exe` isn't on PATH.
- **`m3 embedder install` fails with "Access Denied" or service errors** —
  registering a Windows Service requires elevation. Open an Administrator
  PowerShell and re-run `m3 embedder install`. If you can't elevate, use
  the Task Scheduler approach in the Embedder section above — no admin
  rights needed.

- **`m3 embedder install` says "binary not found"** — run
  `m3 embedder install-gpu` first to install the `m3-embed-server` binary
  (prebuilt PyPI wheel, no Rust toolchain needed), then retry `m3 embedder install`.

- **PowerShell vs cmd** — both work; cmd needs the same Scripts dir on PATH.
- **`sqlite3` not on PATH** — winget puts it under
  `%LOCALAPPDATA%\Programs\SQLite`. Add that to PATH for the CLI to be visible.
  Note that Python's stdlib `sqlite3` works regardless, so most m3-memory
  features don't need the CLI.
- **Hooks shipping LF endings on a Windows checkout** — `.gitattributes`
  pins `*.sh` to LF and `*.ps1` to CRLF to keep both platforms working.

---

## Advanced setup

The full homelab walkthrough — Postgres sync, ChromaDB, multi-machine
federation — lives at [install_windows_homelab.md](install_windows_homelab.md).
Most users don't need any of that; the quickstart above is enough for a
working local install.

---

## Verifying

```powershell
m3 doctor
```

Should show:
- m3-memory package version + installed payload
- Chatlog DB path + captured row count + last-capture timestamp
- Per-agent hook state for Claude (Stop / PreCompact) and Gemini (SessionEnd)
