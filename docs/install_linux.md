# Install on Linux

The one-line installer (Linux + macOS):

```bash
curl -fsSL https://raw.githubusercontent.com/skynetcmd/m3-memory/main/install.sh | bash
```

The script:
1. Reads `/etc/os-release` to identify your distro.
2. Installs prerequisites via `apt` / `dnf` / `pacman` / `zypper` / `apk` —
   only what isn't already there. Tools needed: `pipx`, `git`, `sqlite3`,
   `curl`, plus `python3-venv` on Debian-family.
3. `pipx install m3-memory`.
4. `mcp-memory install-m3 --capture-mode both` — fetches the system payload
   from GitHub, auto-wires Claude / Gemini settings.json if either CLI is
   already installed.
5. `mcp-memory doctor` — prints a verification summary.

Refuses to run as root. Sudo is invoked individually for the package install
step so you see what's being elevated.

**Cautious version** (audit first):

```bash
curl -fsSL https://raw.githubusercontent.com/skynetcmd/m3-memory/main/install.sh -o install.sh
less install.sh
bash install.sh
```

## Manual install

### Debian 12+ / Ubuntu 24.04+ / Mint / Pop!_OS

```bash
sudo apt update && sudo apt install -y pipx python3-venv git sqlite3 curl
pipx ensurepath
exec $SHELL -l
pipx install m3-memory
mcp-memory install-m3 --capture-mode both
mcp-memory install-embedder           # optional: self-contained local embedder
mcp-memory doctor
```

### Fedora / RHEL / Rocky / AlmaLinux

```bash
sudo dnf install -y pipx python3-virtualenv git sqlite curl
pipx ensurepath
exec $SHELL -l
pipx install m3-memory
mcp-memory install-m3 --capture-mode both
mcp-memory install-embedder           # optional: self-contained local embedder
mcp-memory doctor
```

### Arch / Manjaro / EndeavourOS

```bash
sudo pacman -S --needed python-pipx git sqlite curl
pipx ensurepath
exec $SHELL -l
pipx install m3-memory
mcp-memory install-m3 --capture-mode both
mcp-memory install-embedder           # optional: self-contained local embedder
mcp-memory doctor
```

### openSUSE

```bash
sudo zypper install -y pipx git sqlite curl
pipx ensurepath
exec $SHELL -l
pipx install m3-memory
mcp-memory install-m3 --capture-mode both
mcp-memory install-embedder           # optional: self-contained local embedder
mcp-memory doctor
```

### Alpine

```bash
sudo apk add --no-cache pipx git sqlite curl
pipx ensurepath
exec $SHELL -l
pipx install m3-memory
mcp-memory install-m3 --capture-mode both
mcp-memory install-embedder           # optional: self-contained local embedder
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

- **`pip install m3-memory` fails with `externally-managed-environment`** —
  Debian 12+, Ubuntu 24.04+, Fedora 38+, Arch ship Python under PEP 668.
  Use `pipx` (above) — that's why it's the recommended path.
- **`mcp-memory: command not found` after `pipx install`** — pipx adds
  `~/.local/bin` to PATH via `pipx ensurepath`, but you need a fresh shell
  for it to take effect. `exec $SHELL -l` works without closing the terminal.
- **Hooks can't find Python on a pipx install** — fixed in v2026.4.24.7+;
  the hook scripts probe both `~/.local/share/pipx/venvs/m3-memory` (pipx ≥1.4
  XDG path) and `~/.local/pipx/venvs/m3-memory` (older pipx).
- **`gemini` not on PATH for cron / non-login shells** — `mcp-memory install-m3`
  appends `~/.npm-global/bin` to `~/.profile`. If you installed Gemini AFTER
  install-m3, run `mcp-memory chatlog init --apply-claude` (or `--apply-gemini`)
  to retroactively fix it.

---

## Advanced setup

The full homelab walkthrough — Postgres sync, ChromaDB, LM Studio embedding
server, multi-machine federation — lives at
[install_linux_homelab.md](install_linux_homelab.md). Most users don't need
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
