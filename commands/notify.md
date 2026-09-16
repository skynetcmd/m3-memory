---
name: notify
description: Poll the inbox for new notifications addressed to you.
---

Poll via the **CLI**, not an MCP tool call. `notifications_poll` lives in m3's
`admin` domain, and even after `tools_load_domain(admin)` it is often not
invokable from an LLM-facing session. `m3_call` is not a fallback: the
anti-spoofing guard refuses an LLM-facing caller that addresses an arbitrary
`agent_id` and fails with *"caller's identity was refused"*. The CLI is the
supported path.

## 1. Resolve your inbox

Notifications are addressed to an `agent_id`, so you need yours before polling:

```bash
m3 agent agent_list
```

Claude Code polls `claude-code`. Sessions may also register a per-session id
(`claude-code@<short>`) — if one exists for this session, **poll both**, since a
sender may have addressed either. Ignore other agents' inboxes (`agy`, `probe-*`).

## 2. Poll

```bash
m3 admin notifications_poll --agent_id <id> --limit 20 --as_records
```

`--as_records` returns JSON; omit it for a one-line-per-item display string.
Records look like:

```json
{"count": 1, "items": [{"id": 1270, "kind": "test_message",
  "created_at": "2026-09-16T00:46:19+00:00", "read_at": null, "received_at": null,
  "payload": {"message": "...", "severity": "info", "_from": {"agent": "claude-code"}}}]}
```

Only `id`, `kind`, `created_at`, `read_at`, `received_at` and `payload` are
guaranteed. **Severity, message and sender are not top-level fields** — they are
conventions inside `payload` (sender is `payload._from.agent`, and `_from.session`
when the sender set one). Any of them may be absent.

Render each notification as:
```
[<payload.severity or kind>] <created_at>  from <payload._from.agent or "unknown">:
  <payload.message, else the payload compactly>
  ack: m3 admin notifications_ack --notification_id <id>
```

If `notifications_poll` reports the agent is **not registered**, the inbox result
is still valid — that note only means the poll recorded no heartbeat.

## 3. Ack

Only after listing, ask whether to ack. Never ack unprompted — an unread
notification no one has read is the signal.

**Ack by specific id. Always.**

```bash
m3 admin notifications_ack --notification_id <id>
```

⚠ **Do not use `notifications_ack_all` after reading or replying.** It acks
everything unread *at the moment it runs* — including messages that arrived while
you were composing, which you have never seen. `notifications` carries a single
timestamp, `read_at`; that flag was the only record the work was pending, so there
is nothing to recover from and a lost message is indistinguishable from one you
read and declined to answer. (Measured: AGY lost a message this way on
2026-09-15.)

Reserve `ack_all` for deliberately dumping a backlog you have decided not to
read, and never chain it after a reply. If you only need to confirm delivery, use
`m3 admin notifications_mark_received --agent_id <id>` — it stamps `received_at`,
is idempotent, and never touches `read_at`, so unread messages stay unread.

Neither ack command needs `--yes`.

Empty inbox is the expected state — say "inbox is clear" rather than printing nothing.
