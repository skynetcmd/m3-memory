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
4. `m3 setup` — one-command wizard: fetches the system payload, installs the
   sovereign CPU embedder, wires every agent it finds on PATH (Claude / Gemini /
   OpenCode / OpenClaw), installs chatlog hooks, runs `m3 doctor`.

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
m3 setup                              # one-command wizard
```

### Fedora / RHEL / Rocky / AlmaLinux

```bash
sudo dnf install -y pipx python3-virtualenv git sqlite curl
pipx ensurepath
exec $SHELL -l
pipx install m3-memory
m3 setup                              # one-command wizard
```

### Arch / Manjaro / EndeavourOS

```bash
sudo pacman -S --needed python-pipx git sqlite curl
pipx ensurepath
exec $SHELL -l
pipx install m3-memory
m3 setup                              # one-command wizard
```

### openSUSE

```bash
sudo zypper install -y pipx git sqlite curl
pipx ensurepath
exec $SHELL -l
pipx install m3-memory
m3 setup                              # one-command wizard
```

### Alpine

```bash
sudo apk add --no-cache pipx git sqlite curl
pipx ensurepath
exec $SHELL -l
pipx install m3-memory
m3 setup                              # one-command wizard
```

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

```bash
# Claude Code
claude mcp add memory m3

# Gemini CLI (auto-wired by m3 setup; re-run if Gemini was installed AFTER m3)
m3 chatlog init --apply-gemini
```

---

## Common gotchas

- **`pip install m3-memory` fails with `externally-managed-environment`** —
  Debian 12+, Ubuntu 24.04+, Fedora 38+, Arch ship Python under PEP 668.
  Use `pipx` (above) — that's why it's the recommended path.
- **`m3: command not found` after `pipx install`** — pipx adds
  `~/.local/bin` to PATH via `pipx ensurepath`, but you need a fresh shell
  for it to take effect. `exec $SHELL -l` works without closing the terminal.
  (`mcp-memory` is also installed as a backwards-compatible alias.)
- **Hooks can't find Python on a pipx install** — fixed in v2026.4.24.7+;
  the hook scripts probe both `~/.local/share/pipx/venvs/m3-memory` (pipx ≥1.4
  XDG path) and `~/.local/pipx/venvs/m3-memory` (older pipx).
- **`gemini` not on PATH for cron / non-login shells** — `m3 install-m3`
  appends `~/.npm-global/bin` to `~/.profile`. If you installed Gemini AFTER
  install-m3, run `m3 chatlog init --apply-claude` (or `--apply-gemini`)
  to retroactively fix it.
- **`m3 embedder install` says GGUF is an LFS pointer** — the bundled
  bge-m3 model file is tracked via Git LFS. If you cloned without LFS,
  run `git lfs install && git lfs pull` inside the m3-memory checkout
  (`pipx`/`pip` users don't hit this — the wizard fetches into
  `~/.m3-memory/repo/_assets/models/` automatically).

---

## Advanced setup

The full homelab walkthrough — Postgres sync, ChromaDB, multi-machine
federation — lives at [install_linux_homelab.md](install_linux_homelab.md).
Most users don't need any of that; the one-liner above is enough for a
working local install.

---

## Verifying

```bash
m3 doctor
```

Should show:
- Package version + installed payload
- Chatlog DB path + captured row count + last-capture timestamp
- Per-agent hook state for Claude (Stop / PreCompact) and Gemini (SessionEnd)
