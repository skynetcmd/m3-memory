---
tool: bin/memory_doctor.py
sha1: 131f9e5af4cd
mtime_utc: 2026-05-30T18:38:21.561007+00:00
generated_utc: 2026-05-31T18:42:52.888348+00:00
private: false
---

# bin/memory_doctor.py

## Purpose

m3-memory doctor — thin CLI dispatcher over the three doctor phases.

Phases (each in its own module under bin/doctor/):

  - db_repair          legacy DB fixes (timestamps, relationships, JSON)
  - cascade_probe      embedding-cascade health (delegates to memory.doctor)
  - embed_server_probe Rust-side `m3-embed-server doctor` subprocess

Each phase can be skipped via --skip-*. Exit code is the maximum across
the non-skipped phases (most-severe wins).

Design note: this file is intentionally thin — narrow CLI + phase
dispatch only. Logic lives in the bin/doctor/ submodules so each can be
tested in isolation.

---

## Entry points

- `def main()` (line 31)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--skip-repair` | Skip the legacy DB-repair phase (read-only health check). | `False` |  | store_true |  |
| `--skip-cascade` | Skip the embedding-cascade health probe. | `False` |  | store_true |  |
| `--skip-embed-server` | Skip the Rust-side m3-embed-server doctor subprocess. | `False` |  | store_true |  |
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None | Falls back to M3_DATABASE env then memory/agent_memory.db. | str | Routes all DB reads/writes against PATH for this run. |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (add_database_arg)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `doctor (cascade_probe)`
- `doctor (db_repair)`
- `doctor (embed_server_probe)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
