#!/usr/bin/env python3
r"""Toggle the M3_AUTO_ENRICH env var on/off, cross-platform.

On every invocation, flips the persistent value: ON -> OFF or OFF -> ON.
After flipping, prints the exact command to revert (so a script log captures
both states).

Persistence:
  - Windows: User scope via `setx` (HKCU\Environment). Persists across sessions.
              Note: existing processes do NOT pick up the new value; only new
              processes inherit it.
  - macOS/Linux: a single-line `export M3_AUTO_ENRICH=...` in
              ~/.config/m3-memory/env, sourced by adding one line to your
              shell rc the first time. Subsequent toggles only rewrite the env
              file; shell rc is left alone.

Detection of current state reads the persistent store, NOT os.environ — that
way the result is consistent regardless of which shell launched this script.
"""
from __future__ import annotations
import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

VAR = "M3_AUTO_ENRICH"
TRUE_VALUES = {"1", "true", "yes", "on"}


def _is_windows() -> bool:
    return platform.system() == "Windows"


# --- Windows persistence ---------------------------------------------------

def _read_windows() -> str | None:
    """Read the User-scope env var via the registry."""
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            val, _ = winreg.QueryValueEx(k, VAR)
            return str(val)
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _write_windows(value: str | None) -> None:
    """Set or clear the User-scope env var. setx for set; reg delete for clear.

    We use external commands rather than winreg writes so the env var change
    is broadcast (WM_SETTINGCHANGE) — which setx does and direct registry
    writes do not.
    """
    if value is None:
        subprocess.run(
            ["reg", "delete", "HKCU\\Environment", "/F", "/V", VAR],
            capture_output=True,
            check=False,
        )
        return
    subprocess.run(["setx", VAR, value], capture_output=True, check=True)


# --- Unix persistence ------------------------------------------------------

def _env_file_path() -> Path:
    return Path.home() / ".config" / "m3-memory" / "env"


def _read_unix() -> str | None:
    p = _env_file_path()
    if not p.exists():
        return None
    pattern = re.compile(rf"^\s*export\s+{VAR}\s*=\s*(.+?)\s*$")
    for line in p.read_text(encoding="utf-8").splitlines():
        m = pattern.match(line)
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return None


def _write_unix(value: str | None) -> None:
    p = _env_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    pattern = re.compile(rf"^\s*export\s+{VAR}\s*=")
    cleaned = [ln for ln in existing if not pattern.match(ln)]
    if value is not None:
        cleaned.append(f'export {VAR}="{value}"')
    body = "\n".join(cleaned).rstrip() + ("\n" if cleaned else "")
    p.write_text(body, encoding="utf-8")


def _shell_rc_hint() -> str:
    """Suggest the right shell rc snippet for sourcing the env file."""
    return (
        f'echo \'[ -f "$HOME/.config/m3-memory/env" ] && '
        f'. "$HOME/.config/m3-memory/env"\' >> "$HOME/.zshrc"'
    )


# --- Cross-platform read / write -------------------------------------------

def read_persistent() -> str | None:
    return _read_windows() if _is_windows() else _read_unix()


def write_persistent(value: str | None) -> None:
    if _is_windows():
        _write_windows(value)
    else:
        _write_unix(value)


def is_truthy(v: str | None) -> bool:
    return v is not None and v.strip().lower() in TRUE_VALUES


# --- Revert command --------------------------------------------------------

def revert_command(current_state_after_flip: bool) -> str:
    """Print the command to flip back to the previous state.

    If we just turned it ON  -> revert command turns it OFF
    If we just turned it OFF -> revert command turns it ON
    On both platforms the user-facing command is the same: re-run this script.
    """
    script = Path(__file__).resolve()
    py = "python" if not _is_windows() else r".venv\Scripts\python.exe"
    if _is_windows():
        return f'{py} "{script}"'
    return f'{shutil.which("python3") or "python3"} {script}'


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Toggle M3_AUTO_ENRICH on or off (cross-platform).",
        epilog="Without flags: flips current state. Use --status to read only.",
    )
    parser.add_argument("--status", action="store_true",
                        help="Print current state and exit (no flip).")
    parser.add_argument("--on", action="store_true",
                        help="Force on regardless of current state.")
    parser.add_argument("--off", action="store_true",
                        help="Force off regardless of current state.")
    args = parser.parse_args()

    if args.on and args.off:
        print("error: --on and --off are mutually exclusive", file=sys.stderr)
        return 2

    current = read_persistent()
    currently_on = is_truthy(current)

    print(f"M3_AUTO_ENRICH (persistent): {current!r}  -> {'ON' if currently_on else 'OFF'}")

    if args.status:
        return 0

    if args.on:
        target_on = True
    elif args.off:
        target_on = False
    else:
        target_on = not currently_on

    if target_on == currently_on:
        print(f"already {'ON' if currently_on else 'OFF'} — no change.")
        return 0

    write_persistent("1" if target_on else None)
    print(f"flipped: {'OFF -> ON' if target_on else 'ON -> OFF'}")
    print(f"to revert: {revert_command(target_on)}")

    if not _is_windows() and target_on:
        env_file = _env_file_path()
        if env_file.exists():
            print(f"\nnote: persistent value lives in {env_file}")
            print("to make it visible in new shells, add this to your shell rc once:")
            print(f"  {_shell_rc_hint()}")

    if _is_windows():
        print("\nnote: existing shells/processes do NOT pick up the new value.")
        print("open a new shell (or restart MCP servers) for it to take effect.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
