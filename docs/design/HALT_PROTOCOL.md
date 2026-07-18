# Design: Cooperative Quiesce Protocol (HALT_m3 + PID registry)

> **Status:** proposed (design review before implementation)
> **Motivation:** the installer/upgrader must be able to run a DB-exclusive
> operation (schema migration, backup, `gdpr_forget`, `doctor --repair`) without
> corrupting a WAL-mode database that an autonomous m3 process is actively
> writing. Today it only handles the Windows `mcp-memory.exe` *file-lock*, not
> the broader *DB-open* hazard from the cognitive loop and embed server.

## Problem

Autonomous, headless m3 writers hold the engine-root DBs open in WAL mode:

- **cognitive loop** (`bin/m3_cognitive_loop.py`, `pythonw`/launchd/systemd) —
  continuous entity/enrich/reflect + maintenance writes to core + chatlog.
- **embed server** (`bin/embed_server_inproc.py`) — holds DB connections for
  embed-backfill.
- **MCP server** (`m3`/`mcp-memory`) — serves live reads/writes for the agent.

Running a migration while any of these has the DB open risks a torn WAL /
plausible-but-wrong state. The current preflight (`setup_wizard.py` Probe 2):

- reasons only about the **binary file-lock** (Windows `pip install -e` can't
  overwrite a running `.exe`), not DB safety;
- covers **only** `mcp-memory.exe` by hardcoded image name — blind to the loop
  and embed server;
- is **Windows-only** in its reasoning (`_ok("Unix: ... doesn't block
  reinstall")`), but the DB-open hazard is cross-platform.

Killing by name is also the wrong instrument: a `taskkill` mid-write is exactly
what tears the WAL. And elevated/scheduled writers can't always be stopped
without admin.

## Design principle

**Cooperative, not violent.** The exclusive-op author asks writers to pause;
writers close their *own* DB connections cleanly (WAL `TRUNCATE` checkpoint on
their terms, per §10) and spin-wait. No process is killed in the common path.

This mirrors two established patterns:

- **§3 headless-config rule** — headless launchers don't inherit the installer's
  shell env, so coordination goes through *a file at a fixed config root, read
  live*, not an env var or a signal. `HALT_m3` **is** that file.
- **The governor yield the loop already runs** — `main_loop` already polls
  `get_governor_pacing()` each iteration and pauses on `HALTED`. The HALT check
  is the same yield point with a second trigger; near-zero conceptual cost.

## Two files under `~/.m3/.internal/`

`.internal/` is derived from the **engine root** (`M3_ENGINE_ROOT` >
`M3_MEMORY_ROOT/engine` > `~/.m3/engine`), so the protocol is scoped to the same
DBs the writers actually use — a second engine root (e.g. a separate user
install) has its own independent `.internal/`.

### 1. `PID/` — a directory, one file per process

**Directory-of-files, not a single shared registry file.** A single `PID` file
that every process read-modify-writes races on concurrent add/remove and one
crash can corrupt the whole registry. A directory where each process owns
exactly one file has **no shared-write contention** — a process only ever
creates/deletes its own entry, and one process's crash can't damage another's.

- **Filename:** `<role>.<pid>` — e.g. `cognitive-loop.48213`,
  `embed-server.3268`, `mcp.51992`. The role is legible from the name alone
  (identify a holder without parsing); the `.<pid>` suffix means two instances
  of a role never clobber each other's file, and the reader globs `<role>.*`.
- **Contents (JSON):** `{"pid": int, "role": str, "started_at": ISO8601,
  "engine_root": str, "protocol": 1}`. `engine_root` lets a reader confirm the
  process belongs to *this* root; `protocol` versions the contract.
- **Lifecycle:** write on startup (after the process has opened its DBs),
  best-effort delete on clean exit (atexit + signal handler).
- **Staleness:** a file whose `pid` is **not alive** (OS liveness probe) is
  ignored and reaped. A crash that skips cleanup therefore self-heals on the
  next read — a dead PID is never treated as a live holder. (Guard against PID
  reuse with `started_at`: if the live process at that PID started *after* the
  file's `started_at`, it's a different process → stale.)

### 2. `HALT_m3` — the quiesce semaphore

- **Created by** the exclusive-op author (installer, backup, repair) *before*
  touching the DBs; **removed** when done → writers resume themselves.
- **Contents (JSON):** `{"owner_pid": int, "owner": str, "reason": str,
  "created_at": ISO8601, "protocol": 1}`.
- **Self-clearing on owner death (critical).** A `HALT_m3` left behind by a
  crashed installer must NOT freeze every writer forever — that is the exact
  silent-stall failure mode `CLAUDE.md` warns about. A writer treats the
  semaphore as **void if `owner_pid` is not alive**, and reaps the file. So a
  dead owner's HALT self-clears on the next writer poll.
- **Malformed file → warn loudly, treat as absent** (§3): a corrupt `HALT_m3`
  must never silently pause or silently un-pause; log and proceed as if no halt,
  so a bad file can't wedge the system.

## Writer-side contract

Each autonomous writer, in its main loop (the loop already has the governor
yield point — add the HALT check right beside it):

