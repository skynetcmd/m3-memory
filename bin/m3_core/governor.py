import asyncio
import logging
import os
import time
from typing import Any

from m3_core.paths import get_m3_config_root
from m3_core.runtime import M3_CORE_RS_DISABLE

logger = logging.getLogger("M3_SDK")

_LAST_USER_INTERACTION = 0.0

# ── Governor thresholds (live-reloadable) ─────────────────────────────────────
# INITIAL_LIMIT = load% at/above which background work THROTTLES; LIMIT_THRESHOLD
# = load% at/above which it HALTS. Resolved at runtime each cycle via
# _governor_thresholds() with precedence: config file > env var > default. The
# config file (<config_root>/.governor_config.json) is the cross-platform,
# RESTART-FREE knob: none of the headless launchers (Windows task / macOS
# launchd / Linux systemd) reliably inherit shell env, but they all resolve the
# config root the same way, and the file is re-read every _GOV_CFG_TTL seconds so
# an edit takes effect in the running governor within seconds — no restart.
#
# Defaults below are the import-time fallback (kept for any caller reading the
# module constants directly); the live path is _governor_thresholds().
_GOV_DEFAULT_INITIAL = 85
_GOV_DEFAULT_LIMIT = 95
_GOV_CFG_TTL = float(os.environ.get("M3_GOVERNOR_CFG_TTL", "5.0"))
# mtime tracks the last-parsed file modification time so we only re-read+parse
# when the file actually changed; `ts` throttles how often we even stat it.
_gov_cfg_cache: dict[str, Any] = {"ts": 0.0, "mtime": None, "initial": None, "limit": None}


def _governor_config_path() -> str:
    return os.path.join(get_m3_config_root(), ".governor_config.json")


def _governor_thresholds(now: float | None = None) -> tuple[int, int]:
    """Return (initial_threshold, limit_threshold), clamped & sanity-checked.
    Precedence per value: config file > env var > default.

    The file is only re-read+parsed when its mtime changes — between the stat
    checks (throttled to _GOV_CFG_TTL so we don't stat on every single call) an
    unchanged file costs one os.stat, not an open+JSON-parse. Edits apply within
    one stat interval, no restart. Never raises."""
    now = now if now is not None else time.time()
    if now - _gov_cfg_cache["ts"] >= _GOV_CFG_TTL:
        _gov_cfg_cache["ts"] = now
        try:
            path = _governor_config_path()
            mtime = os.stat(path).st_mtime  # raises if absent
        except OSError:
            mtime = None  # file gone / unreadable -> fall back to env+default
        if mtime != _gov_cfg_cache["mtime"]:
            _gov_cfg_cache["mtime"] = mtime
            cfg: dict = {}
            if mtime is not None:
                try:
                    import json as _json
                    with open(_governor_config_path(), encoding="utf-8") as f:
                        cfg = _json.load(f) or {}
                except Exception as e:
                    # §3 never silent: a malformed config would otherwise revert
                    # the governor to env/defaults invisibly — a bad threshold
                    # edit must be loud so the user knows their tuning isn't live.
                    logger.warning(
                        "Governor config %s is unreadable/malformed (%s) — "
                        "falling back to env vars + defaults until fixed.",
                        _governor_config_path(), e)
                    cfg = {}
            _gov_cfg_cache["initial"] = cfg.get("initial_threshold")
            _gov_cfg_cache["limit"] = cfg.get("limit_threshold")

    def _resolve(cfg_val, env_name, default):
        if cfg_val is not None:
            try:
                return int(cfg_val)
            except (TypeError, ValueError):
                pass
        try:
            return int(os.environ.get(env_name, default))
        except (TypeError, ValueError):
            return default

    initial = min(99, max(10, _resolve(
        _gov_cfg_cache["initial"], "M3_GOVERNOR_INITIAL_THRESHOLD", _GOV_DEFAULT_INITIAL)))
    limit = min(100, max(20, _resolve(
        _gov_cfg_cache["limit"], "M3_GOVERNOR_LIMIT_THRESHOLD", _GOV_DEFAULT_LIMIT)))
    # Enforce initial < limit.
    if initial >= limit and limit != 100:
        initial = limit - 5
    return initial, limit


_GOV_CFG_TEMPLATE_COMMENT = (
    "Adaptive Background Workload Governor thresholds. Read live by m3_sdk "
    "(re-read on mtime change, no restart). load = max(cpu%, ram%, gpu%). "
    "Background work THROTTLES at >= initial_threshold and HALTS at >= "
    "limit_threshold, keeping headroom for interactive use. Edit and save; the "
    "running cognitive loop / MCP server picks it up within seconds. GPU load "
    "only gates local-LLM work; cloud/SQL passes ignore it."
)


