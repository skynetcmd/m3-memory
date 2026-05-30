---
name: m3-notify
description: Poll the inbox for new notifications addressed to you.
---
# M3 Notify

## When to Use
Use this skill when you start a session, at logical breakpoints, or when you want to poll the notification queue for messages, handoffs, task updates, or maintenance requests sent to you.

## Instructions
Call `m3:notifications_poll`.

Render each notification as:
```
[<severity>] <created_at>  from <sender>:
  <message>
  ack: m3:notifications_ack with id=<id>
```

After listing, ask if the user wants to ack any (call `notifications_ack`) or ack-all (`notifications_ack_all`).

Empty inbox is the expected state — say "inbox is clear" rather than printing nothing.
