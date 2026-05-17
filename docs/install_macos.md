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
m3 setup                               # one-command wizard
```

> 🍎 **Apple Silicon vs Intel:** the sovereign baseline (BGE-M3 CPU on :8082)
> runs on both. The wizard offers an opt-in GPU in-process embedder; on Apple
> Silicon it builds with `embedded-metal` for ~10-50× faster embeddings.
> Intel Macs stay on CPU (still plenty fast for typical use).

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

- **`m3: command not found` after `pipx install`** — pipx adds
  `~/.local/bin` to PATH via `pipx ensurepath`, but you need a new shell
  for it to take effect. `exec $SHELL -l` works without closing the terminal.
  (`mcp-memory` is also installed as a backwards-compatible alias.)
- **Homebrew Python is PEP 668** — that's fine, it's why we use pipx.
- **macOS-shipped Python (`/usr/bin/python3`) is old and externally managed** —
  don't try to `pip install` against it. Always use brew Python via pipx.
- **`m3 embedder install` says GGUF is an LFS pointer** — the bundled bge-m3
  model file is tracked via Git LFS. If you cloned m3-memory directly without
  LFS, run `git lfs install && git lfs pull` inside the checkout
  (`pipx`/`pip` users don't hit this — the wizard handles it).

---

## Advanced setup

The full homelab walkthrough — Postgres sync, ChromaDB, multi-machine
federation — lives at [install_macos_homelab.md](install_macos_homelab.md).
Most users don't need any of that; the one-liner above is enough for a
working local install.

---

## Verifying

```bash
mcp-memory doctor
```

Should show:
- Package version + installed payload
- Chatlog DB path + captured row count + last-capture timestamp
- Per-agent hook state for Claude (Stop / PreCompact) and Gemini (SessionEnd)
