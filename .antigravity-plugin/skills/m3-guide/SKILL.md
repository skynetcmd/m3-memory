---
name: m3-guide
description: How to use m3 memory well — search-first protocol, write vs supersede, chatlog access and its store-topology trap, and verifying capture is live. Use when working with m3 memory tools or unsure which one fits.
---

# Using m3 Memory

Tool names and signatures come from the MCP tool list. This is the part that
isn't in a tool description: the protocol, and the traps.

## Trust order

**m3 memory > artifacts (git, handoff files) > session context.**
When m3 contradicts what you think you remember, trust m3 — context degrades
across session boundaries, memory does not.

## Verify capture is live

**Registered ≠ working.** The server can be connected while chatlog capture is
silently dead. Call `chatlog_status` once early in a substantive session. If
hooks are off or `last_write` is null/stale, say so loudly and don't proceed
silently — a dead chatlog means this session's decisions vanish at the next
session boundary. Never degrade quietly to flat files.

## Protocol

- **Search first.** `memory_search` before re-deriving anything about this user,
  project, or machine. A settled decision re-litigated is a bug reintroduced.
- **Write as you go.** If it took effort to learn and will matter later,
  `memory_write` it — decisions, corrections, runbooks, preferences.
- **Update, don't duplicate.** Search before writing; prefer `memory_update` or
  `memory_supersede` (retires the old claim with a `supersedes` edge, keeps
  history) over a near-duplicate. Duplicates degrade later retrieval.
- **Link.** Reference related memories as `[[name]]` so `memory_graph` traversal
  stays useful.

## Chat log

Captured turns from host agents live in a store separate from curated memory.
Reach them with `chatlog_search`, `chatlog_list_conversations`, and
`chatlog_promote` (promotes turns into curated memory).

### ⚠️ Never open a database file directly

The chat store and main store may be **two databases, one unified database, or
PostgreSQL** — a deployment choice only the tools know.

Querying `agent_memory.db` for `type='chat_log'` returns **zero rows on a split
deployment even when capture is perfectly healthy** — a false emergency. On a
PostgreSQL primary there's no local file at all. Use `chatlog_status`;
`m3 chatlog status --json` reports `unified: true|false` if you need the
topology.

## Scoping

Passing `conversation_id`, `agent_filter`, `user_id`, or `scope` enforces the
filter strictly: an empty result means nothing matched *in that scope*, not that
the store is empty.

## More

Only some tools load at startup — `tools_list_domains` / `tools_load_domain`
reach the rest (100+ in total). Health: `m3 doctor`, `m3 chatlog doctor`.
Full reference: [`docs/AGENT_INSTRUCTIONS.md`](https://github.com/skynetcmd/m3-memory/blob/main/docs/AGENT_INSTRUCTIONS.md)
