# Install on macOS

The one-line installer (Linux + macOS):

```bash
curl -fsSL https://raw.githubusercontent.com/skynetcmd/m3-memory/main/install.sh | bash
```

That's all you need. The script:
1. Detects macOS, checks for Homebrew (installs from https://brew.sh if missing).
2. `brew install pipx git sqlite` — only what isn't already there.
3. `pipx install m3-memory`.
4. `mcp-memory install-m3 --capture-mode both` — fetches the system payload,
   auto-wires Claude / Gemini settings.json if either CLI is installed.
5. `mcp-memory doctor` — prints a verification summary.

**Cautious version** (audit before running):

```bash
curl -fsSL https://raw.githubusercontent.com/skynetcmd/m3-memory/main/install.sh -o install.sh
less install.sh
bash install.sh
```

## Manual install

If you'd rather not run the script:

```bash
brew install pipx git sqlite
pipx ensurepath
exec $SHELL -l                         # pick up ~/.local/bin in PATH
pipx install m3-memory
mcp-memory install-m3 --capture-mode both
mcp-memory doctor
```

---

## Adding to an MCP client

```bash
# Claude Code
claude mcp add memory mcp-memory

# Gemini CLI (if installed)
# install-m3 auto-wires ~/.gemini/settings.json when gemini is on PATH.
# If you install Gemini AFTER m3-memory, run:
mcp-memory chatlog init --apply-gemini
```

---

## Common gotchas

- **`mcp-memory: command not found` after `pipx install`** — pipx adds
  `~/.local/bin` to PATH via `pipx ensurepath`, but you need a new shell
  for it to take effect. `exec $SHELL -l` works without closing the terminal.
- **Homebrew Python is PEP 668** — that's fine, it's why we use pipx.
- **macOS-shipped Python (`/usr/bin/python3`) is old and externally managed** —
  don't try to `pip install` against it. Always use brew Python via pipx.

---

## Advanced setup

The full homelab walkthrough — Postgres sync, ChromaDB, LM Studio embedding
server, multi-machine federation — lives at
[install_macos_homelab.md](install_macos_homelab.md). Most users don't need
any of that; the one-liner above is enough for a working local install.

---

## Verifying

```bash
mcp-memory doctor
```

Should show:
- Package version + installed payload
- Chatlog DB path + captured row count + last-capture timestamp
- Per-agent hook state for Claude (Stop / PreCompact) and Gemini (SessionEnd)
