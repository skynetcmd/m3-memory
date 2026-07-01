import logging
import os
import subprocess
import sys

logger = logging.getLogger("thermal_utils")

# get_thermal_status() runs on a WARM path — the cognitive loop polls telemetry
# every cycle (and the governor on every load check). On Windows the powershell
# and wmic probes are console-subsystem processes that FLASH a window and steal
# focus on each poll unless CREATE_NO_WINDOW is set. Route every spawn through the
# shared no_window helper (no-op off Windows). Without it, background thermal
# polling visibly flashes windows during normal operation on every Windows host.
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _task_runtime import no_window_kwargs
except Exception:  # pragma: no cover - fallback if _task_runtime unavailable
    def no_window_kwargs() -> dict:
        return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

def get_thermal_status() -> str:
    """
    Returns the thermal/pressure status of the current system.
    Returns: Nominal, Fair, Serious, or Critical.
    """
    # sys.platform, not platform.system(): the latter can hang on a WMI query
    # on Python 3.14 / Windows, and thermal checks run on a warm path.
    system = {"darwin": "Darwin", "win32": "Windows"}.get(sys.platform, "Linux")

    if system == "Darwin":
        # macOS: Use 'sysctl' for thermal pressure
        try:
            res = subprocess.run(
                ["sysctl", "-n", "kern.thermal_pressure"],
                capture_output=True, text=True, timeout=5, **no_window_kwargs(),
            )
            if res.returncode == 0:
                val = int(res.stdout.strip())
                # 0=Nominal, 1=Fair, 2=Serious, 3=Critical
                mapping = {0: "Nominal", 1: "Fair", 2: "Serious", 3: "Critical"}
                return mapping.get(val, "Nominal")
        except (subprocess.TimeoutExpired, ValueError, OSError) as e:
            logger.debug(f"Thermal check failed on Darwin: {e}")

    elif system == "Windows":
        # Primary: PowerShell Get-CimInstance (works on modern Windows 11 22H2+)
        try:
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance -Namespace root/WMI -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop).CurrentTemperature"],
                capture_output=True, text=True, timeout=10, **no_window_kwargs(),
            )
            if res.returncode == 0 and res.stdout.strip():
                raw_temp = int(res.stdout.strip().splitlines()[0])
                temp_c = (raw_temp / 10.0) - 273.15
                if temp_c > 90: return "Critical"
                if temp_c > 80: return "Serious"
                if temp_c > 70: return "Fair"
                return "Nominal"
        except (subprocess.TimeoutExpired, ValueError, OSError) as e:
            logger.debug(f"PowerShell thermal check failed: {e}")

        # Fallback: legacy wmic (pre-22H2)
        try:
            res = subprocess.run(
                ["wmic", "/namespace:\\\\root\\wmi", "path",
                 "MSAcpi_ThermalZoneTemperature", "get", "CurrentTemperature"],
                capture_output=True, text=True, timeout=5, **no_window_kwargs(),
            )
            if res.returncode == 0 and "CurrentTemperature" in res.stdout:
                lines = res.stdout.strip().split("\n")
                if len(lines) > 1:
                    raw_temp = int(lines[1].strip())
                    # WMI returns deci-Kelvin (K * 10)
                    temp_c = (raw_temp / 10.0) - 273.15
                    if temp_c > 90: return "Critical"
                    if temp_c > 80: return "Serious"
                    if temp_c > 70: return "Fair"
                    return "Nominal"
        except (subprocess.TimeoutExpired, ValueError, OSError) as e:
            logger.debug(f"WMIC thermal check failed: {e}")

    elif system == "Linux":
        # Linux: Read thermal zones from sysfs
        try:
            for zone in range(10):
                path = f"/sys/class/thermal/thermal_zone{zone}/temp"
                if os.path.exists(path):
                    with open(path, "r") as f:
                        temp = int(f.read().strip()) / 1000.0
                        if temp > 95: return "Critical"
                        if temp > 85: return "Serious"
                        if temp > 75: return "Fair"
        except (ValueError, OSError) as e:
            logger.debug(f"Thermal check failed on Linux: {e}")

    return "Nominal"
