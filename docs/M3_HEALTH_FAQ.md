# M3 Health FAQ — Understanding `/m3:health` and `mcp-memory doctor`

## What is `/m3:health`?

`/m3:health` is a slash command in Claude Code that runs the m3-memory diagnostic tool. It wraps `mcp-memory doctor`, which prints the status of your m3-memory installation, database, and hook configuration.

The command uses a three-fallback resolver chain:
1. `mcp-memory` CLI (if on PATH)
2. `python -m m3_memory.cli doctor` (direct module invocation)
3. Developer sibling bridge (if running inside the m3-memory repo)

Output is the doctor's diagnostic text plus one interpretation line at the end. No code is modified; this is read-only inspection.

---

## Reading the Output, Line by Line

### Package version and config file

```
m3-memory package version: 2026.4.24.12
config file:               <home>/.m3-memory/config.json
  (no config - system not installed via `mcp-memory install-m3`)
```

(Paths in this doc use `<home>` and `<repo>` as placeholders; your output will show actual paths like `/home/alice/...` on Linux/macOS or `C:\Users\Alice\...` on Windows.)

**What it means:**
- `package version` — the m3-memory package you have installed.
- `config file` — absolute path to the system config file.
- `(no config ...)` — config.json doesn't exist. This is **expected for developers** working in the m3-memory repo. General users who ran `mcp-memory install-m3` will have this file.

**Actionable?** No. Both states are normal.

---

### Environment variable and developer bridge

```
M3_BRIDGE_PATH (env):      (unset)
developer sibling bridge:  <repo>/bin/memory_bridge.py
```

**What it means:**
- `M3_BRIDGE_PATH` — optional env var pointing to a custom bridge path (useful for production deployments with non-standard layouts).
- `(unset)` — no custom path. Doctor falls back to find_bridge() which looks for config.json, then the developer sibling.
- `developer sibling bridge` — doctor found the bridge by walking up from your current working directory to the m3-memory repo root.

**Actionable?** No. For developers: both lines are expected. For general users: M3_BRIDGE_PATH should be unset unless you've explicitly relocated your bridge.

---

### Chatlog subsystem

```
chatlog subsystem:
  db_path:                 <repo>/memory/agent_chatlog.db
  captured rows:           <N>
  last capture at:         <ISO-8601 timestamp>
  claude hooks:            Stop [on]  PreCompact [on]
  gemini mcp (memory):     [on]  SessionEnd [on]
```

