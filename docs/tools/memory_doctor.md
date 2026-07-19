---
tool: bin/memory_doctor.py
sha1: 9c0149b72926
mtime_utc: 2026-07-19T15:07:24.085774+00:00
generated_utc: 2026-07-19T19:29:22.636880+00:00
private: false
---

# bin/memory_doctor.py

## Purpose

m3-memory doctor — thin CLI dispatcher over the doctor phases.

Phases (each in its own module under bin/doctor/):

  - db_repair          legacy DB fixes (timestamps, relationships, JSON)
  - cascade_probe      embedding-cascade health (delegates to memory.doctor)
  - embed_server_probe Rust-side `m3-embed-server doctor` subprocess
  - oxidation_probe    m3_core_rs native-extension presence/staleness report

Each phase can be skipped via --skip-*. Exit code is the maximum across
the non-skipped phases (most-severe wins). The embed-server and oxidation
phases are report-only and never bump the exit code.

Design note: this file is intentionally thin — narrow CLI + phase
dispatch only. Logic lives in the bin/doctor/ submodules so each can be
tested in isolation.

---

## Entry points

- `def main()` (line 33)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--skip-repair` | Skip the legacy DB-repair phase (read-only health check). | `False` |  | store_true |  |
| `--skip-cascade` | Skip the embedding-cascade health probe. | `False` |  | store_true |  |
| `--skip-embed-server` | Skip the Rust-side m3-embed-server doctor subprocess. | `False` |  | store_true |  |
| `--skip-oxidation` | Skip the m3_core_rs native-extension status report. | `False` |  | store_true |  |
| `--skip-governor` | Skip the governor scheduled-task migration check. | `False` |  | store_true |  |
| `--skip-schedule` | Skip the dangling scheduled-task interpreter check. | `False` |  | store_true |  |
| `--skip-shared-embedder` | Skip the shared-embedder-mode check (config + server + keep-alive task). | `False` |  | store_true |  |
| `--skip-plugin` | Skip the Claude Code plugin version/enabled check. | `False` |  | store_true |  |
| `--skip-agent-paths` | Skip the cross-agent dead-path check (Gemini/OpenCode/Hermes/...). | `False` |  | store_true |  |
| `--skip-dashboard` | Skip the web-dashboard liveness check (registry + port probe). | `False` |  | store_true |  |
| `--verbose` | Show the full detail (DB-repair steps + each probe's expanded report + model-load logs). Default is a compact one-line-per-check summary of high-yield verdicts. | `False` |  | store_true |  |
| `--fix` | Run quick-repair mode to auto-fix common deployment issues. | `False` |  | store_true |  |
| `--dry-run` | Use with --fix to simulate repair steps without making changes. | `False` |  | store_true |  |
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

- `doctor (agent_paths_probe)`
- `doctor (cascade_probe)`
- `doctor (dashboard_probe)`
- `doctor (db_repair)`
- `doctor (embed_server_probe)`
- `doctor (governor_probe)`
- `doctor (oxidation_probe)`
- `doctor (plugin_version_probe)`
- `doctor (schedule_probe)`
- `doctor (shared_embedder_probe)`
- `memory.doctor (memory_doctor_fix_impl)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
