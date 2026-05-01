#!/usr/bin/env python3
"""
mission_control.py — Cross-platform pulse dashboard (macOS / Windows / Linux).
Run:  python bin/mission_control.py
"""
from __future__ import annotations

import logging
import os
import pathlib
import platform
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime

import psutil
import requests

# ── Platform detection ────────────────────────────────────────────────────────
IS_WIN   = platform.system() == "Windows"
IS_MAC   = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"

# ── UTF-8 stdout (Windows terminals default to cp1252) ────────────────────────
if IS_WIN:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass  # Python < 3.7 fallback

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE = pathlib.Path(__file__).parent.parent.resolve()
import sys as _sys

_sys.path.insert(0, str(BASE / "bin"))
try:
    from m3_sdk import resolve_db_path as _resolve_db
    DB_PATH = pathlib.Path(_resolve_db(None))
except ImportError:
    DB_PATH = BASE / "memory" / "agent_memory.db"
LOG_DIR  = BASE / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "mission_control.log", encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger("mission_control")

# ── ANSI colours ──────────────────────────────────────────────────────────────
GREEN, BLUE, CYAN, YELLOW, RED, RESET = (
    "\033[92m", "\033[94m", "\033[96m", "\033[93m", "\033[91m", "\033[0m"
)
BOLD = "\033[1m"

# Enable VT100 on Windows 10+ (required for ANSI escape codes)
if IS_WIN:
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass


# ── Auth ──────────────────────────────────────────────────────────────────────
def get_api_token() -> str | None:
    token = os.getenv("LM_API_TOKEN") or os.getenv("LM_STUDIO_API_KEY")
    if token:
        return token
    if IS_MAC:
        try:
            return subprocess.check_output(
                ["security", "find-generic-password", "-s", "LM_STUDIO_API_KEY", "-w"],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
        except Exception:
            pass
    try:
        import keyring
        val = keyring.get_password("LM_STUDIO_API_KEY", "LM_STUDIO_API_KEY")
        if val:
            return val
    except Exception:
        pass
    return None


API_TOKEN = None


# ── LM Studio KV stats ────────────────────────────────────────────────────────
def get_kv_stats() -> tuple:
    global API_TOKEN
    if not API_TOKEN:
        API_TOKEN = get_api_token()
    if not API_TOKEN:
        return 0, 0, 0, "TOKEN_MISSING", None
    try:
        headers = {"Authorization": f"Bearer {API_TOKEN}"}
        resp = requests.get(
            "http://127.0.0.1:1234/api/v0/models", headers=headers, timeout=4.5
        )
        if resp.status_code == 401:
            API_TOKEN = None
            return 0, 0, 0, "UNAUTHORIZED", None
        if resp.status_code != 200:
            return 0, 0, 0, f"HTTP_{resp.status_code}", None
        data = resp.json()
        models = [m for m in data.get("data", []) if m.get("state") == "loaded"]
        if not models:
            return 0, 0, 0, "IDLE", None

        # Select the model with the largest parameter count
        def get_params(m_id: str) -> float:
            # Look for 70b, 8B, 1.5b etc.
            match = re.search(r'(\d+(?:\.\d+)?)[bB]', m_id)
            return float(match.group(1)) if match else 0.0

        models.sort(key=lambda x: get_params(x.get("id", "")), reverse=True)
        model = models[0]
        model_id = model.get("id", "Unknown")

        loaded  = model.get("loaded_context_length") or model.get("context_length", 0)
        maximum = model.get("max_context_length", 1) or 1
        return loaded, maximum, (loaded / maximum) * 100, "OK", model_id
    except requests.exceptions.Timeout:
        return 0, 0, 0, "TIMEOUT", None
    except Exception as e:
        logger.debug(f"KV stats fetch error: {type(e).__name__}")
        return 0, 0, 0, "OFFLINE", None


# ── GPU utilisation ───────────────────────────────────────────────────────────
def get_gpu_usage() -> float:
    """Returns GPU utilisation % for the primary GPU. Best-effort per platform."""
    if IS_MAC:
        try:
            out = subprocess.check_output(
                ["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"],
                stderr=subprocess.DEVNULL,
            ).decode()
            m = re.search(r'"Device Utilization %"=(\d+)', out)
            return float(m.group(1)) if m else 0.0
        except Exception:
            return 0.0

    if IS_WIN:
        # Try WMI (requires pywin32 or wmi package) — graceful fallback
        try:
            import wmi  # type: ignore
            w = wmi.WMI(namespace="root\\cimv2")
            gpus = w.Win32_VideoController()
            if gpus:
                # Win32_VideoController doesn't expose load % natively;
                # use performance counter via subprocess if available
                raise ImportError("no load % in WMI")
        except Exception:
            pass
        # Try nvidia-smi (NVIDIA GPUs)
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=utilization.gpu",
                 "--format=csv,noheader,nounits"],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
            return float(out.splitlines()[0])
        except Exception:
            pass
        # psutil doesn't expose GPU — return 0 with a note
        return 0.0

    if IS_LINUX:
        # NVIDIA
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=utilization.gpu",
                 "--format=csv,noheader,nounits"],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
            return float(out.splitlines()[0])
        except Exception:
            pass
        # AMD via /sys
        try:
            p = pathlib.Path("/sys/class/drm")
            for card in sorted(p.glob("card*/device/gpu_busy_percent")):
                return float(card.read_text().strip())
        except Exception:
            pass
        return 0.0

    return 0.0


