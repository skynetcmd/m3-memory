---
tool: bin/chatlog_redaction.py
sha1: a6f9dcd842ef
mtime_utc: 2026-04-21T19:15:03.919848+00:00
generated_utc: 2026-05-01T13:05:26.754907+00:00
private: false
---

# bin/chatlog_redaction.py

## Purpose

Optional secret-scrubbing for chat log entries.

Scans content with pre-compiled regex patterns for common secret formats
and replaces matches with [REDACTED:<group>]. Disabled by default.

---

## Entry points

- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
