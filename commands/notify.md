---
name: notify
description: Poll the inbox for new notifications addressed to you.
---

Call `m3:notifications_poll`.

Render each notification as:
```
[<severity>] <created_at>  from <sender>:
  <message>
  ack: m3:notifications_ack with id=<id>
```

After listing, ask if the user wants to ack any (call `notifications_ack`) or ack-all (`notifications_ack_all`).

Empty inbox is the expected state — say "inbox is clear" rather than printing nothing.
