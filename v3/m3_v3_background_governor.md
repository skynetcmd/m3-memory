# 🧠 M3-v3: Adaptive Background Workload Governor

This document presents the architecture and implementation design for the **M3 Adaptive Background Workload Governor**. The governor dynamically paces and throttles both non-interactive background tasks and interactive single-process tools based on user-selectable hardware resource limits.

---

## 🏗️ 1. Core Architecture: Fine-Grained Resource Gating

The Governor operates under two configurable resource limits to ensure the host machine remains perfectly responsive under general workloads.

```
                   [ System Resource Utilization ]
         0%                       85%           95%           100%
         ├─────────────────────────┼─────────────┼──────────────┤
         │     Continuous Mode     │Throttled    │Critical Mode │100% Limit
         │   (Background standard) │(Back step)  │(Inter step)  │Override
         │   - BG: 100ms - 5s delay│- BG: 5-10s  │- BG: HALTED  │- Interactive:
         │   - Interactive: 0s     │- Inter: 0s  │- Inter: 30-60s│  unthrottled
```

### 1. User-Selectable Thresholds
These limits are configurable via environment variables or `.env` settings:
*   `M3_GOVERNOR_INITIAL_THRESHOLD` (Default: `85`): System load percentage above which **background tasks** begin stepping back.
*   `M3_GOVERNOR_LIMIT_THRESHOLD` (Default: `95`): System load percentage above which **interactive processes** step back to prevent system freezing.
*   *Validation Rule:* The initial threshold must always be strictly lower than the limit threshold:
    $$\text{M3\_GOVERNOR\_INITIAL\_THRESHOLD} < \text{M3\_GOVERNOR\_LIMIT\_THRESHOLD}$$
*   *100% Limit Override:* If `M3_GOVERNOR_LIMIT_THRESHOLD = 100`, no stepbacks are ever applied to interactive processes.

---

## 🚦 2. Workload Governor Operational Modes

| Mode | System Load Range | Interactive Processes pacing | Background Tasks pacing |
| :--- | :--- | :--- | :--- |
| **1. Baseline / Normal** | $< \text{Initial (85\%)}$ | **Unthrottled** ($0\text{s}$ delay) | **Continuous:** $100\text{ms}$ delay<br>**Tapered:** $5.0\text{s}$ delay (if user query was $<60\text{s}$ ago) |
| **2. Throttled** | $\text{Initial (85\%)} \le \text{Load} < \text{Limit (95\%)}$ | **Unthrottled** ($0\text{s}$ delay) | **Step Back:** **$5\text{s}$ to $10\text{s}$ delay** (default $10\text{s}$) between atomic work units to allow host CPU/GPU to breathe. |
| **3. Critical** | $\ge \text{Limit (95\%)}$ | **Step Back:** **$30\text{s}$ to $60\text{s}$ delay** (default $30\text{s}$) between interactive tool runs (cools system/GPU). | **HALTED** ($100\%$ suspended). |
| **4. Max Limit Override** | `LIMIT_THRESHOLD = 100` | **Unthrottled** ($0\text{s}$ delay) even at $99\%$ CPU load. | **HALTED** ($100\%$ suspended) if load is $\ge 85\%$. |

---

## 🛠️ 3. Technical Implementation Design

### 1. Volatile Interaction & Telemetry Registry (`bin/m3_sdk.py`)
```python
import os
import time

_LAST_USER_INTERACTION = 0.0

# User-selectable configurations
INITIAL_LIMIT = min(99, max(10, int(os.environ.get("M3_GOVERNOR_INITIAL_THRESHOLD", "85"))))
LIMIT_THRESHOLD = min(100, max(20, int(os.environ.get("M3_GOVERNOR_LIMIT_THRESHOLD", "95"))))

# Enforce sanity constraint: initial < limit
if INITIAL_LIMIT >= LIMIT_THRESHOLD and LIMIT_THRESHOLD != 100:
    INITIAL_LIMIT = LIMIT_THRESHOLD - 5

def register_user_interaction():
    global _LAST_USER_INTERACTION
    _LAST_USER_INTERACTION = time.time()

def get_governor_pacing(telemetry: dict) -> dict:
    """Return pacing delay configurations for background and interactive pipelines."""
    load = max(telemetry.get("cpu_total", 0), telemetry.get("ram_total", 0), telemetry.get("gpu_total", 0))
    elapsed = time.time() - _LAST_USER_INTERACTION
    
    # 1. Critical Mode (Overall load >= LIMIT_THRESHOLD)
    if LIMIT_THRESHOLD != 100 and load >= LIMIT_THRESHOLD:
        return {"background": "HALTED", "interactive_delay": 30.0} # 30s-60s delay
        
    # 2. Throttled Mode (Overall load >= INITIAL_LIMIT but < LIMIT_THRESHOLD)
    if load >= INITIAL_LIMIT:
        return {"background": "THROTTLED", "background_delay": 10.0, "interactive_delay": 0.0} # 5s-10s delay
        
    # 3. Normal Mode
    if elapsed < 30.0:
        return {"background": "HALTED", "interactive_delay": 0.0}
    elif elapsed < 60.0:
        return {"background": "TAPERED", "background_delay": 5.0, "interactive_delay": 0.0}
    return {"background": "CONTINUOUS", "background_delay": 0.1, "interactive_delay": 0.0}
```

### 2. Cooperative Background Worker Loop
```python
async def background_worker_loop(cancellation_event: asyncio.Event):
    while not cancellation_event.is_set():
        telemetry = get_system_telemetry()
        pacing = get_governor_pacing(telemetry)
        
        if pacing["background"] == "HALTED":
            await asyncio.sleep(5.0) # Check back later
            continue
            
        # Check standard M3 idle-mode gates
        if telemetry["thermal"] in ("Serious", "Critical"):
            logger.debug("Thermal load serious. Pausing background task.")
            await asyncio.sleep(10.0)
            continue
            
        # Execute exactly ONE atomic chunk of work
        work_done = await execute_atomic_work_unit()
        
        if not work_done:
            await asyncio.sleep(10.0) # Queue empty
            continue
            
        # Apply pacing throttle
        delay = pacing.get("background_delay", 0.1)
        await asyncio.sleep(delay)
```

### 3. Single-Process Interactive Loop Pacing
Any interactive endpoint (such as `memory_write` or `memory_search`) runs through a unified entry check before executing:
```python
async def pre_execute_interactive_check():
    register_user_interaction()
    
    telemetry = get_system_telemetry()
    pacing = get_governor_pacing(telemetry)
    
    delay = pacing.get("interactive_delay", 0.0)
    if delay > 0.0:
        logger.warning(
            f"Host load critical. Throttling interactive task by {delay}s "
            "to prevent system freeze."
        )
        await asyncio.sleep(delay)
```

---

## 🔒 4. Master Plan Checklist Additions

We integrate these user-selectable tuning controls directly into the **M3-v3 Master Implementation Plan**:

### Milestone 1: Path Decoupling, SDK Realignment & Hardened Startup
*   [ ] Add `M3_GOVERNOR_INITIAL_THRESHOLD` and `M3_GOVERNOR_LIMIT_THRESHOLD` environment parsing logic to `bin/m3_sdk.py`.
*   [ ] Implement `get_governor_pacing()` and integrate `pre_execute_interactive_check()` into the MCP catalog execution bridge.
*   [ ] Unify telemetry checks into the SDK (`M3Context.get_system_telemetry()`).