def ensure_governor_config() -> str:
    """Create <config_root>/.governor_config.json with the CURRENT effective
    thresholds if it does not already exist. Idempotent and race-safe (atomic
    create; never overwrites an existing file). Returns the path. Never raises —
    a write failure just leaves the system on env+defaults. Call at setup and
    once at daemon startup; NOT from the per-cycle read path."""
    path = _governor_config_path()
    if os.path.exists(path):
        return path
    try:
        initial, limit = _governor_thresholds()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        import json as _json
        payload = {
            "_comment": _GOV_CFG_TEMPLATE_COMMENT,
            "initial_threshold": initial,
            "limit_threshold": limit,
        }
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(payload, f, indent=2)
        # os.replace is atomic; if another process raced us, last-writer-wins on
        # identical default content is harmless.
        os.replace(tmp, path)
        logger.info("Seeded governor config at %s (initial=%d limit=%d)", path, initial, limit)
    except Exception as e:
        logger.debug("Could not seed governor config (%s); using env+defaults", e)
    return path


# Import-time snapshot (back-compat for any reader of the module constants).
# Env+default ONLY — the config-file path requires get_m3_config_root(), defined
# later in this module, so reading it here would NameError at import. The live
# governor path (_governor_thresholds, called per-cycle) picks up the file.
def _import_time_threshold(env_name: str, default: int, lo: int, hi: int) -> int:
    try:
        return min(hi, max(lo, int(os.environ.get(env_name, default))))
    except (TypeError, ValueError):
        return default


INITIAL_LIMIT = _import_time_threshold("M3_GOVERNOR_INITIAL_THRESHOLD", _GOV_DEFAULT_INITIAL, 10, 99)
LIMIT_THRESHOLD = _import_time_threshold("M3_GOVERNOR_LIMIT_THRESHOLD", _GOV_DEFAULT_LIMIT, 20, 100)
if INITIAL_LIMIT >= LIMIT_THRESHOLD and LIMIT_THRESHOLD != 100:
    INITIAL_LIMIT = LIMIT_THRESHOLD - 5

def register_user_interaction():
    global _LAST_USER_INTERACTION
    _LAST_USER_INTERACTION = time.time()

def get_governor_pacing(telemetry: dict) -> dict:
    """Return pacing delay configurations for background and interactive pipelines."""
    load = max(telemetry.get("cpu_total", 0.0), telemetry.get("ram_total", 0.0), telemetry.get("gpu_total", 0.0))
    elapsed = time.time() - _LAST_USER_INTERACTION
    # Live thresholds (config file > env > default), re-read each cycle so a
    # .governor_config.json edit takes effect without restarting the loop.
    initial_limit, limit_threshold = _governor_thresholds()

    # Native fast-path: the Rust governor is the source-of-truth for this ladder
    # (crate m3-governor, exposed as m3_core_rs.Governor). It returns a dict
    # key-for-key identical to the Python fallback below. Mirrors the
    # try-native/except-fallback convention used by migration_lock(). A missing
    # or older wheel (no Governor attr) falls through to pure Python.
    if not M3_CORE_RS_DISABLE:
        try:
            import m3_core_rs
            if hasattr(m3_core_rs, "Governor"):
                return m3_core_rs.Governor(initial_limit, limit_threshold).decide(load, elapsed)
        except Exception:
            pass  # fall through to the pure-Python ladder

    # 1. Critical Mode (Overall load >= limit_threshold)
    if limit_threshold != 100 and load >= limit_threshold:
        return {"background": "HALTED", "interactive_delay": 30.0} # 30s-60s delay

    # 2. Throttled Mode (Overall load >= initial_limit but < limit_threshold)
    if load >= initial_limit:
        return {"background": "THROTTLED", "background_delay": 10.0, "interactive_delay": 0.0} # 5s-10s delay

    # 3. Normal Mode
    if elapsed < 30.0:
        return {"background": "HALTED", "interactive_delay": 0.0}
    elif elapsed < 60.0:
        return {"background": "TAPERED", "background_delay": 5.0, "interactive_delay": 0.0}
    return {"background": "CONTINUOUS", "background_delay": 0.1, "interactive_delay": 0.0}

async def pre_execute_interactive_check():
    from m3_core.context import M3Context
    register_user_interaction()

    ctx = M3Context.for_db()
    telemetry = ctx.get_system_telemetry()
    pacing = get_governor_pacing(telemetry)

    delay = pacing.get("interactive_delay", 0.0)
    if delay > 0.0:
        logger.warning(
            f"Host load critical. Throttling interactive task by {delay}s "
            "to prevent system freeze."
        )
        await asyncio.sleep(delay)
