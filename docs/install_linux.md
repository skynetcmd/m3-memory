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

> **No sudo / not in the sudo group?** The script detects this and prints the
> exact package-manager command to run in a root shell. Open a second terminal
> as root, run that command, then continue in your original shell.
> If you have no root access at all, re-run with `--skip-prereqs` once an
> admin has installed pipx, git, and sqlite3 for you.

> **Installing as root / want m3 accessible to the root user?**
> The installer refuses to run as root by design — a root-owned pipx install
> can't be reached by normal-user agents. The correct approach is to install
> as a normal user (e.g. `bob`) and point root's Claude at that install via
> the MCP server config. One command to get started:
> ```bash
> su - bob
> curl -fsSL https://raw.githubusercontent.com/skynetcmd/m3-memory/main/install.sh | bash
> ```
> Then see **[install_root_as_user.md](install_root_as_user.md)** for the
> full walkthrough: permissions, MCP wiring, chatlog hooks, and linger setup.

**Cautious version** (audit first):

```bash
curl -fsSL https://raw.githubusercontent.com/skynetcmd/m3-memory/main/install.sh -o install.sh
less install.sh
bash install.sh
```

## Manual install

### Debian 12+ / Ubuntu 24.04+ / Mint / Pop!_OS / Kali / Neon / Zorin

Covers any distro whose `/etc/os-release` ID is `debian`, `ubuntu`,
`linuxmint`, `pop`, `raspbian`, `neon`, `kali`, `zorin`, `elementary`,
or `mint` — and any unknown distro where `apt-get` is present.

```bash
sudo apt update && sudo apt install -y pipx python3-venv git sqlite3 curl
pipx ensurepath
source ~/.bashrc    # or open a new terminal
pipx install m3-memory
m3 setup                              # one-command wizard
```

> **No sudo?** Ask an admin to run the `apt install` line above, then
> continue from `pipx ensurepath` as your normal user.

### Fedora / RHEL / Rocky / AlmaLinux

```bash
sudo dnf install -y pipx python3-virtualenv git sqlite curl
pipx ensurepath
source ~/.bashrc    # or open a new terminal
pipx install m3-memory
m3 setup                              # one-command wizard
```

### Arch / Manjaro / EndeavourOS

```bash
sudo pacman -S --needed python-pipx git sqlite curl
pipx ensurepath
source ~/.bashrc    # or open a new terminal
pipx install m3-memory
m3 setup                              # one-command wizard
```

### openSUSE

```bash
sudo zypper install -y pipx git sqlite curl
pipx ensurepath
source ~/.bashrc    # or open a new terminal
pipx install m3-memory
m3 setup                              # one-command wizard
```

### Alpine

> **Note:** `pipx` is in Alpine's community repository. Enable it first
> if not already: `echo "https://dl-cdn.alpinelinux.org/alpine/edge/community" >> /etc/apk/repositories`

```bash
sudo apk add --no-cache pipx git sqlite curl
pipx ensurepath
source ~/.profile   # Alpine uses /etc/profile.d, not .bashrc
pipx install m3-memory
m3 setup                              # one-command wizard
```

If `pipx` is unavailable in your Alpine version, use pip directly:

```bash
sudo apk add --no-cache py3-pip git sqlite curl
pip install --user --break-system-packages m3-memory
export PATH="$HOME/.local/bin:$PATH"
m3 setup
```

### Other / unknown distro

If your distro isn't listed above but has `apt-get` or `dnf`, use the
matching section above — the install.sh one-liner also auto-detects and
falls back to these. If neither package manager is available:

```bash
# Minimal path via pip + virtualenv (no package manager needed):
python3 -m venv ~/.venv/m3
source ~/.venv/m3/bin/activate
pip install m3-memory
m3 setup
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
claude mcp add --global memory m3

# Gemini CLI (auto-wired by m3 setup; re-run if Gemini was installed AFTER m3)
m3 chatlog init --apply-gemini
```

---

## Common gotchas

- **`pip install m3-memory` fails with `externally-managed-environment`** —
  Debian 12+, Ubuntu 24.04+, Fedora 38+, Arch ship Python under PEP 668.
  Use `pipx` (above) — that's why it's the recommended path. Fallback:
  `python3 -m venv ~/.venv/m3 && source ~/.venv/m3/bin/activate && pip install m3-memory`.

- **`m3: command not found` after `pipx install`** — pipx adds
  `~/.local/bin` to PATH via `pipx ensurepath`, but you need a fresh shell
  for it to take effect. Run `source ~/.bashrc` (or open a new terminal) —
  `exec $SHELL -l` only works in interactive login shells, not inside
  `curl | bash`. (`mcp-memory` is also installed as a backwards-compatible alias.)

- **No sudo / not in the sudo/wheel group** — the install.sh script detects
  this and prints the exact command to run in a root shell. Open a second
  terminal as root or ask a sysadmin to install the prerequisites, then
  re-run the installer with `--skip-prereqs`.

- **`m3 embedder install` fails with "binary not found"** — the
  `m3-embed-server` binary must be installed first via a separate step:
  ```bash
  m3 embedder install-gpu   # installs prebuilt wheel — no Rust needed for CPU
  m3 embedder install       # registers the systemd --user service
  ```
  `install-gpu` despite its name works on CPU-only machines.

- **`m3 embedder install` fails with "Failed to connect to user scope bus"** —
  systemd --user is unavailable (container, SSH session without D-Bus, minimal
  image). Run the server directly:
  ```bash
  M3_EMBED_GGUF=~/.m3-memory/_assets/models/bge-m3-Q4_K_M.gguf \
      nohup m3-embed-server > ~/.m3/engine/embed-server.log 2>&1 &
  ```
  For boot persistence: `crontab -e` and add:
  ```
  @reboot M3_EMBED_GGUF=~/.m3-memory/_assets/models/bge-m3-Q4_K_M.gguf m3-embed-server >> ~/.m3/engine/embed-server.log 2>&1 &
  ```
  Tier-1 in-process GGUF embedding is active regardless — Tier-2 is optional.

- **Hooks can't find Python on a pipx install** — fixed in v2026.4.24.7+;
  the hook scripts probe both `~/.local/share/pipx/venvs/m3-memory` (pipx ≥1.4
  XDG path) and `~/.local/pipx/venvs/m3-memory` (older pipx).

- **`gemini` not on PATH for cron / non-login shells** — `m3 install-m3`
  appends `~/.npm-global/bin` to `~/.profile`. If you installed Gemini AFTER
  install-m3, run `m3 chatlog init --apply-claude` (or `--apply-gemini`)
  to retroactively fix it.

- **`m3 embedder install` says GGUF is an LFS pointer** — the bundled
  bge-m3 model file is tracked via Git LFS. If you cloned without LFS,
  run `git lfs install && git lfs pull` inside the m3-memory checkout.
  (`pipx`/`pip` users don't hit this — the wizard fetches into
  `~/.m3-memory/repo/_assets/models/` automatically.)

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
