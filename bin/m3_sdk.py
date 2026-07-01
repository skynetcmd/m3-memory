import argparse
import asyncio
import atexit
import contextvars
import logging
import os
import queue
import random
import sqlite3
import sys
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from typing import Any, Optional

import httpx
from sqlite_pragmas import apply_pragmas, profile_for_db

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs): pass

M3_CORE_RS_DISABLE = os.environ.get("M3_CORE_RS_DISABLE", "0") == "1"

try:
    if M3_CORE_RS_DISABLE:
        raise ImportError
    from m3_core_rs import format_log
except ImportError:
    def format_log(event: str, *args, **kwargs) -> str:
        parts = [event]
        for a in args:
            if a is None or a == "":
                continue
            parts.append(str(a))
        for k, v in kwargs.items():
            if v is None:
                continue
            parts.append(f"{k}={v}")
        return " | ".join(parts)

logger = logging.getLogger("M3_SDK")

import hashlib

_LAST_USER_INTERACTION = 0.0

# ── GPU utilization probe ─────────────────────────────────────────────────────
# The Python telemetry path historically hardcoded gpu_total=0.0, so the governor
# was blind to GPU load — a local LLM (LM Studio / Ollama / vLLM) or the embed
# server could pin the GPU at ~100% and the loop would keep dispatching work,
# saturating it. Probe real GPU utilization via `nvidia-smi` (no extra dep) and
# feed it into telemetry so the governor's max(cpu,ram,gpu,llm) load reacts.
#
# Cached with a short TTL: spawning nvidia-smi costs ~30-80ms, and the loop reads
# telemetry often. Disable with M3_GPU_PROBE_DISABLE=1 (e.g. CPU-only hosts).
_GPU_PROBE_DISABLE = os.environ.get("M3_GPU_PROBE_DISABLE", "").lower() in ("1", "true", "yes")
_GPU_PROBE_TTL = float(os.environ.get("M3_GPU_PROBE_TTL", "2.0"))
# `backend` pins the first probe that returned a reading so we don't re-try dead
# ones every cycle; `misses` trips the whole probe off after every backend has
# failed enough times (circuit breaker — §6: don't keep paying for a dead call).
_gpu_probe_cache: dict[str, Any] = {"ts": 0.0, "util": 0.0, "backend": None, "misses": 0}
_GPU_PROBE_MAX_MISSES = 3


