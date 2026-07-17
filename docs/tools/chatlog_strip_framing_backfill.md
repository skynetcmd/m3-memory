---
tool: bin/chatlog_strip_framing_backfill.py
sha1: 1ac4efd9404f
mtime_utc: 2026-07-13T03:57:28.562666+00:00
generated_utc: 2026-07-17T02:18:40.477683+00:00
private: false
---

# bin/chatlog_strip_framing_backfill.py

## Purpose

chatlog_strip_framing_backfill.py — one-off backfill that removes harness
control framing (<system-reminder> / <task-notification> blocks) from EXISTING
chat_log rows.

Why this exists (companion to the write-path fix in chatlog_core):
    chatlog_redaction.strip_harness_framing() is wired into the two chatlog
    WRITE paths, so new turns are stripped as they land. But rows captured
    BEFORE that fix still carry the blocks verbatim. Because chatlog search
    returns stored turns as DATA, a persisted <system-reminder> (e.g. a genuine
    "the date has changed … do not mention this to the user") re-surfaces later
    reading like a LIVE instruction — indistinguishable from a prompt-injection
    payload, which is exactly what a curation subagent tripped over.

    chatlog_rescrub_impl does NOT fix these rows: it runs scrub() (the optional,
    config-gated SECRET redaction), not strip_harness_framing(). The two were
    deliberately kept separate — framing removal is structural block deletion,
    not secret regex→[REDACTED] substitution. So clearing legacy framing needs
    its own backfill: this script.

Safety:
    * DRY-RUN by default. Nothing is written unless --apply is passed.
    * Does NOT require redaction.enabled — framing stripping is independent of
      secret redaction (mirrors the unconditional call in chatlog_core).
    * Idempotent: strip_harness_framing() returns count 0 on already-clean
      content, so re-running is a no-op on rows already handled.
    * Read-only DRY-RUN opens no write transaction; --apply updates only rows
      that actually change, and records provenance in metadata_json:
      original_content_sha256 (once), harness_framing_stripped=True,
      harness_blocks_removed (cumulative).

Usage:
    python bin/chatlog_strip_framing_backfill.py                 # dry-run, all rows
    python bin/chatlog_strip_framing_backfill.py --apply         # apply to all rows
    python bin/chatlog_strip_framing_backfill.py --conversation-id <id> --apply
    python bin/chatlog_strip_framing_backfill.py --since 2026-06-01 --until 2026-07-01
    python bin/chatlog_strip_framing_backfill.py --limit 100 --verbose --apply

---

## Entry points

- `def main()` (line 155)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--apply` | Write changes. Without this, runs a dry-run (default). | `False` |  | store_true |  |
| `--conversation-id` | Limit to one conversation. | `` |  | str |  |
| `--since` | Only rows with created_at >= this ISO timestamp. | `` |  | str |  |
| `--until` | Only rows with created_at <= this ISO timestamp. | `` |  | str |  |
| `--limit` | Max rows to scan (default 100000). | `100000` |  | int |  |
| `--verbose` | Include per-row samples (first 20 changed rows). | `False` |  | store_true |  |
| `--json` | Emit the summary as JSON. | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `chatlog_config`
- `chatlog_core (_content_hash, _utcnow_iso)`
- `chatlog_redaction`
- `m3_sdk (M3Context)`

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