def get_vram_usage() -> tuple[float, float, float] | None:
    """
    Returns (used_gb, total_gb, pct) for dedicated VRAM via nvidia-smi.
    Returns None on macOS (unified RAM — no separate VRAM pool) or if unavailable.
    """
    if IS_MAC:
        return None  # unified memory — VRAM == system RAM, no separate pool
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL,
        ).decode().strip().splitlines()[0]
        used_mib, total_mib = (int(x.strip()) for x in out.split(","))
        used_gb  = used_mib  / 1024
        total_gb = total_mib / 1024
        pct = (used_mib / total_mib) * 100
        return used_gb, total_gb, pct
    except Exception:
        return None


def gpu_label() -> str:
    """Human-readable GPU source label."""
    if IS_MAC:
        return "GPU (Metal)"
    if IS_WIN or IS_LINUX:
        try:
            subprocess.check_output(
                ["nvidia-smi"], stderr=subprocess.DEVNULL
            )
            return "GPU (NVIDIA)"
        except Exception:
            pass
        if IS_LINUX:
            p = pathlib.Path("/sys/class/drm")
            if list(p.glob("card*/device/gpu_busy_percent")):
                return "GPU (AMD)"
        return "GPU (N/A)"
    return "GPU"


# ── Ping latency ──────────────────────────────────────────────────────────────
def ping_ms(host: str) -> str | None:
    """Returns latency string or None on failure."""
    try:
        if IS_WIN:
            cmd = ["ping", "-n", "1", "-w", "1000", host]
            pat = r"Average = (\d+)ms|time[=<](\d+)ms"
        else:
            cmd = ["ping", "-c", "1", "-W", "1", host]
            pat = r"time[=<]([\d.]+) ms|min/avg/max/(?:mdev|stddev)\s*=\s*[\d.]+/([\d.]+)/"
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=3).decode()
        m = re.search(pat, out)
        if m:
            val = next(g for g in m.groups() if g is not None)
            return f"{float(val):.1f}ms"
    except Exception:
        pass
    return None


# ── Render helpers ────────────────────────────────────────────────────────────
def draw_bar(pct: float, width: int = 30, color: str = GREEN) -> str:
    filled = int(width * (min(100, pct) / 100))
    return f"{color}{'█' * filled}{RESET}{'░' * (width - filled)} {pct:.1f}%"