```
if halt_is_active(engine_root, role):  # live-read, honor only a live owner
    checkpoint_and_close_db()          # PRAGMA wal_checkpoint(TRUNCATE), release pool  (§10)
    deregister(role, engine_root)      # leave PID/ WHILE paused → "not a DB holder"
    while halt_is_active(engine_root, role):
        sleep(short)                   # spin, do NOT exit the process
    register_process(role, engine_root)  # re-register on resume
    reopen_db()                        # resume cleanly
    continue
```

The writer passes its own `role` (the same one it registered in `PID/`) to
`halt_is_active`. Today that argument only ever gates on the master switch; it
exists so per-role granularity is an addable extension, not a rewrite — see
**Granularity** below.

- **Pause the *process*, but leave the *registry* while paused.** The process
  does NOT exit — it spin-waits and resumes when HALT clears (no restart-ledger
  needed). But it removes its `PID/` entry for the duration of the pause and
  re-adds it on resume. This makes the registry mean exactly "who is holding the
  DB right now": a paused writer holds nothing, so it must not appear as a live
  holder. The exclusive op's `wait_for_quiesce` therefore succeeds precisely when
  the registry is empty — a clean, single invariant.
- **Close connections, don't just stop writing** — the point is to release the
  WAL lock so the migration has exclusive access.

## Exclusive-op (installer) flow

```
1. list PID/ entries; drop dead/stale; → set of LIVE holders for this engine_root
2. write HALT_m3 (owner_pid = self)
3. wait up to T seconds for every live holder to release its DB handle
4. all released → run migration → remove HALT_m3 → writers resume
5. NOT all released after T:
     interactive:    prompt the human per stuck holder —
                     "PID <p> (<role>) hasn't paused (may have a task
                      finishing). [K]ill / [W]ait another T / [A]bort?"
                     act on the choice.
     non-interactive: honor the existing opt-in gate —
                      --force-quiesce (or --force-kill-mcp) → kill the stuck PID;
                      otherwise ABORT with a clear message (never silently
                      proceed). Matches today's mcp-memory.exe safe-default.
   On abort or on any error: remove HALT_m3 (don't leave writers wedged).
```

**Why ask the human (step 5 interactive):** the user has context the installer
can't — a long-running enrichment pass that will finish in 20 s should be waited
out, not killed. Unilateral kill or blind proceed both throw away that context.
Non-interactive has no human to ask, so it falls back to the safe-by-default,
explicit-opt-in-to-kill rule the installer already uses.

## Granularity: one master switch, not per-role HALT files

**Decision: `HALT_m3` is the only quiesce switch. We do NOT ship per-role files
(`HALT_cognitive-loop`, `HALT_embed-server`, …).** The API is *parameterized* by
role so per-role granularity is addable later behind the existing seam — but it
is deliberately not populated now. Rationale, so a future author doesn't
re-litigate it or bolt on per-role files reflexively:

- **No exclusive op wants partial quiescing.** Every DB-exclusive operation —
  migration, backup, `gdpr_forget`, `doctor --repair` — needs *all* writers off
  the DB, because they share one WAL. "Pause the loop but let the embed server
  keep writing" is not a coherent state for a DB-exclusive op: if the DB must be
  quiesced, it must be quiesced for everyone holding it. Per-role HALT serves no
  caller of *this* protocol.

- **The real "stop just the loop" need is a different contract, served
  elsewhere.** Wanting to pause only the cognitive loop (it's using the GPU
  during a game / a demo) is a **runtime-control** need, not a DB-exclusive-op
  need. Its correct semantics are "stop dispatching work" — the writer should
  *keep* its DB handle and keep honoring the governor — which is the opposite of
  HALT's "close your DB connections, a migration is coming." That need is already
  served by the **governor** (`get_governor_pacing → HALTED`) and the
  `M3_*_AUTO` gates, and should stay there. Overloading HALT with both contracts
  is the fat-abstraction failure mode DESIGN_PHILOSOPHIES §1 warns against
  ("a document store does NOT fit this seam and must not be forced into it").

- **Cost now would be real, benefit zero (YAGNI, §2/§5).** Per-role files force
  every writer to poll N files with precedence rules (does `HALT_m3` override
  `HALT_cognitive-loop`? both present?), and multiply the stale-file /
  owner-PID reaping logic — combinatorial surface for a feature with no caller
  and no pre-registered outcome (§5).

**The extension seam (already in the API, unpopulated):**

```
set_halt(owner, reason, engine_root, targets="*")   # only "*" (all) is implemented today
halt_is_active(engine_root, role) -> bool           # role checks the master switch now;
                                                     # a later HALT_<role> is one function's change
```

Because the writer already threads its `role` through `halt_is_active`, adding
`HALT_<role>` later touches only that one resolver — no writer-contract churn,
no new polling in the loops. If a concrete per-writer *pause* need arises first,
prefer serving it via the governor/runtime-control path; reach for `HALT_<role>`
only if that need genuinely wants HALT's close-your-connections semantics.

## Backend & topology scope (SQLite / PostgreSQL)

