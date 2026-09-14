---
tool: bin/m3_embed_watchdog.py
sha1: d59acb0aff38
mtime_utc: 2026-09-13T23:11:19.578796+00:00
generated_utc: 2026-09-13T23:12:14.182535+00:00
private: false
---

# bin/m3_embed_watchdog.py

## Purpose

m3 shared-embedder watchdog — liveness-based self-heal for all three OSes.

WHY THIS EXISTS (2026-09-13)
The tier-2 shared embed server (:8082) was down for 30 hours and nothing
brought it back. The sequence, reconstructed from the Windows event log:

  1. The server crashed 9 times in ~2.5h inside the GPU driver (nvcuda64.dll,
     0xc0000005) while that driver was in a bad state.
  2. Windows SCM applied its recovery policy — RESTART x3 — and, when the
     crashes kept coming, gave up permanently (event 7023, "Incorrect
     function"). That is SCM working AS DESIGNED.
  3. The driver was reinstalled the next day. The environment was healthy
     again, but SCM's restart budget was already spent, so nothing ever tried
     again. The server stayed stopped until a human noticed.

THE GAP THIS CLOSES — restart budgets have no memory. Every OS supervisor
(SCM FAILURE_ACTIONS, systemd StartLimitBurst, launchd's throttle) is designed
to stop retrying a service that keeps failing, so none of them can recover one
whose ENVIRONMENT healed after the budget was exhausted. A periodic liveness
check has no budget: it asks "is it serving?" every tick, forever, and so it
recovers on the first tick after the environment is fixed.

This is the same lesson as m3_loop_watchdog (a supervisor satisfied by a live
PID cannot see a wedged process) applied one layer out: a supervisor satisfied
by "I already tried" cannot see a healed environment.

DESIGN (§1 three-OS, §3 fail-loud/never-silent, §4 cheap, §10a one owner)
  * Detection is OS- and backend-agnostic: one HTTP GET of the server's own
    /health endpoint, the same contract every tier-2 implementation serves
    (the Rust binary and bin/embed_server_inproc.py are interchangeable here).
    Nothing in this module touches a database, so SQLite vs PostgreSQL is
    irrelevant to it by construction — and nothing imports the embedder, so a
    broken GPU stack cannot take the watchdog down with it.
  * Only the RESTART ACTION is per-OS, isolated in _restart_backend(), exactly
    as m3_loop_watchdog does it. Restart goes through the platform supervisor
    (sc.exe / systemctl / launchctl), never by spawning a server directly: two
    processes on one port is the mutual-exclusion failure the single-:8082
    contract exists to prevent.
  * SHARED MODE ONLY. When .embed_config.json does not enable shared mode this
    is a no-op — an unshared host has per-process tier-1 embedders and no
    :8082 to guard. Guarding a port nobody uses would be a §3 false alarm.
  * Backoff, so a server that crashes on startup is not a fork bomb. The
    budget resets every tick (that is the entire point), but a start is not
    attempted more often than _MIN_RESTART_GAP_S.
  * Degrade, never guess: the server is declared down only after
    _FAILED_CHECKS consecutive misses, so one slow /health during a model load
    does not trigger a needless bounce.

---

## Entry points

- `def main()` (line 255)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `M3_CONFIG_ROOT`
- `M3_MEMORY_ROOT`

---

## Calls INTO this repo (intra-repo imports)

- `_task_runtime (no_window_kwargs)`
- `m3_sdk (get_m3_config_root)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `cmd`` (line 229)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `.embed_config.json`
- `.embed_watchdog_state.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
