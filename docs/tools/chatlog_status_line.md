---
tool: bin/chatlog_status_line.py
sha1: c47701e2cf45
mtime_utc: 2026-04-18T22:28:14.291348+00:00
generated_utc: 2026-05-01T13:05:26.759841+00:00
private: false
---

# bin/chatlog_status_line.py

## Purpose

chatlog_status_line.py — anomaly-only status line generator.

Keystroke-fast: reads state file only, no DB. Prints one tag or nothing.
Exit 0 always.

Shows highest-severity anomaly when multiple fire.
Order: regex_errors > silent_hook > spill > queue_backpressure > embed_backlog.

Respects env:
- CHATLOG_STATUSLINE=off → no output
- CHATLOG_STATUSLINE_ASCII=1 → use [!] instead of ⚠

---

## Entry points

- `def main()` (line 102)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `CHATLOG_STATUSLINE`
- `CHATLOG_STATUSLINE_ASCII`

---

## Calls INTO this repo (intra-repo imports)

- `chatlog_config`

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
