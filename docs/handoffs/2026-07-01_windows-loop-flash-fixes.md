# Handoff: Windows cognitive-loop + console-flash fixes → macOS UX parity

**Date:** 2026-07-01 · **From:** Claude on the Windows host · **For:** the macOS session building UX parity

All changes below are **merged to `main`** (PRs #64–#69) — you have them via `git pull`.
This file exists because m3 memories may not be synced to a store you can reach; the
repo is the reliable channel.

---

## What was fixed on Windows (and what carries to macOS)

### 1. GPU bursts — CROSS-PLATFORM, already applies to macOS (#66)
`bin/m3_cognitive_loop.py`: `--limit-per-pass` default **50 → 1**. The heavy
local-LLM passes (entity extraction / enrichment / observation drain) now do ONE
item, then yield and re-check the governor, instead of dispatching 50 back-to-back
and pinning the GPU for ~17 min. This is shared code — the launchd job inherits
`limit=1` automatically (the plist passes no explicit `--limit-per-pass`).

Also: `bin/chatlog_embed_sweeper.py` gained `--deadline` (soft per-run wall-clock,
default 60s; converts a duration to an ABSOLUTE `time.monotonic()+d` deadline —
a raw duration reads as already-elapsed). `install_schedules.py` passes
`--deadline 60` to the embed task and lowered ObservationDrain `--drain-batch` 200→8.
**On macOS, pass the same `--deadline` to the embed job.**

Metal GPU probe already exists (`m3_sdk.probe_gpu_util` has a macOS/ioreg branch),
so GPU-load gating works on Apple Silicon.

### 2. Self-heal — Windows uses schtasks XML; macOS gets it cleaner via launchd (#64/#65)
Windows: tasks registered via `schtasks /Create /XML` (removed the old PowerShell
hardening step). `AgentOS_CognitiveLoop` is ONSTART with BOTH BootTrigger +
LogonTrigger, each carrying a 30-min `Repetition` (self-heal), plus
`MultipleInstances=IgnoreNew` and `ExecutionTimeLimit=PT0S`.

**macOS equivalent — the parity task for you:** the launchd plist for the cognitive
loop should set `<key>RunAtLoad</key><true/>` AND `<key>KeepAlive</key><true/>`.
**KeepAlive IS the self-heal** — launchd auto-restarts a dead job immediately
(cleaner than Windows' 30-min repetition, which only exists because Task Scheduler
has no KeepAlive). Do NOT set a hard runtime limit. Check
`bin/install_schedules.py::install_unix_cognitive_loop()` — confirm it emits
`KeepAlive`; if not, that's the gap to close.

### 3. Cross-platform `--verify` (#67)
`python bin/install_schedules.py --verify [NAME]` reads back the LIVE registered job
and checks it matches the spec:
- **macOS:** launchd agent installed AND loaded; warns if the plist lacks `KeepAlive`.
- **Linux:** systemd `--user` unit installed AND active.
- **Windows:** schtasks XML has IgnoreNew + the expected Repetition.

`bin/deploy_cognitive_loop.ps1` is Windows-only (carries elevation to re-register an
admin-owned task). **macOS needs no such wrapper** — just
`install_schedules.py --add cognitive-loop` then `--verify cognitive-loop`.

### 4. Console-window flashes — mostly Windows-specific, but two cross-platform wins (#68/#69)
Windows `CREATE_NO_WINDOW` fixes (no macOS analogue needed — no console-flash issue
on macOS). BUT `bin/statusline-command.sh` got two **cross-platform** improvements
you inherit:
- Dropped the `whoami` subprocess → uses `USER`/`USERNAME` env.
- Dropped `hostname -s` (crashes on Windows; unnecessary everywhere) → uses
  `socket.gethostname()`. Pure Python, fewer subprocess deps on every OS.

---

## GPU-load diagnostic lesson (both OSes)
"100% GPU util" at LOW power draw is NOT saturation — it's tiny constant pokes/spin.
On Windows, LM Studio inference shows under Task Manager's "3D" engine (not "Compute"),
and LM Studio's own meter under-reads vs whole-GPU util. **Judge real load by POWER
DRAW + temp + engine, not util%.** macOS: `sudo powermetrics --samplers gpu_power`
or the Metal branch of `probe_gpu_util`.

---

## Open / unresolved (for awareness)
- **One flash remains** on the Windows host when an item is fed to the LLM — ruled out the entity
  path (no subprocess), the HTTP LLM call, and the statusline. Hypothesis: a cached
  old statusline in the running session (reboot clears it) or LM Studio's own helper.
  Being retested after a the Windows host reboot. Not a macOS concern.
- On the Windows host only 2 of 7 m3 scheduled tasks were actually registered (`--verify` found
  this). Not relevant to macOS beyond: run `--verify all` after install to catch drift.