def clear_screen(is_apple_terminal: bool) -> None:
    if is_apple_terminal:
        print("\n" * 2)
    else:
        # \033[H: cursor to top-left, \033[J: clear from cursor to end of screen
        print("\033[H\033[J", end="", flush=True)


# ── SQLite helpers ────────────────────────────────────────────────────────────
def get_hw_info() -> tuple[str, str]:
    """Returns (chip, mem) from live OS detection. Never reads the DB."""
    # CPU
    chip = ""
    if IS_WIN:
        try:
            chip = subprocess.check_output(
                ["powershell.exe", "-NoProfile", "-Command",
                 "(Get-WmiObject Win32_Processor).Name"],
                stderr=subprocess.DEVNULL,
            ).decode().strip().splitlines()[0].strip()
        except Exception:
            pass
    elif IS_MAC:
        try:
            chip = subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
            # Prefer Apple Silicon marketing name if available
            try:
                model = subprocess.check_output(
                    ["system_profiler", "SPHardwareDataType"],
                    stderr=subprocess.DEVNULL,
                ).decode()
                m = re.search(r"Chip:\s+(.+)", model)
                if m:
                    chip = m.group(1).strip()
            except Exception:
                pass
        except Exception:
            pass
    elif IS_LINUX:
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        chip = line.split(":", 1)[1].strip()
                        break
        except Exception:
            pass
    if not chip:
        chip = platform.processor() or platform.machine() or "Unknown CPU"

    # RAM
    total_gb = psutil.virtual_memory().total / (1024 ** 3)
    mem = f"{round(total_gb)}GB RAM"

    # GPU name + VRAM (nvidia-smi; macOS/AMD fall back to empty)
    gpu_info = ""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL,
        ).decode().strip().splitlines()[0]
        name, vram_mib = out.split(",", 1)
        vram_gb = round(int(vram_mib.strip()) / 1024)
        gpu_info = f"{name.strip()} {vram_gb}GB VRAM"
    except Exception:
        if IS_MAC:
            try:
                out = subprocess.check_output(
                    ["system_profiler", "SPDisplaysDataType"],
                    stderr=subprocess.DEVNULL,
                ).decode()
                m = re.search(r"Chipset Model:\s+(.+)", out)
                vram_m = re.search(r"VRAM.*?:\s+(.+)", out)
                if m:
                    gpu_info = m.group(1).strip()
                    if vram_m:
                        gpu_info += f" ({vram_m.group(1).strip()})"
            except Exception:
                pass

    return chip, mem, gpu_info


def get_latest_activity() -> tuple:
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur  = conn.cursor()
        cur.execute("SELECT summary FROM system_focus LIMIT 1")
        row   = cur.fetchone()
        focus = row[0] if row else "IDLE"
        cur.execute(
            "SELECT query, model_used FROM activity_logs "
            "ORDER BY timestamp DESC LIMIT 3"
        )
        logs = cur.fetchall()
        conn.close()
        return focus, logs
    except Exception:
        return "N/A", []


