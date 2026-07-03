---
tool: bin/m3_sdk.py
sha1: cdd76b5cc3ea
mtime_utc: 2026-07-02T21:51:11.647462+00:00
generated_utc: 2026-07-03T20:00:03.615677+00:00
private: false
---

# bin/m3_sdk.py

## Purpose

m3_sdk — facade. Real implementations live in bin/m3_core/*.
Kept as the stable import surface for ~60 callers. Do not add logic here.

---

## Entry points

_(no conventional entry point detected)_

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `m3_core.context`
- `m3_core.context (M3Context, _cleanup, _close_context_pool, _CIRCUITS, _CB_THRESHOLD, _CB_COOLDOWN, _HTTP_CLIENT, _HTTP_CLIENT_LOOP_ID, _HTTP_CLIENT_LOCK, _CONTEXT_CACHE_SIZE, _CONTEXTS, _CONTEXTS_LOCK)`
- `m3_core.governor`
- `m3_core.governor (INITIAL_LIMIT, LIMIT_THRESHOLD, register_user_interaction, get_governor_pacing, pre_execute_interactive_check, ensure_governor_config, _governor_thresholds, _governor_config_path)`
- `m3_core.gpu`
- `m3_core.gpu (probe_gpu_util, _GPU_PROBE_DISABLE, _GPU_PROBE_TTL, _gpu_probe_cache, _GPU_PROBE_MAX_MISSES, _GPU_PROBES, _no_window)`
- `m3_core.locking`
- `m3_core.locking (migration_lock, _MIGRATION_LOCK_MAX_AGE_S, _lock_owner_stamp, _pid_alive, _reclaim_stale_lock)`
- `m3_core.paths`
- `m3_core.paths (resolve_venv_python, get_m3_root, get_m3_config_root, get_m3_engine_root, resolve_db_path, active_database, add_database_arg, getenv_compat, deprecated_env_in_use, _active_db, _db_is_populated, _default_db_path)`
- `m3_core.runtime`
- `m3_core.runtime (format_log, logger, M3_CORE_RS_DISABLE, ensure_utf8, LM_STUDIO_BASE, LM_READ_TIMEOUT, StructuredLogger)`
- `types`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
