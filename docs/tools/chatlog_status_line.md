---
tool: bin/chatlog_status_line.py
sha1: fc004938fc06
mtime_utc: 2026-08-07T23:53:51.810688+00:00
generated_utc: 2026-08-08T14:40:49.762630+00:00
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

- `def main()` (line 104)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `chatlog_config`
- `m3_sdk (getenv_compat)`

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
