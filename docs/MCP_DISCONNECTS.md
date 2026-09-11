# Why your agent says "m3 is unreachable" — and why your memory is fine

**Short version:** the memory server did not crash and your data is not lost. The
*client* dropped the pipe it holds to the server process. This is a known,
documented limitation in MCP client lifecycle management — not an m3 failure —
and it can affect **any agent that reaches m3 over stdio**, which is all of them
by default. One command fixes it, in about a second, with nothing at risk:

```
/mcp
```

This page exists because the symptom *looks* like a memory-system failure, and we
would rather show you the evidence than ask you to take our word for it.

---

## What actually happens

Most MCP hosts launch local servers over the **stdio transport**. The client
spawns the server as a child process and holds a pipe to its stdin/stdout:

```
  client (claude.exe) ──spawn──> m3 (memory_bridge.py)
        │                              │
        └────── stdin / stdout ────────┘     the pipe IS the channel
```

The pipe is the entire channel. When the client's end goes away — an idle exit,
context compaction, a lifecycle timeout, OS memory pressure — the server process
goes with it, and the tools vanish from the agent's view.

**Nothing on the m3 side can heal this.** A process we started from outside would
have its stdin/stdout going nowhere, no way into the client's tool table, and no
way to make the client re-issue `initialize`. The client must do the spawning,
because the client must hold the file descriptor. That is precisely why `/mcp`
fixes it instantly: it asks the client to spawn a fresh server.

## Your data is not affected

This is the part worth internalising, because the alarming-looking symptom and
the actual risk are unrelated.

**Chatlog capture writes to the database directly. It does not go through the MCP
connection.** They are independent paths. A dropped MCP connection costs you the
ability to *search and write* memory from the agent for a few seconds. It does
not cost you a single captured turn.

Measured on a developer machine, 2026-09-11, during a real outage in which **two
concurrent Claude Code sessions lost m3 simultaneously**:

| Signal | Reading during the outage |
|---|---|
| Chatlog capture | **healthy — 865 rows written in 15 min** |
| Rows lost | **0** |
| Engine heartbeat (watchdog log) | unbroken `OK`, every 5 min, right through |
| Cognitive loop | alive, restarting normally on its own schedule |
| Dashboard service | alive and serving |
| MCP bridge process | dead — no live process at all |

A second measurement, 2026-09-07: a session with **17 MCP disconnects captured
610 turns with zero loss**, including turns written *during* disconnect windows.

The engine keeps running. Only the client's view of it lapses.

## Why this is not an m3 shortcoming

We want to be precise rather than defensive, so here is the evidence trail.

**The upstream behaviour is documented and acknowledged.** Claude Code's own MCP
documentation describes client-side timeouts governing tool calls, and the issue
tracker carries multiple reports of stdio servers dropping mid-session:

