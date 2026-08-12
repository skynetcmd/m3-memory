---
tool: bin/m3_loop_watchdog.py
sha1: 5cb38d1819f9
mtime_utc: 2026-08-09T19:41:32.422028+00:00
generated_utc: 2026-08-12T00:59:01.549116+00:00
private: false
---

# bin/m3_loop_watchdog.py

## Purpose

m3 cognitive-loop watchdog — progress-based self-heal for all three OSes.

WHY THIS EXISTS (2026-08-09)
The loop was down ~4h after an upgrade and nothing noticed. Three failure modes;
the platform supervisors cover only the first two, and only on some platforms:

  1. CLEAN EXIT. An upgrade raises HALT_m3; the loop checkpoints and exits 0.
     macOS shipped KeepAlive={Crashed:true}, which by design ignores a clean
     exit, so the loop stayed dead. Linux (Restart=always) and Windows (the
     ONSTART task's self-heal Repetition) were never exposed to this; the plist
     is now KeepAlive=true, bringing macOS to parity.
  2. CRASH. Covered everywhere: KeepAlive / Restart=always / task repetition.
  3. WEDGED BUT ALIVE. Livelocked work-gate, stuck pass, blocked on a DB lock.
     The process exists, so every supervisor on every platform is satisfied and
     the loop does nothing forever. NOTHING covers this — it is why this file
     exists, and it is why the check is PROGRESS-based (heartbeat), not
     liveness-based (pid exists).

DESIGN (§2 modularity, §3 fail-safe, §5 effectiveness)
  * Detection is OS- and backend-agnostic: a heartbeat file the loop stamps at
    each cycle boundary. Only the RESTART ACTION is per-OS, isolated in
    _restart_backend(). Nothing here touches a database, so SQLite vs
    PostgreSQL is irrelevant to it by construction.
  * Restart goes through the platform supervisor (launchctl kickstart /
    systemctl --user restart / schtasks /End+/Run), never by spawning the loop
    directly — two writers on one WAL DB is precisely what the single-instance
    lock and the halt protocol exist to prevent.
  * HALT_m3-aware via m3_halt.halt_is_active(), the shared protocol reader (it
    already voids a halt whose owner died). Re-parsing HALT_m3 here would let
    this drift from the protocol it must honor. Restarting into an exclusive op
    is the torn-WAL case the protocol exists to prevent.
  * Degrade, never guess: a MISSING heartbeat is "unknown", not "dead" — an
    upgrade predating the heartbeat would otherwise cause an endless restart
    loop. Falls back to the m3_halt PID registry for liveness.
  * Import-fragile by necessity, so imports are guarded: if the venv is
    half-upgraded and m3_sdk/m3_halt won't import, the watchdog still resolves
    roots from the documented env vars and keeps working. It is least useful
    exactly when it is most needed otherwise.

---

## Entry points

- `def main()` (line 266)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `M3_CONFIG_ROOT`
- `M3_ENGINE_ROOT`
- `M3_MEMORY_ROOT`

---

## Calls INTO this repo (intra-repo imports)

- `_task_runtime (no_window_kwargs)`
- `m3_halt`
- `m3_sdk (get_m3_config_root, get_m3_engine_root)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `cmd`` (line 239)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `.loop_heartbeat.json`
- `.loop_watchdog_state.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