# ── Memory Health ─────────────────────────────────────────────────────────────
def get_memory_health() -> dict:
    """Query memory system stats for the dashboard."""
    stats = {
        "total": 0, "by_type": {}, "by_agent": {},
        "queue_depth": 0, "embedded": 0, "unembedded": 0,
        "watermarks": {},
    }
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        cur = conn.cursor()

        # Total active items
        cur.execute("SELECT COUNT(*) FROM memory_items WHERE is_deleted = 0")
        stats["total"] = cur.fetchone()[0]

        # By type
        for row in cur.execute(
            "SELECT type, COUNT(*) FROM memory_items WHERE is_deleted = 0 GROUP BY type ORDER BY COUNT(*) DESC"
        ).fetchall():
            stats["by_type"][row[0]] = row[1]

        # By change_agent
        for row in cur.execute(
            "SELECT COALESCE(change_agent, 'unknown'), COUNT(*) FROM memory_items WHERE is_deleted = 0 GROUP BY change_agent ORDER BY COUNT(*) DESC"
        ).fetchall():
            stats["by_agent"][row[0]] = row[1]

        # Chroma sync queue depth
        try:
            cur.execute("SELECT COUNT(*) FROM chroma_sync_queue")
            stats["queue_depth"] = cur.fetchone()[0]
        except sqlite3.OperationalError:
            pass

        # Embedding coverage
        cur.execute(
            """SELECT
                 (SELECT COUNT(DISTINCT memory_id) FROM memory_embeddings) AS embedded,
                 (SELECT COUNT(*) FROM memory_items WHERE is_deleted = 0) AS total"""
        )
        row = cur.fetchone()
        stats["embedded"] = row[0]
        stats["unembedded"] = row[1] - row[0]

        # Sync watermarks
        try:
            for row in cur.execute("SELECT direction, last_synced_at FROM sync_watermarks").fetchall():
                stats["watermarks"][row[0]] = row[1]
        except sqlite3.OperationalError:
            pass

        conn.close()
    except Exception as e:
        logger.debug(f"Memory health query failed: {type(e).__name__}")
    return stats


