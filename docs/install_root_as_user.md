# Using M3 Memory as Root When Another User Owns the Install

M3's installer refuses to run as root by design — `pipx install` as root
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

All state now lives under `/home/bob/.m3-memory/` and
`/home/bob/.local/share/pipx/venvs/m3-memory/`.

---

## Step 2 — Make the embed server survive across sessions (optional but recommended)

If you want Tier-2 embedding available at all times — including when bob is
not logged in — enable systemd linger **as root**:

```bash
loginctl enable-linger bob
```

This keeps bob's systemd user session (and the `m3-embed-server` service)
running after bob logs out. Without it, the embed server stops when bob's
last session exits; Tier-1 in-process GGUF embedding still works regardless.

---

## Step 3 — Fix file permissions so root can read/write the store

The m3 MCP process spawned by root's Claude runs as root. It needs write
access to bob's database files:

```bash
# Option A — add root to bob's group (cleanest):
usermod -aG bob root
chmod -R g+rwX /home/bob/.m3-memory
chown -R bob:bob /home/bob/.m3-memory   # ensure bob is still the owner
# Then set the group sticky bit so new files inherit the group:
find /home/bob/.m3-memory -type d -exec chmod g+s {} \;

# Option B — world-readable/writable (simpler, less secure):
chmod o+rx /home/bob/.m3-memory
chmod o+rw /home/bob/.m3-memory/engine/agent_memory.db
chmod o+rw /home/bob/.m3-memory/engine/agent_memory.db-wal
chmod o+rw /home/bob/.m3-memory/engine/agent_memory.db-shm
```

Option A is preferred on shared machines. Option B is fine on a single-user
dev box.

---

## Step 4 — Wire root's Claude to bob's m3 install

As root, add the MCP server to `/root/.claude/settings.json`. The key is
setting `HOME` so m3 resolves all paths relative to bob's home, not `/root`:

```json
{
  "mcpServers": {
    "memory": {
      "command": "/home/bob/.local/bin/m3",
      "env": {
        "HOME": "/home/bob",
        "M3_MEMORY_ROOT": "/home/bob/.m3-memory"
      }
    }
  }
}
```

Or use the CLI (as root):

```bash
claude mcp add memory /home/bob/.local/bin/m3
```

Then manually add the `env` block to `/root/.claude/settings.json` — the
`claude mcp add` command doesn't accept env overrides on the command line.

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

Add those hooks to `/root/.claude/settings.json`, replacing any relative
paths with absolute paths pointing at bob's install:

```json
{
  "hooks": {
    "Stop": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "/home/bob/.local/bin/m3 chatlog write --agent claude-code"
      }]
    }],
    "PreCompact": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "/home/bob/.local/bin/m3 chatlog write --agent claude-code --precompact"
      }]
    }]
  }
}
```

> **Verify the exact hook command** from bob's settings — the above is a
> template; the actual flags may differ depending on your m3 version and
> capture mode. Run `m3 chatlog init --dry-run` as bob to see what the
> wizard would write.

---

## Concurrent use

Bob and root can both run Claude sessions simultaneously against the same
store. M3 uses SQLite WAL mode with a `busy_timeout`, so concurrent writers
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
| Wrong memory store (empty) | `HOME` not overridden | Add `"HOME": "/home/bob"` to MCP env (Step 4) |

---

## Related

- [install_linux.md](install_linux.md) — standard Linux install
- [QUICKSTART_LINUX.md](QUICKSTART_LINUX.md) — five-minute walkthrough
- [EMBED_DEPLOYMENT.md](EMBED_DEPLOYMENT.md) — embedder architecture and Tier-1/Tier-2 details
