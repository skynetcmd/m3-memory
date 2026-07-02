import logging
import os
import sys
import time
from typing import Any

logger = logging.getLogger("M3_SDK")

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
