"""m3_sdk — facade. Real implementations live in bin/m3_core/*.
Kept as the stable import surface for ~60 callers. Do not add logic here."""
import sys as _sys
import types as _types

# In the pre-split monolith, callers and tests reached module globals as
# m3_sdk.NAME for both reads AND writes, and the functions that used those globals
# read them from the very same module namespace. After the split the
# implementations live in bin/m3_core/*, so re-exporting a REBINDABLE name into
# this facade creates a SEPARATE binding: a write to m3_sdk.NAME (a scalar
# reassignment like _LAST_USER_INTERACTION, or monkeypatch.setattr(m3_sdk, X, ...)
# in the test suite) would not be seen by the owning submodule, and the code would
# keep reading the stale value. To preserve byte-identical behavior, the facade
# routes writes THROUGH to every submodule whose functions read that name from
# their OWN namespace, and reads THROUGH from the canonical owner.
#
# Scope is deliberately a fixed ALLOWLIST of the names that (a) are rebound at
# runtime or by tests AND (b) are read by implementation code via a bare
# module-global reference. Everything else — dunders (__spec__ during
# importlib.reload), plain functions/classes, and mutable objects shared by
# reference (dicts like _CIRCUITS, the _active_db ContextVar) — uses normal module
# attribute semantics. Keeping the proxy narrow avoids clobbering importlib.reload
# and internal attributes.
import m3_core.context as _context  # noqa: E402,F401
import m3_core.governor as _governor  # owns the mutable _LAST_USER_INTERACTION scalar
import m3_core.gpu as _gpu  # noqa: E402
import m3_core.locking as _locking  # noqa: E402
import m3_core.paths as _paths  # noqa: E402
import m3_core.runtime as _runtime  # noqa: E402
from m3_core.context import (  # noqa: F401
    _CB_COOLDOWN,
    _CB_THRESHOLD,
    _CIRCUITS,
    _CONTEXT_CACHE_SIZE,
    _CONTEXTS,
    _CONTEXTS_LOCK,
    _HTTP_CLIENT,
    _HTTP_CLIENT_LOCK,
    _HTTP_CLIENT_LOOP_ID,
    M3Context,
    _cleanup,
    _close_context_pool,
)
from m3_core.governor import (  # noqa: F401
    INITIAL_LIMIT,
    LIMIT_THRESHOLD,
    _governor_config_path,
    _governor_thresholds,
    ensure_governor_config,
    get_governor_pacing,
    pre_execute_interactive_check,
    register_user_interaction,
)
from m3_core.gpu import (  # noqa: F401
    _GPU_PROBE_DISABLE,
    _GPU_PROBE_MAX_MISSES,
    _GPU_PROBE_TTL,
    _GPU_PROBES,
    _gpu_probe_cache,
    _no_window,
    probe_gpu_util,
)
from m3_core.locking import (  # noqa: F401
    _MIGRATION_LOCK_MAX_AGE_S,
    _lock_owner_stamp,
    _pid_alive,
    _reclaim_stale_lock,
    migration_lock,
)
from m3_core.paths import (  # noqa: F401
    _active_db,
    _db_is_populated,
    _default_db_path,
    active_database,
    add_database_arg,
    deprecated_env_in_use,
    get_m3_config_root,
    get_m3_engine_root,
    get_m3_root,
    getenv_compat,
    assert_no_deprecated_pg_url_on_install,
    resolve_cdw_pg_dsn,
    resolve_db_path,
    resolve_primary_pg_dsn,
    resolve_venv_python,
)
from m3_core.runtime import (  # noqa: F401
    LM_READ_TIMEOUT,
    LM_STUDIO_BASE,
    M3_CORE_RS_DISABLE,
    StructuredLogger,
    ensure_utf8,
    format_log,
    logger,
)

# name -> submodules whose namespace must observe a rebind of that name. The
# first entry is the canonical read source used by the facade's own __getattr__.
_ROUTED = {
    # Rust fast-path toggle: runtime owns it; governor/locking/context each
    # imported it and read it via a bare global.
    "M3_CORE_RS_DISABLE": (_runtime, _governor, _locking, _context),
    # paths owns it; governor + locking imported it and call it by bare name.
    "get_m3_config_root": (_paths, _governor, _locking),
    # governor-owned globals read within governor's own functions.
    "_LAST_USER_INTERACTION": (_governor,),
    "_governor_thresholds": (_governor,),
    "_GOV_CFG_TTL": (_governor,),
    "_gov_cfg_cache": (_governor,),
    # gpu-owned globals read within gpu's own functions.
    "_GPU_PROBES": (_gpu,),
    "_GPU_PROBE_DISABLE": (_gpu,),
    "_GPU_PROBE_TTL": (_gpu,),
    "_GPU_PROBE_MAX_MISSES": (_gpu,),
    "_gpu_probe_cache": (_gpu,),
}


class _Facade(_types.ModuleType):
    def __getattr__(self, name):
        # Reached only for names absent from this module's own __dict__ — i.e.
        # names we intentionally did NOT import so reads route to the live owner.
        targets = _ROUTED.get(name)
        if targets is not None:
            return getattr(targets[0], name)
        # Lazy facade re-export of the backend seam accessors. These are resolved
        # on ACCESS, never at module load, because ``memory.backends`` is DOWNSTREAM
        # of m3_sdk (selector.py / postgres_backend.py top-level-import m3_sdk) — a
        # top-level ``from memory.backends import dialect`` here would close the
        # cycle ``m3_sdk -> backends -> selector -> m3_sdk`` (RH1). Symmetry with
        # ``active_database``: a facade-only pass-through, no logic.
        if name in ("dialect", "active_backend"):
            from memory.backends import active_backend, dialect

            return {"dialect": dialect, "active_backend": active_backend}[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        targets = _ROUTED.get(name)
        if targets is not None:
            for _mod in targets:
                setattr(_mod, name, value)
            # Do not also stash a copy in the facade __dict__: that would shadow
            # the read-through in __getattr__ with a value that goes stale when
            # the owning module later rebinds it (e.g. register_user_interaction).
            self.__dict__.pop(name, None)
            return
        super().__setattr__(name, value)


# Drop the facade's own copies of routed names so __getattr__ always reflects the
# live owning-module value (these were bound by the `from ... import` lines above).
for _n in _ROUTED:
    _sys.modules[__name__].__dict__.pop(_n, None)

_sys.modules[__name__].__class__ = _Facade
