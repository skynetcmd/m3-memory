# Using m3 Memory as Root When Another User Owns the Install

m3's installer refuses to run as root by design — `pipx install` as root
puts all state under `/root/`, which agents running as normal users can't
reach, and vice versa. The correct pattern is:

- **Install m3 once, as the owning user** (e.g. `bob`)
- **Point every other user's agent** (including root's) at bob's install
  via the MCP server config, overriding `HOME` so m3 resolves paths correctly

This gives root full access to the same memory store, chatlog, and embedder
without duplicating the install or weakening file permissions beyond what's
needed.

---

## Step 1 — Install m3 as the owning user

Log in as (or `su` to) the normal user who will own the m3 install:

```bash
su - bob
curl -fsSL https://raw.githubusercontent.com/skynetcmd/m3-memory/main/install.sh | bash
```

Follow the wizard. When it finishes, verify:

```bash
m3 doctor
```

State now lives under bob's decoupled roots — databases in
`/home/bob/.m3/engine/`, config in `/home/bob/.m3/config/`, logs in
`/home/bob/.m3/logs/` — with the payload in `/home/bob/.m3-memory/` and the
package in `/home/bob/.local/share/pipx/venvs/m3-memory/`.

---

## Step 2 — Make the embed server survive across sessions (optional but recommended)

If you want Tier-2 embedding available at all times — including when bob is
not logged in — enable systemd linger **as root**:

```bash
loginctl enable-linger bob
```

This keeps bob's systemd user session (and the `m3-embed-server` service)
running after bob logs out. Without it, the embed server stops when bob's
last session exits — and since that shared server is the default embedder
(in-process embedding is opt-in via `M3_EMBED_INPROC=1`), embeds degrade
until bob logs in again.

---

## Step 3 — Fix file permissions so root can read/write the store

The m3 MCP process spawned by root's Claude runs as root. It needs write
access to bob's database files:

```bash
# Option A — add root to bob's group (cleanest):
usermod -aG bob root
chmod -R g+rwX /home/bob/.m3
chown -R bob:bob /home/bob/.m3   # ensure bob is still the owner
# Then set the group sticky bit so new files inherit the group:
find /home/bob/.m3 -type d -exec chmod g+s {} \;

# Option B — world-readable/writable (simpler, less secure):
chmod o+rx /home/bob/.m3 /home/bob/.m3/engine
chmod o+rw /home/bob/.m3/engine/agent_memory.db
chmod o+rw /home/bob/.m3/engine/agent_memory.db-wal
chmod o+rw /home/bob/.m3/engine/agent_memory.db-shm
# …and the same for agent_chatlog.db* if bob's chatlog uses a separate store
```

Option A is preferred on shared machines. Option B is fine on a single-user
dev box.

---

## Step 4 — Wire root's Claude to bob's m3 install

As root, register the MCP server with `claude mcp add` (Claude Code does not
read MCP servers from `~/.claude/settings.json` — only `~/.claude.json`,
`.mcp.json` and plugins). The key is setting `HOME` and pinning bob's roots so
m3 resolves every path to bob's store, not `/root`:

```bash
claude mcp add --scope user \
  --env HOME=/home/bob \
  --env M3_ENGINE_ROOT=/home/bob/.m3/engine \
  --env M3_CONFIG_ROOT=/home/bob/.m3/config \
  -- m3_memory /home/bob/.local/bin/m3
```

Restart Claude Code as root. Confirm the MCP is connected:

```
/m3:health
```

---

## Step 5 — Wire chatlog hooks for root's Claude (optional)

The Stop and PreCompact hooks are per-user. To capture root's Claude sessions
into bob's chatlog store, copy the hook entries from bob's settings:

```bash
# Read bob's hook config:
cat /home/bob/.claude/settings.json | python3 -c "
import json, sys
s = json.load(sys.stdin)
print(json.dumps(s.get('hooks', {}), indent=2))
"
```

Copy those `Stop` and `PreCompact` entries into `/root/.claude/settings.json`
**verbatim** — each is an absolute `<pipx venv python> …/m3_memory/bin/hooks/chatlog/claude_code_precompact.py`
command pointing at bob's install. The hook inherits root's *process* env, not
the MCP server's `env`, so make sure each command is prefixed with the same
pins as Step 4 (`HOME=/home/bob M3_ENGINE_ROOT=/home/bob/.m3/engine
M3_CONFIG_ROOT=/home/bob/.m3/config …`); otherwise the hook writes turns to a
different store than the server reads.

---

## Concurrent use

Bob and root can both run Claude sessions simultaneously against the same
store. m3 uses SQLite WAL mode with a `busy_timeout`, so concurrent writers
queue safely. In practice, the only contention is between root's and bob's
sessions writing to the same DB — this is handled automatically.

---

## Upgrading

Upgrades must be run as bob (the owning user), not root:

```bash
su - bob -c "pipx upgrade m3-memory && m3 update"
```

Root's Claude picks up the upgrade automatically on the next session start
(the MCP server is spawned fresh each time).

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `m3: command not found` when root's Claude starts | Absolute path not set | Use `/home/bob/.local/bin/m3`, not just `m3` |
| `Permission denied` on DB files | Missing write permission | Re-run Step 3 |
| Memory writes succeed but chatlog missing | Hooks not wired for root | Re-do Step 5 |
| Embed server not reachable | Bob not logged in + no linger | `loginctl enable-linger bob` (Step 2) |
| Wrong memory store (empty) | `HOME` / roots not overridden | Re-register with `--env HOME=/home/bob` + the `M3_ENGINE_ROOT` / `M3_CONFIG_ROOT` pins (Step 4) |

---

## Related

- [install_linux.md](install_linux.md) — standard Linux install
- [QUICKSTART_LINUX.md](QUICKSTART_LINUX.md) — five-minute walkthrough
- [EMBED_DEPLOYMENT.md](EMBED_DEPLOYMENT.md) — embedder architecture and Tier-1/Tier-2 details