def _no_window() -> dict:
    """subprocess kwargs that suppress a console window on Windows (no-op off
    Windows). These GPU/telemetry probes run on the per-cycle governor path;
    without CREATE_NO_WINDOW, nvidia-smi / powershell / tasklist each FLASH a
    console window and steal focus on every poll. Shared _task_runtime helper is
    preferred; this local fallback avoids an import cycle in the hot path."""
    import subprocess as _sp
    # getattr, not _sp.CREATE_NO_WINDOW: the attribute only exists on Windows, so
    # a direct reference fails mypy on the Linux CI runner.
    flags = getattr(_sp, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    return {"creationflags": flags} if flags else {}


def _gpu_util_nvidia() -> float | None:
    """CUDA GPUs (any OS) via nvidia-smi. None = backend unavailable."""
    import subprocess
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2.0, **_no_window(),
        )
    except (FileNotFoundError, OSError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    utils = [float(x) for x in out.stdout.split() if x.strip().replace(".", "", 1).isdigit()]
    return max(utils) if utils else None


def _gpu_util_windows_counter() -> float | None:
    """Windows AMD/Intel/NVIDIA via the '\\GPU Engine(*)\\Utilization Percentage'
    perf counter (covers Vulkan/D3D on any vendor). Sums per-engine usage,
    capped at 100. None = unavailable / not Windows."""
    if os.name != "nt":
        return None
    import subprocess
    try:
        # PowerShell Get-Counter; pick the busiest 3D/compute engine sample.
        ps = (
            "$s=(Get-Counter '\\GPU Engine(*)\\Utilization Percentage' "
            "-ErrorAction Stop).CounterSamples; "
            "[math]::Round((($s | Measure-Object -Property CookedValue -Maximum).Maximum),1)"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=4.0, **_no_window(),
        )
    except (FileNotFoundError, OSError):
        return None
    s = out.stdout.strip()
    if out.returncode != 0 or not s:
        return None
    try:
        return min(100.0, float(s))
    except ValueError:
        return None


def _gpu_util_macos_ioreg() -> float | None:
    """macOS (Apple Silicon Metal / AMD eGPU) via ioreg IOAccelerator
    'Device Utilization %' — no sudo. None = unavailable / not macOS."""
    if sys.platform != "darwin":
        return None
    import re as _re
    import subprocess
    try:
        out = subprocess.run(
            ["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"],
            capture_output=True, text=True, timeout=3.0,
        )
    except (FileNotFoundError, OSError):
        return None
    if out.returncode != 0 or not out.stdout:
        return None
    # Key is reported as "Device Utilization %"=NN (integer percent).
    vals = [float(m) for m in _re.findall(r'"Device Utilization %"\s*=\s*(\d+)', out.stdout)]
    return max(vals) if vals else None


def _gpu_util_linux_sysfs() -> float | None:
    """Linux AMD (amdgpu, incl. Vulkan/ROCm) via
    /sys/class/drm/card*/device/gpu_busy_percent. None = unavailable / no file."""
    if not sys.platform.startswith("linux"):
        return None
    import glob
    vals = []
    for path in glob.glob("/sys/class/drm/card*/device/gpu_busy_percent"):
        try:
            with open(path) as f:
                vals.append(float(f.read().strip()))
        except (OSError, ValueError):
            continue
    return max(vals) if vals else None


# Ordered by likelihood + cheapness. The first that returns non-None wins and is
# pinned in the cache. NVIDIA first (most common LLM GPU); then the OS-native
# counters that cover Metal / AMD / Intel / Vulkan; CPU-only hosts match none.
_GPU_PROBES = (
    ("nvidia", _gpu_util_nvidia),
    ("windows", _gpu_util_windows_counter),
    ("macos", _gpu_util_macos_ioreg),
    ("linux-sysfs", _gpu_util_linux_sysfs),
)


def probe_gpu_util(now: float | None = None) -> float:
    """Best-effort GPU utilization percent (0-100) across CUDA / Apple Metal /
    AMD+Intel Vulkan / CPU-only, on all three OSes. Returns 0.0 when no GPU
    backend is available (CPU-only) or the probe is disabled. TTL-cached; pins
    the working backend; trips off after repeated total misses. Never raises."""
    if _GPU_PROBE_DISABLE or _gpu_probe_cache["misses"] >= _GPU_PROBE_MAX_MISSES:
        return 0.0
    now = now if now is not None else time.time()
    if now - _gpu_probe_cache["ts"] < _GPU_PROBE_TTL:
        return _gpu_probe_cache["util"]
    _gpu_probe_cache["ts"] = now

    # If a backend already worked, try only it (fast path); else scan all.
    pinned = _gpu_probe_cache["backend"]
    probes = ([p for p in _GPU_PROBES if p[0] == pinned] or list(_GPU_PROBES)) if pinned else list(_GPU_PROBES)
    for name, fn in probes:
        try:
            val = fn()
        except Exception:
            val = None  # transient (timeout/parse) — try the next backend
        if val is not None:
            _gpu_probe_cache.update(util=max(0.0, min(100.0, val)), backend=name, misses=0)
            return _gpu_probe_cache["util"]

    # Nothing answered this round. If a backend was pinned but just failed, it may
    # be a transient hiccup — keep the last value and count the miss; once we hit
    # the cap (or never found any GPU), settle to 0.0 and stop probing.
    _gpu_probe_cache["misses"] += 1
    if _gpu_probe_cache["backend"] is None or _gpu_probe_cache["misses"] >= _GPU_PROBE_MAX_MISSES:
        _gpu_probe_cache["util"] = 0.0
    return _gpu_probe_cache["util"]

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

@contextmanager
def migration_lock():
    """Acquires an exclusive atomic file lock for safe startup migrations.

    If the lock is held by another process, it block-waits (with a timeout of 120s)
    until the lock is released.
    """
    lock_path = os.path.join(get_m3_config_root(), ".migration.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)

    # Try native Rust advisory lock first
    if not M3_CORE_RS_DISABLE:
        try:
            import m3_core_rs
            if hasattr(m3_core_rs, "NativeMigrationLock"):
                lock = m3_core_rs.NativeMigrationLock(lock_path)
                acquired = lock.acquire(120)
                if not acquired:
                    raise RuntimeError(
                        f"Could not acquire native migration lock at {lock_path} within 120 seconds."
                    )
                try:
                    yield
                finally:
                    lock.release()
                return
        except Exception as e:
            if isinstance(e, RuntimeError) and "lock" in str(e):
                raise
            # Fall back to Python busy-wait lock on other errors

    fd = None
    start_time = time.time()
    acquired = False

    while time.time() - start_time < 120.0:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            # Stamp ownership so a future waiter can tell whether the holder is
            # still alive (PID liveness) or this is an orphaned lock to reclaim.
            try:
                os.write(fd, _lock_owner_stamp().encode("utf-8"))
            except OSError:
                pass  # stamping is best-effort; the lock itself is what matters
            acquired = True
            break
        except FileExistsError:
            # The lock exists. Before sleeping, check whether it's STALE — a
            # process that died holding it (crash / kill -9 / OOM) leaves the
            # file behind, which would otherwise wedge every migration for the
            # full 120s and then hard-error (the 2026-06-27 incident). Reclaim
            # it only when we can prove the owner is gone.
            if _reclaim_stale_lock(lock_path):
                continue  # reclaimed — retry os.open immediately, no sleep
            time.sleep(0.5)

    if not acquired:
        raise RuntimeError(
            f"Could not acquire migration lock at {lock_path} within 120 seconds. "
            "Another migration process may be hung. If you are sure no other process is migrating, "
            f"delete the lock file manually: {lock_path}"
        )

    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
            try:
                os.unlink(lock_path)
            except Exception:
                pass


# Hard ceiling: no legitimate startup migration runs longer than this. A lock
# older than this from an UNKNOWN/cross-host owner (whose PID we can't probe) is
# treated as stale. Generous so we never reclaim a genuinely-slow migration.
_MIGRATION_LOCK_MAX_AGE_S = 600.0


def _lock_owner_stamp() -> str:
    """Owner metadata written into the lock file: 'pid host epoch'."""
    import socket
    return f"{os.getpid()} {socket.gethostname()} {int(time.time())}"


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID exists. Conservative: on any uncertainty
    we return True (assume alive) so we never steal a live lock."""
    if pid <= 0:
        return False
    if os.name == "nt":
        # No os.kill(pid, 0) semantics on Windows; query the task list.
        import subprocess
        try:
            out = subprocess.run(
                ["tasklist", "/fi", f"PID eq {pid}", "/nh"],
                capture_output=True, text=True, timeout=5, **_no_window(),
            )
            return str(pid) in (out.stdout or "")
        except (OSError, subprocess.SubprocessError):
            return True  # can't tell -> assume alive (safe)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user -> alive
    except OSError:
        return True  # unknown -> assume alive


def _reclaim_stale_lock(lock_path: str) -> bool:
    """Remove the lock file IFF its owner is provably gone. Returns True if a
    stale lock was reclaimed (caller should retry acquisition immediately).

    Decision rules (all fail SAFE — when unsure, leave the lock alone):
      - Same host + owner PID not alive  -> stale, reclaim.
      - Cross host (can't probe PID) + file older than the max-age ceiling
        -> stale, reclaim.
      - Unparseable/empty stamp + file older than the ceiling -> stale.
      - Otherwise -> not stale (return False; caller keeps waiting).
    """
    import socket
    try:
        raw = ""
        with open(lock_path, encoding="utf-8") as f:
            raw = f.read().strip()
        try:
            mtime = os.path.getmtime(lock_path)
        except OSError:
            mtime = 0.0
    except FileNotFoundError:
        return True  # vanished out from under us — effectively reclaimable
    except OSError:
        return False  # can't read -> don't touch it

    age = max(0.0, time.time() - mtime)
    parts = raw.split()
    pid: int = 0
    host = ""
    if len(parts) >= 2:
        try:
            pid = int(parts[0])
        except ValueError:
            pid = 0
        host = parts[1]

    this_host = socket.gethostname()
    stale = False
    if pid and host == this_host:
        # Same machine: authoritative liveness check.
        stale = not _pid_alive(pid)
    else:
        # Cross-host or unparseable: we cannot probe the PID, so fall back to a
        # generous age ceiling. Only reclaim something clearly abandoned.
        stale = age > _MIGRATION_LOCK_MAX_AGE_S

    if not stale:
        return False
    try:
        os.unlink(lock_path)
        logger.warning(
            "Reclaimed stale migration lock %s (owner pid=%s host=%s age=%.0fs)",
            lock_path, pid or "?", host or "?", age,
        )
        return True
    except FileNotFoundError:
        return True  # someone else reclaimed it first — fine, retry
    except OSError:
        return False




def ensure_utf8() -> None:
    """Guarantee the current process runs in Python UTF-8 mode.

    On Windows both stdio AND open() default to the legacy cp1252 code page, so
    any non-cp1252 character (em-dashes, arrows, box-drawing, emoji) crashes with
    UnicodeEncodeError on print or UnicodeDecodeError on a no-encoding open().
    True UTF-8 mode (PEP 540) fixes both, but the interpreter reads it only at
    startup — so we set PYTHONUTF8 and re-exec once with -X utf8.

    Shared canonical implementation: called from every m3 entry process that
    isn't guaranteed to inherit UTF-8 mode — the m3 CLI (m3_memory.cli) and the
    standalone MCP→OpenAI proxy (bin/mcp_proxy.py, the OpenClaw path, launched
    directly as `python bin/mcp_proxy.py` so it never flows through the CLI).

    Safety: no-op if already in UTF-8 mode; an env sentinel bounds the re-exec to
    exactly once so it cannot loop; sys.orig_argv reconstructs the launch
    faithfully (so -m / file-path forms survive).

    KNOWN LIMITATION: inline `python -c "<code>"` launches can mangle on re-exec
    because the OS re-quotes the program string; not a supported m3 entry path.
    Set PYTHONUTF8=1 in the env to bypass (then this short-circuits).
    """
    if sys.flags.utf8_mode:
        return
    if os.environ.get("_M3_UTF8_REEXEC") == "1":
        return
    os.environ["PYTHONUTF8"] = "1"
    os.environ["_M3_UTF8_REEXEC"] = "1"
    orig = list(getattr(sys, "orig_argv", [sys.executable, *sys.argv])) or [
        sys.executable, *sys.argv]
    try:
        os.execv(sys.executable, [orig[0], "-X", "utf8", *orig[1:]])
    except OSError:
        # Re-exec failed (exotic launcher / permissions). Caller's stdio
        # reconfigure (if any) still handles the common print path.
        pass


# Single source of truth for the local LLM base URL + read timeout. Bridges
# imported this from here in bench-wip; main had been redefining it in each
# bridge. Still overridable via env so dev machines with LM Studio on a
# different port (or a remote Ollama) work without code edits.
LM_STUDIO_BASE = os.environ.get("LM_STUDIO_BASE", "http://localhost:1234/v1")
LM_READ_TIMEOUT = float(os.environ.get("LM_READ_TIMEOUT", "4800.0"))

# ── Per-path context registry ─────────────────────────────────────────────────
# Previously a module-global _SQLITE_POOL was used, with singleton M3Context
# silently reusing the first-initialized pool. Multi-DB support requires a
# pool per resolved DB path, so each M3Context instance owns its own pool and
# instances are cached per absolute path in _CONTEXTS.
#
# LRU-bounded to prevent unbounded growth on long-running MCP servers that see
# many distinct per-call `database` values. Hot paths (default DB, any DB the
# process accesses repeatedly) get refreshed to most-recently-used on every
# lookup, so the cap only ever evicts cold paths. Override via M3_CONTEXT_CACHE_SIZE.
_CONTEXT_CACHE_SIZE = max(2, int(os.environ.get("M3_CONTEXT_CACHE_SIZE", "16")))
_CONTEXTS: "OrderedDict[str, M3Context]" = OrderedDict()
_CONTEXTS_LOCK = threading.Lock()


def _close_context_pool(ctx: "M3Context") -> None:
    """Close every connection in ctx's pool. Safe to call once; idempotent."""
    pool = ctx._pool
    if pool is None:
        return
    ctx._pool = None
    while not pool.empty():
        try:
            conn = pool.get_nowait()
            conn.close()
        except queue.Empty:
            break
        except Exception as e:
            logger.error(f"Error closing SQLite connection: {e}")

_CIRCUITS = {}
_CB_THRESHOLD = 3
_CB_COOLDOWN = 60
_HTTP_CLIENT: Optional[httpx.AsyncClient] = None
_HTTP_CLIENT_LOOP_ID: Optional[int] = None
_HTTP_CLIENT_LOCK = threading.Lock()

# ── Active-database ContextVar ────────────────────────────────────────────────
# Consulted by callers that want "whatever DB the surrounding request/CLI
# specified, else the default". The MCP tool dispatcher sets this before each
# tool call; CLI scripts set it once at startup.
_active_db: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "m3_active_db", default=None
)


def resolve_venv_python() -> str:
    """Returns the path to the project venv Python executable, cross-platform."""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.platform == "win32":
        return os.path.join(base, ".venv", "Scripts", "python.exe")
    return os.path.join(base, ".venv", "bin", "python")


def get_m3_root() -> str:
    """Returns the M3 root directory for user state (config, backups, etc.).
    Honors M3_MEMORY_ROOT env var, defaults to ~/.m3-memory.
    """
    root = os.getenv("M3_MEMORY_ROOT")
    if root:
        return os.path.abspath(os.path.expanduser(root))
    return os.path.join(os.path.expanduser("~"), ".m3-memory")


def get_m3_config_root() -> str:
    """Returns the M3 configuration directory.
    Precedence: M3_CONFIG_ROOT > M3_MEMORY_ROOT/config > ~/.m3/config
    """
    root = os.getenv("M3_CONFIG_ROOT")
    if root:
        return os.path.abspath(os.path.expanduser(root))
    m3_mem_root = os.getenv("M3_MEMORY_ROOT")
    if m3_mem_root:
        return os.path.join(os.path.abspath(os.path.expanduser(m3_mem_root)), "config")
    return os.path.join(os.path.expanduser("~"), ".m3", "config")


def get_m3_engine_root() -> str:
    """Returns the M3 database engine directory.
    Precedence: M3_ENGINE_ROOT > M3_MEMORY_ROOT/engine > ~/.m3/engine
    """
    root = os.getenv("M3_ENGINE_ROOT")
    if root:
        return os.path.abspath(os.path.expanduser(root))
    m3_mem_root = os.getenv("M3_MEMORY_ROOT")
    if m3_mem_root:
        return os.path.join(os.path.abspath(os.path.expanduser(m3_mem_root)), "engine")
    return os.path.join(os.path.expanduser("~"), ".m3", "engine")


def _db_is_populated(path: str) -> bool:
    """True iff `path` is a SQLite file that actually carries the memory schema.

    A bare-existence check is not enough: a connection attempt against a not-yet-
    migrated engine root auto-creates a 0-table `agent_memory.db` stub, and that
    stub would otherwise shadow a populated legacy DB (the M3_MEMORY_ROOT drift —
    a fresh engine/ stub silently winning over memory/agent_memory.db with the
    real data). Returns False for a missing file, an empty stub, or any open/read
    error (treat unreadable as "not usable" so the caller keeps searching).
    """
    if not os.path.exists(path):
        return False
    try:
        conn = sqlite3.connect(path, timeout=2)
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_items' LIMIT 1"
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — unreadable/locked DB is not a usable default
        return False


def _default_db_path() -> str:
    # Precedence: explicit M3_ENGINE_ROOT (honored as-is) > a *populated* derived
    # engine root > a populated ~/.m3/engine default > populated sibling memory/
    # (dev clone) > the derived engine path as a last resort (fresh install).
    #
    # The key fix over the naive "any env var set -> engine path" rule: when only
    # M3_MEMORY_ROOT is set, the engine path is DERIVED, not chosen. If that
    # derived DB is missing or an empty stub, we must not let it shadow a
    # populated legacy memory/ DB — see _db_is_populated.
    if os.getenv("M3_ENGINE_ROOT"):
        # Explicit engine root is a deliberate operator choice; honor it verbatim
        # even if empty (a fresh deployment legitimately starts empty here).
        return os.path.join(get_m3_engine_root(), "agent_memory.db")

    engine_db = os.path.join(get_m3_engine_root(), "agent_memory.db")
    if os.getenv("M3_MEMORY_ROOT"):
        if _db_is_populated(engine_db):
            return engine_db
        # Derived engine DB is missing/empty. Prefer a populated legacy memory/
        # DB under the same root before falling back to the empty engine path.
        legacy_under_root = os.path.join(
            os.path.abspath(os.path.expanduser(os.getenv("M3_MEMORY_ROOT"))),
            "memory", "agent_memory.db",
        )
        if _db_is_populated(legacy_under_root):
            logger.warning(
                "M3_MEMORY_ROOT engine DB (%s) is missing or unmigrated; using the "
                "populated legacy store at %s. Run bin/homecoming.py to migrate, or "
                "set M3_ENGINE_ROOT explicitly to silence this.",
                engine_db, legacy_under_root,
            )
            return legacy_under_root
        return engine_db

    # No env override: prefer a populated ~/.m3/engine default, else a populated
    # sibling memory/ (developer clone), else the engine default for a fresh start.
    m3_engine_default = os.path.join(os.path.expanduser("~"), ".m3", "engine", "agent_memory.db")
    if _db_is_populated(m3_engine_default):
        return m3_engine_default

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    legacy_path = os.path.join(base, "memory", "agent_memory.db")
    if _db_is_populated(legacy_path):
        return legacy_path

    return os.path.join(get_m3_engine_root(), "agent_memory.db")


def resolve_db_path(explicit: Optional[str] = None) -> str:
    """Resolve an absolute SQLite DB path.

    Order: explicit arg > M3_DATABASE env > active_database ContextVar > default
    (memory/agent_memory.db). Returns an absolute path so pool-cache keys are
    consistent regardless of caller CWD.
    """
    candidate = explicit or os.environ.get("M3_DATABASE") or _active_db.get() or _default_db_path()
    return os.path.abspath(candidate)


@contextmanager
def active_database(path: Optional[str]):
    """Set the active DB path for the duration of a block (ContextVar-scoped).

    Propagates across ``await`` within the same task but does not leak across
    threads — each executor thread gets its own copy unless the caller sets it
    explicitly. Pass ``None`` or "" to defer to env/default resolution.
    """
    resolved = resolve_db_path(path) if path else None
    token = _active_db.set(resolved)
    try:
        yield resolved
    finally:
        _active_db.reset(token)


def add_database_arg(parser: argparse.ArgumentParser) -> None:
    """Attach a standard --database flag to a CLI argparse parser.

    Precedence honored by resolve_db_path(): --database > M3_DATABASE env >
    default (memory/agent_memory.db). Scripts should activate the returned
    path via active_database() or by writing to os.environ['M3_DATABASE']
    before any DB-touching code runs.
    """
    parser.add_argument(
        "--database",
        default=None,
        metavar="PATH",
        help=(
            "SQLite database path. "
            "Env: M3_DATABASE. Default: memory/agent_memory.db."
        ),
    )


class M3Context:
    def __init__(self, db_path: Optional[str] = None):
        self.m3_config_root = get_m3_config_root()
        self.m3_engine_root = get_m3_engine_root()
        self.m3_memory_root = get_m3_root()  # Keep for legacy compatibility

        # Load dotenv from config root first, fallback to memory root
        dotenv_path = os.path.join(self.m3_config_root, ".env")
        if not os.path.exists(dotenv_path):
            dotenv_path = os.path.join(self.m3_memory_root, ".env")

        if os.path.exists(dotenv_path):
            load_dotenv(dotenv_path)

        # Preserve constructor contract: no-arg M3Context() resolves against
        # env + default. Callers passing an explicit path bypass the resolver.
        self.db_path = os.path.abspath(db_path or resolve_db_path(None))
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._pool: Optional["queue.Queue[sqlite3.Connection]"] = None
        self._pool_lock = threading.Lock()
        self._init_sqlite_pool()

    @classmethod
    def for_db(cls, db_path: Optional[str] = None) -> "M3Context":
        """Return a cached M3Context for the given path (or default).

        Callers should prefer this over M3Context() so pool reuse works across
        invocations that target the same DB. Constructor remains public for
        legacy callers.

        The cache is LRU-bounded. When full, the least-recently-used context's
        pool is closed before the new one is inserted — in-flight connections
        checked out of that pool stay usable (they were captured by the caller
        via ``with get_sqlite_conn()``), but put-back will raise since the
        pool is torn down. Callers that hold conns across context-cache
        pressure should not; the whole design is request-scoped.
        """
        resolved = resolve_db_path(db_path)
        with _CONTEXTS_LOCK:
            ctx = _CONTEXTS.get(resolved)
            if ctx is not None:
                _CONTEXTS.move_to_end(resolved)
                return ctx
            # Miss — build and insert. Evict LRU if full.
            ctx = cls(resolved)
            _CONTEXTS[resolved] = ctx
            while len(_CONTEXTS) > _CONTEXT_CACHE_SIZE:
                evicted_key, evicted_ctx = _CONTEXTS.popitem(last=False)
                logger.debug(
                    f"M3Context cache evicting {evicted_key} "
                    f"(cache size={len(_CONTEXTS) + 1}, cap={_CONTEXT_CACHE_SIZE})"
                )
                _close_context_pool(evicted_ctx)
            return ctx

    def get_path(self, relative_path: str) -> str:
        return os.path.join(self.m3_memory_root, relative_path)

    def get_setting(self, key: str, default: Any = None) -> Any:
        return os.environ.get(key, default)

    def _init_sqlite_pool(self):
        with self._pool_lock:
            if self._pool is not None:
                return
            pool_size = int(os.environ.get("DB_POOL_SIZE", "5"))
            pool_timeout = int(os.environ.get("DB_POOL_TIMEOUT", "30"))
            pool: "queue.Queue[sqlite3.Connection]" = queue.Queue(maxsize=pool_size)
            for _ in range(pool_size):
                try:
                    conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=pool_timeout)
                    conn.row_factory = sqlite3.Row
                    # Centralised pragma stack — profile selected by DB basename.
                    # Gains wal_autocheckpoint + journal_size_limit to bound WAL growth.
                    apply_pragmas(conn, profile_for_db(self.db_path))
                    pool.put(conn)
                except sqlite3.Error as e:
                    logger.error(f"Failed to create SQLite connection for {self.db_path}: {e}")
                    raise
            self._pool = pool
            try:
                self._verify_cohesion()
            except Exception as e:
                # If we fail the cohesion check, let's close the pool and raise
                self._pool = None
                while not pool.empty():
                    try:
                        pool.get_nowait().close()
                    except Exception:
                        pass
                logger.error(f"Cohesion validation failed: {e}")
                raise
            # One-time sanity log per context.
            _probe = pool.queue[0]
            _jm = _probe.execute("PRAGMA journal_mode").fetchone()[0]
            _sy = _probe.execute("PRAGMA synchronous").fetchone()[0]
            logger.info(f"SQLite pool ready: db={self.db_path} journal_mode={_jm} synchronous={_sy} pool_size={pool_size}")

    def get_system_telemetry(self) -> dict:
        """Unify system hardware metrics (CPU, RAM, GPU, Thermal status)."""
        # Try native Rust FFI fast path first
        if not M3_CORE_RS_DISABLE:
            try:
                import m3_core_rs
                if hasattr(m3_core_rs, "get_native_telemetry"):
                    telemetry = m3_core_rs.get_native_telemetry()
                    native_gpu = float(getattr(telemetry, "gpu_total", 0.0))
                    # If the native path reports no GPU (older wheel without GPU
                    # support), fall back to the nvidia-smi probe so the governor
                    # still sees a GPU-pinned local LLM / embed server.
                    gpu_total = native_gpu if native_gpu > 0.0 else probe_gpu_util()
                    return {
                        "cpu_total": float(getattr(telemetry, "cpu_total", 0.0)),
                        "ram_total": float(getattr(telemetry, "ram_total", 0.0)),
                        "gpu_total": gpu_total,
                        "thermal": str(getattr(telemetry, "thermal", "Nominal")),
                    }
            except Exception:
                pass

        try:
            import psutil
        except ImportError:
            return {
                "cpu_total": 0.0,
                "ram_total": 0.0,
                "gpu_total": 0.0,
                "thermal": "Nominal"
            }

        # CPU Total Usage
        try:
            cpu_total = psutil.cpu_percent(interval=None)
        except Exception:
            cpu_total = 0.0

        # RAM Total Usage
        try:
            ram = psutil.virtual_memory()
            ram_total = ram.percent
        except Exception:
            ram_total = 0.0

        # GPU Total Usage — real probe via nvidia-smi (was hardcoded 0.0, which
        # left the governor blind to a GPU-pinned local LLM / embed server).
        gpu_total = probe_gpu_util()

        # Thermal Load
        try:
            from thermal_utils import get_thermal_status
            thermal = get_thermal_status()
        except Exception:
            thermal = "Nominal"

        return {
            "cpu_total": cpu_total,
            "ram_total": ram_total,
            "gpu_total": gpu_total,
            "thermal": thermal
        }

    def _verify_cohesion(self):
        """Verifies the cohesion between the configuration salt and the database.

        Creates the `m3_system_cohesion` metadata table if it does not exist, and
        stores or re-verifies the SHA-256 hash of the active encryption salt.
        """
        try:
            from auth_utils import get_salt_path
        except ImportError:
            return

        salt_path = get_salt_path()
        if not salt_path or not os.path.exists(salt_path):
            return

        try:
            with open(salt_path, "rb") as f:
                salt_bytes = f.read()
            salt_hash = hashlib.sha256(salt_bytes).hexdigest()
        except Exception as e:
            logger.warning(f"Failed to read/hash salt for cohesion check: {e}")
            return

        with self.get_sqlite_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS m3_system_cohesion (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

            row = conn.execute("SELECT value FROM m3_system_cohesion WHERE key = 'salt_hash'").fetchone()
            if row:
                stored_hash = row[0]
                if stored_hash != salt_hash:
                    raise RuntimeError(
                        f"CRITICAL COHESION ERROR: Active configuration salt mismatch with stored database hash!\n"
                        f"Stored Hash: {stored_hash}\n"
                        f"Active Hash: {salt_hash}\n"
                        f"This database was previously encrypted with a different salt. Decoupled path mismatch detected.\n"
                        f"Please reconcile your config and engine folders or env overrides (M3_CONFIG_ROOT / M3_ENGINE_ROOT)."
                    )
            else:
                conn.execute(
                    "INSERT INTO m3_system_cohesion (key, value) VALUES ('salt_hash', ?)",
                    (salt_hash,)
                )
                conn.commit()


    def _check_circuit(self, service: str) -> bool:
        """Checks if the circuit for a specific service is open."""
        if not M3_CORE_RS_DISABLE:
            try:
                import m3_core_rs
                if hasattr(m3_core_rs, "NativeCircuitBreaker"):
                    state = _CIRCUITS.get(service)
                    if not state or not hasattr(state, "check"):
                        state = m3_core_rs.NativeCircuitBreaker(3, 60)
                        _CIRCUITS[service] = state
                    return state.check()
            except Exception:
                pass

        state = _CIRCUITS.get(service)
        if state is None or isinstance(state, dict):
            if not state:
                return True
            if state["open_until"] > time.time():
                logger.error(f"Circuit for {service} is OPEN. Failing fast.")
                return False
            return True
        return True

    def _record_success(self, service: str):
        if not M3_CORE_RS_DISABLE:
            try:
                import m3_core_rs
                if hasattr(m3_core_rs, "NativeCircuitBreaker"):
                    state = _CIRCUITS.get(service)
                    if state and hasattr(state, "record_success"):
                        state.record_success()
                        return
            except Exception:
                pass

        if service in _CIRCUITS:
            del _CIRCUITS[service]

    def _record_failure(self, service: str, custom_cooldown: Optional[float] = None):
        if not M3_CORE_RS_DISABLE:
            try:
                import m3_core_rs
                if hasattr(m3_core_rs, "NativeCircuitBreaker"):
                    state = _CIRCUITS.get(service)
                    if not state or not hasattr(state, "check"):
                        cooldown = int(custom_cooldown or _CB_COOLDOWN)
                        state = m3_core_rs.NativeCircuitBreaker(3, cooldown)
                        _CIRCUITS[service] = state
                    if hasattr(state, "record_failure"):
                        state.record_failure()
                        return
            except Exception:
                pass

        state = _CIRCUITS.get(service)
        if state is None or not isinstance(state, dict):
            state = {"failures": 0, "open_until": 0}
        state["failures"] += 1
        cooldown = custom_cooldown or _CB_COOLDOWN
        if state["failures"] >= _CB_THRESHOLD:
            state["open_until"] = time.time() + cooldown
            logger.warning(f"Circuit for {service} OPENED for {cooldown}s.")
        _CIRCUITS[service] = state

    def get_async_client(self) -> httpx.AsyncClient:
        """Returns a shared httpx.AsyncClient, recreating if the event loop has changed."""
        global _HTTP_CLIENT, _HTTP_CLIENT_LOOP_ID
        try:
            loop_id = id(asyncio.get_running_loop())
        except RuntimeError:
            loop_id = None
        if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed or loop_id != _HTTP_CLIENT_LOOP_ID:
            with _HTTP_CLIENT_LOCK:
                # Double-check inside lock to prevent redundant recreation
                if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed or loop_id != _HTTP_CLIENT_LOOP_ID:
                    timeout = httpx.Timeout(connect=5.0, read=4800.0, write=10.0, pool=5.0)
                    from crypto_provider import provider as crypto
                    ssl_ctx = crypto.get_ssl_context()

                    try:
                        _HTTP_CLIENT = httpx.AsyncClient(timeout=timeout, http2=True, verify=ssl_ctx)
                        logger.debug("Initialized shared httpx.AsyncClient with HTTP/2 and hardened SSL.")
                    except ImportError:
                        _HTTP_CLIENT = httpx.AsyncClient(timeout=timeout, http2=False, verify=ssl_ctx)
                        logger.info("HTTP/2 support not found (h2 package missing). Falling back to HTTP/1.1.")
                    _HTTP_CLIENT_LOOP_ID = loop_id
        return _HTTP_CLIENT

    async def aclose(self):
        """Closes the shared async client if it exists (H4)."""
        global _HTTP_CLIENT
        if _HTTP_CLIENT and not _HTTP_CLIENT.is_closed:
            await _HTTP_CLIENT.aclose()
            logger.debug("Closed shared httpx.AsyncClient.")

    async def request_with_retry(self, method: str, url: str, retries: int = 3, **kwargs):
        """Resilient HTTP requests with exponential backoff and Circuit Breaker."""
        service = url.split("//")[-1].split("/")[0]

        if not self._check_circuit(service):
            raise httpx.HTTPStatusError(f"Circuit open for {service}", request=None, response=None) # type: ignore

        client = self.get_async_client()
        for attempt in range(retries):
            try:
                resp = await client.request(method, url, **kwargs)
                resp.raise_for_status()
                self._record_success(service)
                return resp
            except (httpx.HTTPStatusError, httpx.NetworkError, httpx.TimeoutException) as exc:
                if attempt == retries - 1:
                    self._record_failure(service)
                    logger.error(f"HTTP Request failed after {retries} attempts: {exc}")
                    raise
                wait = (2 ** attempt) + random.uniform(0, 1)  # nosec B311 - backoff jitter, not cryptographic
                logger.warning(f"Request to {url} failed ({exc}). Retrying in {wait:.1f}s...")
                await asyncio.sleep(wait)

    @contextmanager
    def get_sqlite_conn(self) -> sqlite3.Connection:
        # Capture self._pool inside the with so put-back lands in the correct
        # pool even if another thread somehow swaps the attribute. (It won't
        # today, but cheap insurance for a multi-pool world.)
        if self._pool is None:
            self._init_sqlite_pool()
        pool = self._pool
        conn = pool.get(timeout=10)
        try:
            yield conn
        finally:
            pool.put(conn)

    @contextmanager
    def get_chatlog_conn(self) -> sqlite3.Connection:
        """Yield a SQLite connection for chat log writes/reads.

        Resolution: the chatlog DB path comes from chatlog_config (which now
        honors CHATLOG_DB_PATH > active M3_DATABASE > default agent_chatlog.db).
        If the resolved chatlog path equals this context's main path, we reuse
        the main pool. Otherwise a dedicated chatlog-tuned pool is used.
        """
        try:
            import chatlog_config
        except ImportError:
            with self.get_sqlite_conn() as conn:
                yield conn
            return

        target = chatlog_config.chatlog_db_path()
        if os.path.abspath(target) == os.path.abspath(self.db_path):
            with self.get_sqlite_conn() as conn:
                yield conn
            return

        with chatlog_config.chatlog_sqlite_conn() as conn:
            yield conn

    def get_secret(self, service: str) -> Optional[str]:
        # Lazy import: auth_utils may route through M3Context for vault reads,
        # creating a cycle if imported at module top.
        from auth_utils import get_api_key
        return get_api_key(service)

    def get_logger(self, name: str = "m3") -> "StructuredLogger":
        """Return a StructuredLogger for grep-friendly key=value output.

        Thin convenience accessor; main's StructuredLogger is stateless so
        the returned instance is shareable across calls. The ``name``
        parameter is reserved for a future namespacing pass — currently
        ignored, kept in the signature to match bench-wip callers.
        """
        return StructuredLogger()

    def query_memory(self, sql: str, params: tuple = ()) -> list:
        """Read-only ad-hoc SQL against the active pool.

        Convenience wrapper for bridges that want to run a quick SELECT
        without managing their own context manager. Callers must NOT pass
        mutating SQL here — the wrapper doesn't commit and the connection
        returns to the pool mid-transaction, which silently loses the
        write on the next borrow. Use ``get_sqlite_conn()`` for writes.
        """
        with self.get_sqlite_conn() as conn:
            return conn.execute(sql, params).fetchall()

    def log_event(self, category: str, detail_a: str,
                  detail_b: str = "", detail_c: Optional[str] = None) -> None:
        """Route a structured event to the correct legacy table.

        Used by bridges that predate the unified memory_items model.
        Categories: 'thought'/'activity' → activity_logs; 'decision' → project_decisions.
        Unknown categories fall through to activity_logs for safety.
        """
        from audit_trail import log_event
        log_event(self, category, detail_a, detail_b, detail_c)


    @contextmanager
    def pg_connection(self):
        """Returns a psycopg2 connection to the PostgreSQL data warehouse with circuit breaker."""
        import psycopg2
        if not self._check_circuit("postgresql"):
            raise RuntimeError("PostgreSQL circuit breaker is open. Failing fast.")
        url = os.getenv("PG_URL") or self.get_secret("PG_URL")
        if not url:
            raise RuntimeError("PG_URL not found in environment or keychain.")
        last_exc = None
        for attempt in range(2):
            try:
                conn = psycopg2.connect(url, connect_timeout=10)
                self._record_success("postgresql")
                try:
                    yield conn
                finally:
                    conn.close()
                return
            except psycopg2.OperationalError as e:
                last_exc = e
                self._record_failure("postgresql")
                if attempt < 1:
                    logger.warning(f"PostgreSQL connect attempt {attempt + 1} failed: {e}. Retrying in 3s...")
                    time.sleep(3)
        raise RuntimeError(f"PostgreSQL connection failed after 2 attempts: {last_exc}")


class StructuredLogger:
    """Renders structured log lines as `event | k=v | k=v` for grep-friendly output."""

    def format(self, event: str, *args, **kwargs) -> str:
        return format_log(event, *args, **kwargs)

    def log(self, event: str, *args, **kwargs) -> None:
        """Helper to format and print a structured log line to stderr."""
        print(self.format(event, *args, **kwargs), file=sys.stderr)


def _cleanup():
    with _CONTEXTS_LOCK:
        contexts = list(_CONTEXTS.values())
        _CONTEXTS.clear()
    for ctx in contexts:
        _close_context_pool(ctx)

atexit.register(_cleanup)