The protocol is **per-engine-root**, and the engine root is always a *local
filesystem directory* (`get_m3_engine_root()`: `M3_ENGINE_ROOT` >
`M3_MEMORY_ROOT/engine` > `~/.m3/engine`) — resolved with **no reference to
`M3_DB_BACKEND`**. Coordination files live where the *processes and installer*
run, not where the *data* lives. That makes the design backend-agnostic by
construction:

- **SQLite (local, default):** the original case. Writers hold the local WAL DB
  open; the exclusive op needs them off. Fully covered.
- **PostgreSQL as primary store, single box:** `.internal/` is local on that
  box; the local writers still coordinate through it. The **WAL-checkpoint step
  self-no-ops on PG** (`_checkpoint_wal` returns early when
  `active_backend().name != "sqlite"` — PG manages its own WAL), so the loop's
  HALT block runs unchanged and simply skips the SQLite-only flush (§1: same
  code path, degrades correctly). Quiescing is still valuable — you don't want
  the loop issuing writes/DDL-contending work mid-migration.
- **PG warehouse-sync tier (local SQLite + CDW fan-in):** the *local* SQLite is
  what needs quiescing for a local op; covered as the SQLite case. CDW sync is a
  separate subsystem, out of this protocol's scope.

**Explicit boundary — shared multi-box PG (per-clinic scale topology):** because
coordination is per-box, `HALT_m3` on box A does **not** quiesce a writer on
box B hitting the *same* Postgres cluster. This is the correct boundary, not a
gap: the **installer is inherently per-box** (`m3 setup` upgrades one machine's
payload and only needs to quiesce the writers it is about to disrupt — the local
ones sharing its files/venv). A coordinated cluster-wide schema migration is a
*different operation* that belongs to cluster maintenance tooling, above this
protocol. If that need arises, the extension is a fan-out (raise HALT in each
box's `.internal/`) or DB-level advisory locks — deliberately **out of scope
here**, noted so a future author doesn't mistake the per-box boundary for a bug.

## Module seam

New module `bin/m3_halt.py` (or `m3_sdk` submodule if writers import it without
`bin` on path — TBD in review), pure-stdlib, cross-platform:

- `register_process(role, engine_root) -> Path` / `deregister(...)`
- `list_live_processes(engine_root) -> list[ProcInfo]`  (reaps stale)
- `set_halt(owner, reason, engine_root, targets="*") -> Path` / `clear_halt(engine_root)`
  (`targets` is `"*"`-only today — the per-role extension point, see Granularity)
- `halt_is_active(engine_root, role) -> bool`  (honors only a live owner; warns
  on malformed; `role` gates the master switch now, a later `HALT_<role>` behind
  the same signature)
- `wait_for_quiesce(engine_root, timeout) -> (ok, list[stuck ProcInfo])`
- `_pid_is_alive(pid, started_at) -> bool`  (OS probe + reuse guard)

Liveness probe is cross-platform per §1 (Windows / macOS / Linux) — no
`nvidia-smi`-style single-OS assumption. Reuse the existing
`_find_running_mcp_memory_processes` /`_kill_process_windows` helpers for the
kill fallback; generalize discovery to the PID registry.

## Tenet checklist (§12c pre-flight)

- **§1 cross-platform:** file-based coordination + multi-OS liveness probe;
  no shell-env, no signals (Windows-hostile), no single-OS image-name grep.
- **§3 fail-loud/safe:** malformed HALT/PID → warn + treat safely; dead owner →
  auto-void; never silently freeze or silently proceed.
- **§6 hardening:** destructive kill stays gated behind an explicit opt-in /
  human prompt; the semaphore is the non-destructive default.
- **§10 WAL:** writers `wal_checkpoint(TRUNCATE)` on pause; exclusive op gets a
  clean, checkpointed DB.
- **§12c ownership:** this closes a real cross-platform footgun (the DB-open
  hazard) rather than noting it.

## Resolved decisions (review answers)

1. **Module home → `bin/m3_halt.py`.** Both writers already
   `sys.path.insert(0, bin/)` and bare-import `m3_sdk`/`m3_enrich`/
   `chatlog_config` from `bin/`; `m3_sdk` is itself `bin/m3_sdk.py`, not a
   top-level package. `bin/m3_halt.py` matches that convention and reuses
   `m3_sdk.get_m3_engine_root()` for the `.internal/` path. An `m3_sdk`
   submodule would add indirection for no gain.
2. **Timeout T default → 30 s, config-backed.** 10 s is shorter than the loop's
   own 10 s thermal pause and too tight for an in-flight enrich batch. Default
   lives in a code-resolved config with a `--quiesce-timeout` override (§3), not
   an env var.
3. **Rollout → cognitive loop + installer first, in this PR.** The loop is the
   highest-value writer and already has the governor-yield hook (main_loop, the
   `pacing_cpu_ram HALTED` check). Embed-server and MCP-server HALT-honoring are
   a fast follow — keeps this PR to one feature (§2).
4. **Consumers → installer-only now.** Backup / `gdpr_forget` / `doctor
   --repair` reuse the same module later; the API is built for it, but wiring
   every consumer now would break one-feature-per-PR.
