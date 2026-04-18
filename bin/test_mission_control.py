#!/usr/bin/env python3
"""
test_mission_control.py — Single-pass smoke test for mission_control.py.
Runs one iteration of the dashboard and validates all subsystems.
"""
import pathlib
import platform
import sys

# UTF-8 stdout on Windows
if platform.system() == "Windows":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

BASE = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(BASE / "bin"))

from mission_control import (
    DB_PATH,
    IS_LINUX,
    IS_MAC,
    IS_WIN,
    draw_bar,
    get_api_token,
    get_gpu_usage,
    get_hw_info,
    get_kv_stats,
    get_latest_activity,
    get_vram_usage,
    gpu_label,
    ping_ms,
    run_dashboard,
)

PASS = 0
FAIL = 0
SKIP = 0

def ok(label):
    global PASS
    PASS += 1
    print(f"  [PASS] {label}", flush=True)

def fail(label, detail=""):
    global FAIL
    FAIL += 1
    print(f"  [FAIL] {label}{' -- ' + detail if detail else ''}", flush=True)

def skip(label, reason=""):
    global SKIP
    SKIP += 1
    print(f"  [SKIP] {label}{' (' + reason + ')' if reason else ''}", flush=True)

def check(cond, label, detail=""):
    if cond:
        ok(label)
    else:
        fail(label, detail)


print("=" * 60, flush=True)
print("  Mission Control — Cross-Platform Smoke Test", flush=True)
print(f"  Platform: {platform.system()} {platform.release()}", flush=True)
print("=" * 60, flush=True)

# ── 1: Platform flags ─────────────────────────────────────────────────────────
print("\n-- 1: Platform detection --", flush=True)
flags = [IS_WIN, IS_MAC, IS_LINUX]
check(sum(flags) == 1,  "exactly one platform flag is True", str(flags))
check(platform.system() == ("Windows" if IS_WIN else "Darwin" if IS_MAC else "Linux"),
      "platform flag matches platform.system()")

# ── 2: Paths ──────────────────────────────────────────────────────────────────
print("\n-- 2: Paths --", flush=True)
check(BASE.exists(),           "BASE dir exists",    str(BASE))
check(DB_PATH.parent.exists(), "memory dir exists",  str(DB_PATH.parent))
if DB_PATH.exists():
    ok("agent_memory.db found")
else:
    skip("agent_memory.db not found", "run setup_memory.py first")

# ── 3: draw_bar ───────────────────────────────────────────────────────────────
print("\n-- 3: draw_bar --", flush=True)
bar0   = draw_bar(0)
bar50  = draw_bar(50)
bar100 = draw_bar(100)
check("░" in bar0,   "0% bar has empty blocks")
check("█" in bar50,  "50% bar has filled blocks")
check("░" in bar50,  "50% bar has empty blocks")
check("0.0%" in bar0,   "0% bar shows percentage")
check("50.0%" in bar50, "50% bar shows percentage")

# ── 4: Auth token ─────────────────────────────────────────────────────────────
print("\n-- 4: API token --", flush=True)
token = get_api_token()
if token:
    ok("get_api_token() returned a value")
    check(len(token) > 5, "token has plausible length")
else:
    skip("get_api_token() returned None", "LM_API_TOKEN not set / keychain empty")

# ── 5: LM Studio KV stats ─────────────────────────────────────────────────────
print("\n-- 5: LM Studio KV stats --", flush=True)
loaded, maximum, pct, status, model_name = get_kv_stats()
check(isinstance(status, str), "get_kv_stats returns string status")
check(status in ("OK", "IDLE", "OFFLINE", "TIMEOUT", "UNAUTHORIZED",
                 "TOKEN_MISSING") or status.startswith("HTTP_"),
      "status is a known value", status)
if status == "OK":
    check(maximum > 0,      "max_ctx > 0 when OK")
    check(0 <= pct <= 100,  "pct in [0, 100] when OK")
    ok(f"LM Studio online: {loaded:,}/{maximum:,} ctx ({pct:.1f}%)")
elif status == "IDLE":
    ok("LM Studio running but no model loaded")
else:
    skip(f"LM Studio unreachable ({status})", "start server to test fully")

# ── 6: GPU usage ──────────────────────────────────────────────────────────────
print("\n-- 6: GPU usage --", flush=True)
gpu_pct = get_gpu_usage()
label   = gpu_label()
check(isinstance(gpu_pct, float),      "get_gpu_usage returns float")
check(0.0 <= gpu_pct <= 100.0,         "GPU% in [0, 100]")
check(isinstance(label, str) and label, "gpu_label returns non-empty string")
ok(f"GPU: {label} @ {gpu_pct:.1f}%")

# ── 6b: VRAM usage ────────────────────────────────────────────────────────────
print("\n-- 6b: VRAM usage --", flush=True)
vram = get_vram_usage()
if IS_MAC:
    check(vram is None, "get_vram_usage returns None on macOS (unified memory)")
    ok("macOS: VRAM bar correctly suppressed")
elif vram is not None:
    used_gb, total_gb, pct = vram
    check(total_gb > 0,        "VRAM total_gb > 0")
    check(0 <= pct <= 100,     "VRAM pct in [0, 100]")
    check(used_gb <= total_gb, "VRAM used <= total")
    ok(f"VRAM: {used_gb:.1f}/{total_gb:.0f}GB ({pct:.1f}%)")
else:
    skip("VRAM usage", "nvidia-smi not available")

# ── 7: Ping ───────────────────────────────────────────────────────────────────
print("\n-- 7: Ping latency --", flush=True)
lat = ping_ms("api.perplexity.ai")
if lat:
    ok(f"ping api.perplexity.ai => {lat}")
    check(lat.endswith("ms"), "latency string ends with 'ms'")
else:
    skip("ping api.perplexity.ai", "network unreachable")

lat_grok = ping_ms("api.x.ai")
if lat_grok:
    ok(f"ping api.x.ai => {lat_grok}")
else:
    skip("ping api.x.ai", "network unreachable")

# ── 8: Hardware info + SQLite helpers ────────────────────────────────────────
print("\n-- 8: Hardware info + SQLite helpers --", flush=True)
chip, mem, gpu_info = get_hw_info()
check(isinstance(chip, str) and len(chip) > 0,  "get_hw_info returns chip string")
check(isinstance(mem, str) and "GB" in mem,      "get_hw_info returns mem string")
check(isinstance(gpu_info, str),                 "get_hw_info returns gpu_info string")
ok(f"Chip: {chip}")
ok(f"RAM:  {mem}")
ok(f"GPU:  {gpu_info if gpu_info else '(not detected)'}")

if DB_PATH.exists():
    focus, logs = get_latest_activity()
    check(isinstance(focus, str), "get_latest_activity focus is str")
    check(isinstance(logs, list), "get_latest_activity logs is list")
    ok(f"Focus: {str(focus)[:40]}")
else:
    skip("SQLite helpers", "DB not found")

# ── 9: Single-iteration dashboard render ──────────────────────────────────────
print("\n-- 9: Single-iteration dashboard render --", flush=True)
try:
    run_dashboard(iterations=1)
    ok("run_dashboard(iterations=1) completed without exception")
except Exception as e:
    fail("run_dashboard raised exception", str(e))

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 60, flush=True)
print(f"  RESULTS:  {PASS} passed  |  {FAIL} failed  |  {SKIP} skipped", flush=True)
print("=" * 60, flush=True)

if FAIL:
    sys.exit(1)