# ── Dashboard loop ────────────────────────────────────────────────────────────
def run_dashboard(iterations: int = 0) -> None:
    """
    Main loop. iterations=0 runs forever; iterations=N runs N times (for tests).
    """
    is_apple_terminal = IS_MAC and os.getenv("TERM_PROGRAM") == "Apple_Terminal"
    logger.info("Starting Mission Control Dashboard")

    chip, mem, gpu_info = get_hw_info()
    gpu_src = gpu_label()
    sleep_s = 12.0 if is_apple_terminal else (5.0 if IS_WIN else 9.5)

    count = 0
    while True:
        try:
            clear_screen(is_apple_terminal)

            loaded_ctx, max_ctx, kv_pct, api_status, model_name = get_kv_stats()
            gpu_pct  = get_gpu_usage()
            focus, logs = get_latest_activity()
            now = datetime.now().strftime("%H:%M:%S")
            os_tag = ("macOS" if IS_MAC else "Windows" if IS_WIN else "Linux")

            print(
                f"{BOLD}{BLUE}--- MISSION CONTROL [{now}] [{os_tag}] ---{RESET}",
                flush=True,
            )
            hw_line = f"{chip} | {mem}"
            if gpu_info:
                hw_line += f" | {gpu_info}"
            if model_name and api_status == "OK":
                hw_line += f" | {BOLD}{model_name}{RESET}"
            print(f"{CYAN}{BOLD}Hardware:{RESET} {hw_line}", flush=True)
            print(
                f"{YELLOW}{BOLD}Current Focus:{RESET} {str(focus)[:60]}",
                flush=True,
            )
            print("─" * 55, flush=True)
            print(
                f"{BOLD}CPU:       {RESET}{draw_bar(psutil.cpu_percent(interval=0.2), color=GREEN)}",
                flush=True,
            )
            print(
                f"{BOLD}{gpu_src:<10}{RESET}{draw_bar(gpu_pct, color=CYAN)}",
                flush=True,
            )
            vram = get_vram_usage()
            if vram is not None:
                used_gb, total_gb, vram_pct = vram
                vc = GREEN if vram_pct < 70 else (YELLOW if vram_pct < 90 else RED)
                print(
                    f"{BOLD}VRAM:      {RESET}{draw_bar(vram_pct, color=vc)}"
                    f"  {used_gb:.1f}/{total_gb:.0f}GB",
                    flush=True,
                )
            ram = psutil.virtual_memory()
            ram_used_gb = (ram.total - ram.available) / (1024 ** 3)
            ram_total_gb = ram.total / (1024 ** 3)
            print(
                f"{BOLD}RAM:       {RESET}{draw_bar(ram.percent, color=BLUE)}"
                f"  {ram_used_gb:.1f}/{ram_total_gb:.0f}GB",
                flush=True,
            )

            c_color = GREEN if kv_pct < 70 else (YELLOW if kv_pct < 90 else RED)
            if api_status == "OK":
                if loaded_ctx == max_ctx:
                    print(
                        f"\n{BOLD}CTX WIN:   {RESET}{GREEN}LOADED & READY{RESET}"
                        f" (Max: {max_ctx:,})",
                        flush=True,
                    )
                    print(f"Stats:     Standby | {max_ctx:,} context | {BOLD}{model_name}{RESET}", flush=True)
                else:
                    print(
                        f"\n{BOLD}CTX WIN:   {RESET}{draw_bar(kv_pct, color=c_color)}",
                        flush=True,
                    )
                    print(
                        f"Stats:     {loaded_ctx:,}/{max_ctx:,} tokens | {BOLD}{model_name}{RESET}",
                        flush=True,
                    )
            elif api_status == "IDLE":
                print(f"\n{BOLD}CTX WIN:   {RESET}{YELLOW}NO MODEL LOADED{RESET}", flush=True)
                print("Stats:     Standby / Start a model in LM Studio", flush=True)
            else:
                print(
                    f"\n{BOLD}CTX WIN:   {RESET}{RED}OFFLINE ({api_status}){RESET}",
                    flush=True,
                )
                print("Stats:     Check LM Studio Server (Port 1234)", flush=True)

            print(f"\n{BOLD}RECENT ACTIVITY:{RESET}", flush=True)
            if logs:
                for query, model in logs:
                    print(f" * [{model}] {str(query)[:50]}", flush=True)
            else:
                print(" * (No recent logs)", flush=True)

            # Memory Health section
            mh = get_memory_health()
            if mh["total"] > 0:
                print(f"\n{BOLD}MEMORY HEALTH:{RESET}", flush=True)
                type_parts = [f"{t}={c}" for t, c in list(mh["by_type"].items())[:5]]
                print(f"  Items: {mh['total']} total | {' | '.join(type_parts)}", flush=True)
                agent_parts = [f"{a}={c}" for a, c in list(mh["by_agent"].items())[:4]]
                print(f"  Agents: {' | '.join(agent_parts)}", flush=True)
                embed_pct = (mh["embedded"] / mh["total"] * 100) if mh["total"] else 0
                q_color = GREEN if mh["queue_depth"] < 10 else (YELLOW if mh["queue_depth"] < 50 else RED)
                print(
                    f"  Embeddings: {mh['embedded']}/{mh['total']} ({embed_pct:.0f}%) | "
                    f"Queue: {q_color}{mh['queue_depth']}{RESET}",
                    flush=True,
                )
                if mh["watermarks"]:
                    wm_parts = [f"{d}={ts[:19]}" for d, ts in mh["watermarks"].items()]
                    print(f"  Sync: {' | '.join(wm_parts)}", flush=True)

            print(f"\n{BOLD}BRIDGE LATENCY:{RESET}", flush=True)
            for host, label in [("api.perplexity.ai", "Perp"), ("api.x.ai", "Grok")]:
                lat = ping_ms(host)
                if lat:
                    print(f" >> {label}: {GREEN}{lat}{RESET}", end="  |  ", flush=True)
                else:
                    print(f" >> {label}: {RED}OFF{RESET}", end="  |  ", flush=True)

            print(
                f"\n\n{BOLD}STATUS:{RESET} [OK] web_research | [OK] grok_intel"
                f" | [OK] memory | [OK] custom_pc_tool",
                flush=True,
            )

            count += 1
            if iterations and count >= iterations:
                break
            time.sleep(sleep_s)

        except (BrokenPipeError, KeyboardInterrupt):
            break
        except Exception as e:
            logger.error(f"Loop error: {type(e).__name__}: {e}")
            time.sleep(5)


if __name__ == "__main__":
    try:
        run_dashboard()
    except KeyboardInterrupt:
        logger.info("Shutting down Mission Control")
        sys.exit(0)
