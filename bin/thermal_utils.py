import platform
import subprocess
import os
import logging

logger = logging.getLogger("thermal_utils")

def get_thermal_status() -> str:
    """
    Returns the thermal/pressure status of the current system.
    Returns: Nominal, Fair, Serious, or Critical.
    """
    system = platform.system()

    if system == "Darwin":
        # macOS: Use 'sysctl' for thermal pressure
        try:
            res = subprocess.run(
                ["sysctl", "-n", "kern.thermal_pressure"],
                capture_output=True, text=True, timeout=5,
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
                capture_output=True, text=True, timeout=10,
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
                capture_output=True, text=True, timeout=5,
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