- [anthropics/claude-code#36308](https://github.com/anthropics/claude-code/issues/36308)
  — "MCP servers should auto-reconnect when disconnected mid-session." Lists the
  causes: idle timeout (the child process exits after no activity), crash on bad
  config, OS process management, unhandled exceptions. Critically: **no
  configuration, environment variable, or setting prevents the disconnect.**
  Every proposed remedy is recovery-after-the-fact. Closed as duplicate.
- [anthropics/claude-code#24350](https://github.com/anthropics/claude-code/issues/24350)
  — "MCP server connections drop silently and require manual /mcp reconnection."
  States the MCP server does not initiate the disconnect; the connection is
  terminated by the client internally. Closed as duplicate.
- [anthropics/claude-code#57207](https://github.com/anthropics/claude-code/issues/57207)
  — asks for a programmatic `claude mcp reconnect` that does not exist yet.

**It is transport-shaped, not server-shaped.** On the same machine, on the same
day, an HTTP-transport MCP server sat idle for *hours* without dropping, while
stdio servers dropped repeatedly. HTTP has no parent pipe to lose. This is a
property of how the server is *reached*, not of what the server *is*.

**m3 itself stays up.** Probe it by hand at any time — this is the check we use,
and you should trust it over any client's status display:

```bash
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"p","version":"1"}}}\n' | m3
```

A clean `serverInfo` reply means m3 is healthy and the fault is client-side
retention. On 2026-09-09 a server reported FAILED all session by its client
handshook perfectly in 677 ms.

## Does this affect other agents (Antigravity, Gemini CLI, OpenCode)?

**Assume every stdio agent is at risk.** We have measured the drop in Claude
Code; we have *not* yet verified the others, and absence of a report is not
evidence of immunity. Every host we wire registers m3 over **stdio**, which means
every one of them hands its server's lifetime to the client — the client owns the
pipe, so only the client can restore it. Any of them could exhibit this.

| Host | m3 transport | At risk? | Verified by us |
|---|---|---|---|
| Claude Code | stdio | **yes** | **confirmed** — measured repeatedly, two concurrent sessions |
| Antigravity CLI | stdio | **yes — same exposure** | not yet verified |
| Gemini CLI | stdio | **yes — same exposure** | not yet verified (has separate stdio defects upstream) |
| OpenCode | stdio | **yes — same exposure** | not yet verified |

Read the right-hand column as "what we have tested," not as a risk rating. The
left column is the one that matters for planning: **if your agent talks to m3
over stdio, plan for the possibility that its tools can vanish mid-session.** The
remedy and the data-safety guarantee are identical everywhere, so there is
nothing extra to prepare — reconnect and carry on.

The disconnect is a *client lifecycle* behaviour, so sharing the transport does
not guarantee sharing the bug — each client decides when to tear a server down.
Antigravity's MCP documentation does not publish any idle-timeout or lifecycle
policy for local stdio servers, so we have nothing authoritative to cite; it does
expose "live status rings for active, disconnected, or loading servers" and a
manual reload, which implies disconnection is an anticipated state there too.

Gemini CLI has its own, *different* stdio problems reported upstream (hangs on
`tools/call`, connection failures) — a separate defect with a similar blast
radius, not the one described on this page.

**If you see this on a non-Claude host,** please open an issue with the host name
and version plus `m3 doctor` output. The recovery is whatever that host's
reconnect action is (Antigravity: reload the server from the MCP manager), and
the data-safety story is identical: capture never went through MCP.

## Reconnecting is a low-risk, routine action

Worth stating plainly, because "restart the memory server" *sounds* consequential
and is not. **Reconnecting costs about a second and cannot lose data.**

| | |
|---|---|
| Cost | **~0.9 s** — measured, cold start *and* MCP handshake included (3 runs: 897 / 889 / 825 ms) |
| Data at risk | **none** — see below |
| Blast radius | the one session you run it in |
| Safe to repeat | yes — idempotent; run it as often as you like |
| Needs elevation / restart / config edit | no |

**Why it cannot lose anything: the bridge is stateless.** Every memory, every
captured turn, every embedding lives in the database under your engine root. The
server process is a thin protocol adapter in front of that store — it holds no
memory of its own. A freshly spawned bridge opens the same database and sees the
identical contents; there is no in-process buffer to drain, no session state to
preserve, no handoff to get wrong.

That is also why reconnecting is *not* comparable to restarting a stateful
service. You are replacing a protocol adapter, not bouncing a database.

Concretely, the reconnect action per host:

| Host | Action |
|---|---|
| Claude Code | `/mcp` |
| Antigravity CLI | reload the server from the MCP manager |
| Gemini CLI / OpenCode | the host's MCP reload/reconnect action |

If you are ever unsure whether m3 itself is healthy — as opposed to the client's
view of it — probe the server directly with the command in the section above. It
answers in under a second and is independent of any client.

## What to do

1. **Run `/mcp`** in the affected session. Tools return immediately.
2. **Confirm your memory never stopped recording:**
   ```bash
   m3 chatlog doctor          # exits nonzero on real capture warnings
   ```
   `capture.healthy: true` with a recent `last_write_at` means no data was lost,
   regardless of what the MCP connection was doing.
3. **If tools drop repeatedly**, it is usually a long-running foreground task
   (a big test suite, a long build) leaving the server idle. Nothing is wrong
   with m3; reconnect and continue.

## What we deliberately did *not* do

We considered making m3 always-on by switching the default to the HTTP transport,
which genuinely does survive client drops — m3 already supports it
(`M3_TRANSPORT=http`). **We chose not to make it the default**, because it would
charge every user for a problem that costs no data:

- **It weakens multi-agent isolation.** Under stdio, every agent session gets its
  own server process — independent crash domains. One shared HTTP server means a
  single bad tool call can take down every session at once. Supporting many
  agents across many machines is a core promise; we are not trading it for
  convenience.
- **It widens the security surface.** stdio is a pipe to a child process the
  machine already trusts. An HTTP listener publishes the whole tool catalog —
  `memory_delete` and `gdpr_forget` included — to anything that can reach the
  port. Bearer auth is therefore *mandatory* on that transport (m3 refuses to
  start without a valid token), which means a token to provision, store, and
  rotate. A loopback bind is not proof of privacy: tunnels like cloudflared and
  ngrok bind loopback and publish it.
- **It adds always-on lifecycle.** A permanent server holds database connections
  open when no agent is using it, and extends the window that exclusive database
  operations must reason about.

HTTP remains available as a documented **opt-in** for users who want an always-on
server and accept the port and token management. It is not the default, and we
think that is the right call.

---

*If you hit a variant of this that the page does not explain, please open an
issue with the output of `m3 doctor` and `m3 chatlog doctor`.*
