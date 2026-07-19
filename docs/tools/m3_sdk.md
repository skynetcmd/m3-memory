---
tool: bin/m3_sdk.py
sha1: 7de8a162fe05
mtime_utc: 2026-07-19T03:04:59.601522+00:00
generated_utc: 2026-07-19T19:29:22.530652+00:00
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
- `m3_core.context (_CB_COOLDOWN, _CB_THRESHOLD, _CIRCUITS, _CONTEXT_CACHE_SIZE, _CONTEXTS, _CONTEXTS_LOCK, _HTTP_CLIENT, _HTTP_CLIENT_LOCK, _HTTP_CLIENT_LOOP_ID, M3Context, _cleanup, _close_context_pool)`
- `m3_core.governor`
- `m3_core.governor (INITIAL_LIMIT, LIMIT_THRESHOLD, _governor_config_path, _governor_thresholds, ensure_governor_config, get_governor_pacing, pre_execute_interactive_check, register_user_interaction)`
- `m3_core.gpu`
- `m3_core.gpu (_GPU_PROBE_DISABLE, _GPU_PROBE_MAX_MISSES, _GPU_PROBE_TTL, _GPU_PROBES, _gpu_probe_cache, _no_window, probe_gpu_util)`
- `m3_core.locking`
- `m3_core.locking (_MIGRATION_LOCK_MAX_AGE_S, _lock_owner_stamp, _pid_alive, _reclaim_stale_lock, migration_lock)`
- `m3_core.paths`
- `m3_core.paths (_active_db, _db_is_populated, _default_db_path, active_database, add_database_arg, assert_no_deprecated_pg_url_on_install, deprecated_env_in_use, get_m3_config_root, get_m3_engine_root, get_m3_root, getenv_compat, resolve_cdw_pg_dsn, resolve_db_path, resolve_primary_pg_dsn, resolve_venv_python)`
- `m3_core.runtime`
- `m3_core.runtime (LM_READ_TIMEOUT, LM_STUDIO_BASE, M3_CORE_RS_DISABLE, StructuredLogger, ensure_utf8, format_log, logger)`
- `memory.backends (active_backend, dialect)`
- `types`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
