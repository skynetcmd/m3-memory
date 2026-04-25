---
name: export
description: GDPR Article 20 — export all memories you own as portable JSON.
---

Call `m3:gdpr_export`. The tool returns the full memory set as JSON.

Save it to `~/.m3-memory/export-$(date +%Y%m%d-%H%M%S).json` and tell the user:
- where it landed
- how many memories it contains
- one-sentence note that the file is portable: another m3-memory instance can `gdpr_import` it
