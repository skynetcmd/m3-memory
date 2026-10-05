"""Touch ID for sudo on macOS: detect, and enable with the user's consent.

macOS 14+ reads /etc/pam.d/sudo_local (which updates leave alone); one line,
`auth sufficient pam_tid.so`, lets sudo accept a fingerprint instead of the
password. This is a system security setting, so m3 only ever ADDS that line,
only when asked, and checks the file afterwards. A fingerprint cannot reach an
SSH session, so the offer is made only to someone at the Mac.
"""
from __future__ import annotations

import os
import subprocess  # nosec B404 - fixed argv lists, no shell
import sys

SUDO_LOCAL = "/etc/pam.d/sudo_local"
TEMPLATE = "/etc/pam.d/sudo_local.template"
PAM_TID = "/usr/lib/pam/pam_tid.so.2"
LINE = "auth       sufficient     pam_tid.so"


def _is_mac() -> bool:
    return sys.platform == "darwin"


def _active_tid_line(text: str) -> bool:
    return any(ln.split()[:3] == ["auth", "sufficient", "pam_tid.so"]
               for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#"))


def _sensor_present() -> "bool | None":
    """True/False from `bioutil -r -s`; None when it cannot be asked."""
    try:
        out = subprocess.run(["bioutil", "-r", "-s"], capture_output=True, text=True,
                             timeout=10).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return None
    for ln in out.splitlines():
        if "Biometrics functionality" in ln:
            return ln.strip().endswith("1")
    return None


def status() -> str:
    """enabled | available | no-sensor | unsupported."""
    if not _is_mac() or not os.path.exists(PAM_TID):
        return "unsupported"
    try:
        with open(SUDO_LOCAL, encoding="utf-8") as fh:
            if _active_tid_line(fh.read()):
                return "enabled"
    except FileNotFoundError:
        if not os.path.exists(TEMPLATE):
            return "unsupported"  # macOS < 14: sudo_local is not read
    except OSError:
        pass
    return "no-sensor" if _sensor_present() is False else "available"


def at_the_mac() -> bool:
    """A fingerprint (and the sudo prompt) need a person at this Mac, not SSH."""
    return not (os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))


def enable() -> "tuple[bool, str]":
    """Append the pam_tid line to sudo_local via sudo (asks for the password
    once). Returns (enabled, detail); success is read back from the file."""
    try:
        r = subprocess.run(["sudo", "tee", "-a", SUDO_LOCAL], input=LINE + "\n",
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"sudo could not run ({e})"
    if r.returncode != 0:
        return False, (r.stderr or "sudo declined").strip()
    if status() != "enabled":
        return False, f"{SUDO_LOCAL} was written but holds no active pam_tid line"
    return True, f"added `{LINE.split()[0]} sufficient pam_tid.so` to {SUDO_LOCAL}"


UNDO = f"sudo sed -i '' '/pam_tid.so/d' {SUDO_LOCAL}"
