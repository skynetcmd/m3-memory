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

# Absolute free-RAM ladder (GiB). RAM is the one resource where a PERCENT gate is
# wrong: 90% of a 32 GiB box (~3 GiB free) is fine, 90% of an 8 GiB box (~0.8 GiB
# free) is critical — what determines whether the next allocation OOMs is absolute
# free bytes, not the percentage. So RAM is gated here on ram_available_gb, NOT
# folded into the cpu/gpu percent scalar. Overridable via env.
#
# Only the two states existing consumers act on are used (HALTED / THROTTLED) — a
# third gradation would be silently treated as full-speed by every caller (they
# test `== "HALTED"` / `== "THROTTLED"` and fall through otherwise), so it would
# be MORE dangerous than THROTTLED, not less. HALT below ~1 GiB free, THROTTLE
# below ~4 GiB.
# When the user has been idle at least this long, the host is effectively the
# agent's to use, so background work may run down to a TIGHTER free-RAM buffer
# before throttling (idle_throttle_gb) than while the user is active
# (throttle_gb). The HALT floor is idle-independent — a genuinely starved box
# must always pause.
_RAM_IDLE_RELAX_SECONDS = 1800.0  # 30 min


def _ram_free_thresholds() -> "tuple[float, float, float]":
    """(halt_gb, throttle_gb, idle_throttle_gb): free RAM at/below which we HALT /
    THROTTLE-while-active / THROTTLE-while-idle. Default 1 / 4 / 2 GiB. Env
    overrides for smaller/larger hosts."""
    def _f(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, "") or default)
        except ValueError:
            return default
    return (_f("M3_GOVERNOR_RAM_HALT_GB", 1.0),
            _f("M3_GOVERNOR_RAM_THROTTLE_GB", 4.0),
            _f("M3_GOVERNOR_RAM_THROTTLE_IDLE_GB", 2.0))


# State severity for worst-wins combination with the cpu/gpu verdict.
_STATE_RANK = {"CONTINUOUS": 0, "TAPERED": 1, "THROTTLED": 2, "HALTED": 3}


def _ram_free_state(telemetry: dict, elapsed: float) -> "str | None":
    """Absolute-free-RAM verdict, or None when free RAM is unknown/plentiful.

    ram_available_gb == 0.0 means the probe couldn't read it (psutil absent /
    older native wheel that doesn't report it) — treat as unknown and DON'T gate
    on it (fail-open; the cpu/gpu path still protects the host).

    ``elapsed`` is seconds since the last user interaction: when the user has been
    idle past _RAM_IDLE_RELAX_SECONDS, use the tighter idle throttle buffer so the
    agent can use more of an otherwise-unused host."""
    free = float(telemetry.get("ram_available_gb", 0.0))
    if free <= 0.0:
        return None  # unknown — do not gate on RAM
    halt_gb, throttle_gb, idle_throttle_gb = _ram_free_thresholds()
    if free < halt_gb:
        return "HALTED"  # starved regardless of idle
    active_throttle = idle_throttle_gb if elapsed >= _RAM_IDLE_RELAX_SECONDS else throttle_gb
    if free < active_throttle:
        return "THROTTLED"
    return None  # plenty of free RAM for the current activity level


# Delay config per RAM-driven state, matching the shape the cpu/gpu ladder returns.
_STATE_DELAYS = {
    "HALTED": {"background": "HALTED", "interactive_delay": 30.0},
    "THROTTLED": {"background": "THROTTLED", "background_delay": 10.0, "interactive_delay": 0.0},
}


def get_governor_pacing(telemetry: dict) -> dict:
    """Return pacing delay configurations for background and interactive pipelines."""
    # RAM is deliberately EXCLUDED from this scalar — it is gated on ABSOLUTE free
    # RAM (see _ram_free_state), not percent. Folding ram_total (a percent) in here
    # is what made a large-but-full box (e.g. 92% of 32 GiB, ~2.5 GiB free) HALT
    # needlessly. cpu/gpu remain correctly percent-based.
    load = max(telemetry.get("cpu_total", 0.0), telemetry.get("gpu_total", 0.0))
    elapsed = time.time() - _LAST_USER_INTERACTION
    # Live thresholds (config file > env > default), re-read each cycle so a
    # .governor_config.json edit takes effect without restarting the loop.
    initial_limit, limit_threshold = _governor_thresholds()

    # Native fast-path: the Rust governor is the source-of-truth for the cpu/gpu
    # ladder (crate m3-governor, m3_core_rs.Governor). It returns a dict
    # key-for-key identical to the Python fallback below. Because `load` no longer
    # carries RAM, the native path cannot gate on RAM percent — the absolute-RAM
    # escalation below is layered on top in Python, so the RAM fix holds whether
    # the cpu/gpu verdict came from native or Python. (Until the crate itself
    # understands free-GiB RAM — targeted for m3-core-rs > 3.7.17 — this Python
    # layer is the RAM authority.)
    pacing = None
    if not M3_CORE_RS_DISABLE:
        try:
            import m3_core_rs
            if hasattr(m3_core_rs, "Governor"):
                pacing = m3_core_rs.Governor(initial_limit, limit_threshold).decide(load, elapsed)
        except Exception:
            pacing = None  # fall through to the pure-Python ladder

    if pacing is None:
        pacing = _cpu_gpu_pacing(load, elapsed, initial_limit, limit_threshold)

    # Layer the absolute-free-RAM verdict on top: escalate to the WORSE of the
    # cpu/gpu state and the RAM state (a starved host must throttle/halt even when
    # cpu/gpu are idle).
    ram_state = _ram_free_state(telemetry, elapsed)
    if ram_state is not None:
        cur = pacing.get("background", "CONTINUOUS")
        if _STATE_RANK.get(ram_state, 0) > _STATE_RANK.get(cur, 0):
            return dict(_STATE_DELAYS[ram_state])
    return pacing


def _cpu_gpu_pacing(load: float, elapsed: float,
                    initial_limit: float, limit_threshold: float) -> dict:
    """Pure-Python cpu/gpu load ladder (the native Governor mirrors this)."""
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
