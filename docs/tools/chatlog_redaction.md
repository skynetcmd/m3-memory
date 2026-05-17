---
tool: bin/chatlog_redaction.py
sha1: cdfe26c4de7b
mtime_utc: 2026-05-16T19:21:13.935434+00:00
generated_utc: 2026-05-17T15:50:17.535666+00:00
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

- `M3_CORE_RS_DISABLE`

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `m3_core_rs`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