**What it means:**
- **`db_path`** — where the chat log database lives.
- **`captured rows`** — total rows written to the DB. Zero means no captures yet (hooks haven't fired).
- **`last capture at`** — timestamp of the most recent row written. `(never)` or a stale timestamp = hooks may be broken.
- **`claude hooks`**:
  - `Stop [on]` — per-turn capture enabled (Claude Code's Stop hook fires at end of each turn).
  - `PreCompact [on]` — pre-compaction capture enabled (Claude Code's PreCompact hook fires before memory compaction).
  - Either or both can be on; typically PreCompact alone is enough.
- **`gemini mcp (memory)`**:
  - First `[on]/[off]` — Gemini CLI has the memory MCP server registered.
  - Second `[on]/[off]` — Gemini's SessionEnd hook is configured to capture at session end.

**Actionable?** Yes — see common diagnoses below.

---

### Final bridge resolution

```
[OK] resolved bridge: <repo>/bin/memory_bridge.py
```

**What it means:** Doctor successfully found the bridge. If you see `[X]` instead, the bridge is missing — check your installation.

**Actionable?** Yes, only if `[X]`.

---

## Common Diagnoses and How to Fix

### All hooks [on], captured rows > 0, last capture recent

Everything is healthy. Nothing to do.

---

### `mcp-memory: command not found`

Your PATH doesn't have the m3-memory CLI installed. However, the `/m3:health` slash command **automatically falls back** to `python -m m3_memory.cli doctor`, so this isn't blocking.

To fix PATH for direct invocation, find your Python user base:

```bash
python -m site --user-base
```

Then add `<user_base>/Scripts` to your PATH. On Windows, this is typically `C:\Users\<YourName>\AppData\Roaming\Python\Python<VERSION>\Scripts`.

---

### `(no config - system not installed...)` plus `developer sibling bridge:`

You're working in a development clone of m3-memory. This is **expected and correct**. The `config.json` file is created by `mcp-memory install-m3` for production users only.

If you're editing the m3-memory source code or testing changes locally, you don't need config.json. The doctor will resolve the bridge via the sibling walk.

---

### `M3_BRIDGE_PATH (env): (unset)`

This is fine in development. Set M3_BRIDGE_PATH **only if** your bridge lives at a non-standard path:
- You relocated the m3-memory repo after install.
- You have a multi-machine deployment with a custom bridge location.
- Your installation instructions explicitly tell you to set it.

For a standard install, leave it unset.

---

### `captured rows: 0`

The chatlog database exists but has never captured a turn. This means:
1. The DB was created by init but hooks haven't fired yet, OR
2. The hooks are broken and not running.

Check if you've run any Claude Code or Gemini sessions since installing hooks. If yes, the hooks are broken. Run:

```bash
mcp-memory chatlog init --reconfigure
```

This re-wires the hooks. For Claude Code specifically:

```bash
mcp-memory chatlog init --apply-claude
```

---

### `last capture at: (none)` or very stale (> 24 hours)

Your hooks stopped firing. Run a Claude Code session and check again. If still stale:

1. Check the `claude hooks:` line. If either shows `[off]`, re-apply:
   ```bash
   mcp-memory chatlog init --apply-claude
   ```

2. Check the `gemini mcp (memory):` line. If either shows `[off]`:
   ```bash
   mcp-memory chatlog init --apply-gemini
   ```

3. Verify that `~/.claude/settings.json` and `~/.gemini/settings.json` exist and are readable.

---

### `claude hooks: Stop [off]` or `PreCompact [off]`

One or both hooks are not wired. Re-apply:

```bash
mcp-memory chatlog init --apply-claude
```

This merges the hooks into your `~/.claude/settings.json` (creates the file if missing; backs up before writing). It's safe to run multiple times.

---

### `gemini mcp (memory): [off]` or `SessionEnd [off]`

Gemini doesn't have the memory MCP server registered, or the SessionEnd hook is missing.

First, ensure the memory MCP is registered. If you haven't run `mcp-memory install-m3` yet, do that:

```bash
mcp-memory install-m3
```

Then wire the SessionEnd hook:

```bash
mcp-memory chatlog init --apply-gemini
```

(Requires Gemini CLI to be installed first.)

---

### `[OK] resolved bridge: ...` missing or `[X] no bridge found`

The bridge can't be located. Check:

1. **Are you inside the m3-memory repo?** If yes, the developer sibling walk should find it.
2. **Did you run `mcp-memory install-m3`?** If no, run it now.
3. **Is M3_BRIDGE_PATH set to a path that exists?** If set, verify the file is readable.
4. **Is your config.json (if you have one) pointing to a valid path?** Edit it if needed.

---

## Developer Mode vs General User Mode

| Aspect | Developer (working in repo) | General user (pip installed) |
|---|---|---|
| **Install command** | `git clone` then `pip install -e .` or just clone | `pip install m3-memory` then `mcp-memory install-m3` |
| **config.json present** | NO (expected) | YES |
| **M3_BRIDGE_PATH** | usually unset | usually set |
| **Bridge resolution** | developer sibling walk | config.json or env var |
| **Meaning of "no config"** | informational only | something to investigate if unexpected |

**Key difference:** Developers trade the convenience of a config file for the ability to edit and test code directly. The doctor handles both gracefully via its fallback resolver chain.

---

## When to Actually Worry

**Take action if you see:**
- `captured rows: 0` — run `mcp-memory chatlog init` to wire hooks.
- Any hook showing `[off]` — run `mcp-memory chatlog init --apply-claude` or `--apply-gemini`.
- `last capture at` more than 24 hours old **and** your chatlog DB shows recent activity in an editor — hooks are broken, re-run init.
- `[X] no bridge found` — check your installation, config.json, or M3_BRIDGE_PATH.

**Don't worry about:**
- `mcp-memory: command not found` — PATH plumbing only; fallback chain handles it.
- `(no config - system not installed...)` — expected for developers.
- `M3_BRIDGE_PATH (env): (unset)` — expected for developers and standard installs.

---

## Cross-References

- `mcp-memory chatlog --help` — full list of chatlog subcommands.
- `mcp-memory doctor --help` — doctor-specific flags (if any).
- [docs/SYNC.md](SYNC.md) — warehouse sync (separate concern, uses same database).
- [docs/AGENT_INSTRUCTIONS.md](AGENT_INSTRUCTIONS.md) — memory best practices for agents.
