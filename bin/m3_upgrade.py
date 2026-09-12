#!/usr/bin/env python3
"""Upgrade m3-memory end to end, using the right command for how it was installed.

Run this INSTEAD of remembering a sequence:

    python bin/m3_upgrade.py            # detect, upgrade, finalize, verify
    python bin/m3_upgrade.py --dry-run  # print the plan, change nothing

Why a standalone script rather than an ``m3 upgrade`` subcommand: the upgrade
replaces the very package a subcommand would be running from. On Windows that is
a file-locking failure, not a theoretical one. This file deliberately does NOT
import ``m3_memory``, so the package can be swapped underneath it safely. It
shells out to the ``m3`` executable for the steps that need m3 itself, each in a
fresh process that loads whichever payload is current at that moment.

The steps mirror what the CLI's own help already tells you to do:
  1. ``m3 stop``   -- release DB-writer file locks. ``m3 stop --help`` says to do
                      this before upgrading on Windows.
  2. upgrade       -- pipx / pip / pip --user, chosen by DETECTION, never assumed.
  3. ``m3 setup``  -- rewire agent configs, migrate schemas, restart services.
  4. ``m3 doctor`` -- verify, and exit nonzero if it is unhappy.

There is no ``m3 upgrade`` subcommand. Guessing one (or guessing ``pipx`` for a
pip install) is the failure this script exists to prevent: ``pipx upgrade``
against a pip install exits 0 having upgraded NOTHING, which reads as success.
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess  # nosec B404 - orchestrates known package managers, never a shell
import sys

# How m3_memory got onto this machine. The upgrade command differs per method.
PIPX = "pipx"
PIP = "pip"
PIP_USER = "pip-user"
PLUGIN = "plugin"
UNKNOWN = "unknown"


def find_m3_package(exe: str) -> pathlib.Path | None:
    """Locate the INSTALLED m3_memory package without importing it.

    Importing would bind this process to the payload we are about to replace, so
    we ask ``m3`` itself where it lives -- it is the only authoritative answer.

    Two traps this deliberately avoids, both hit on a real machine:

    * **Do not guess a "sibling python".** ``~/.local/bin`` can hold unrelated
      interpreters (``python3.11.exe`` beside a pipx shim). Asking one of those
      to import ``m3_memory`` can succeed against a DEV CHECKOUT on the ambient
      path and report a confident, wrong location.
    * **Resolve symlinks on the real executable.** The console script on PATH is
      often a shim pointing into the venv that owns the package; the shim's own
      directory tells you nothing.
    """
    # 1. Authoritative: ask the m3 binary. It runs in its own interpreter, which
    #    is by definition the one the package is installed into.
    try:
        out = subprocess.run(  # nosec B603 - argv list, executable resolved from PATH
            [exe, "--version"], capture_output=True, text=True, timeout=90
        )
        if out.returncode == 0:
            resolved = pathlib.Path(exe).resolve()
            # <venv>/Scripts/m3.exe -> <venv>; <venv>/bin/m3 -> <venv>
            venv = resolved.parent.parent
            for sp in (
                venv / "Lib" / "site-packages" / "m3_memory",          # Windows
                *(venv.glob("lib/python*/site-packages/m3_memory")),   # POSIX
            ):
                if sp.exists():
                    return sp
    except (OSError, subprocess.TimeoutExpired):
        pass

    # 2. Fallback: an interpreter sitting INSIDE the same venv as the executable
    #    (not merely next to it on PATH), asked with an isolated environment so
    #    an ambient PYTHONPATH cannot substitute a checkout for the install.
    resolved_dir = pathlib.Path(exe).resolve().parent
    for cand in ("python.exe", "python3.exe", "python", "python3"):
        py = resolved_dir / cand
        if not py.exists():
            continue
        try:
            out = subprocess.run(  # nosec B603 - argv list, interpreter inside the venv
                [
                    str(py),
                    "-E",  # ignore PYTHONPATH/PYTHONHOME: no ambient substitution
                    "-c",
                    "import m3_memory,pathlib;"
                    "print(pathlib.Path(m3_memory.__file__).parent)",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out.returncode == 0 and out.stdout.strip():
            return pathlib.Path(out.stdout.strip())
    return None


def detect_install_method(pkg_dir: pathlib.Path | None) -> tuple[str, str]:
    """Return ``(method, evidence)``.

    The evidence string is printed: a detection the user cannot audit is one
    they cannot correct.
    """
    if pkg_dir is None:
        return UNKNOWN, "could not locate an installed m3_memory package"

    # Resolve BOTH sides of every path comparison below. A resolved path
    # compared against an unresolved one silently fails to match wherever
    # symlinks are in play (notably macOS home directories).
    try:
        pkg_dir = pkg_dir.resolve()
    except (OSError, RuntimeError):
        pass
    normalized = str(pkg_dir).replace("\\", "/")

    # Plugin caches are managed by the HOST (Claude Code / Antigravity). Running
    # a package manager against one fights whatever installed it.
    for marker in ("/.claude/plugins/", "/.antigravity/plugins/", "/plugins/cache/"):
        if marker in normalized:
            return PLUGIN, f"package lives in a host plugin cache: {pkg_dir}"

    # pipx venvs carry a metadata file at the venv root; site-packages sits a few
    # levels below it (Lib/site-packages on Windows, lib/pythonX.Y/... on POSIX).
    for parent in list(pkg_dir.parents)[:5]:
        if (parent / "pipx_metadata.json").exists():
            return PIPX, f"pipx_metadata.json found at {parent}"

    # A --user install lands under the per-user site directory.
    #
    # RESOLVE both sides before comparing. `pkg_dir` arrives already resolved,
    # but `site.getusersitepackages()` does not -- and on macOS the user's home
    # is routinely reached through a symlink (/Users/... via /System/Volumes/
    # Data/Users/...), so an unresolved prefix fails `startswith` against a
    # resolved path and a --user install is misdetected as a plain pip install.
    # Reported by antigravity-agent reviewing from macOS, the platform this was
    # never tested on.
    try:
        import site

        user_site = site.getusersitepackages()
        if user_site:
            try:
                resolved_user_site = str(pathlib.Path(user_site).resolve())
            except (OSError, RuntimeError):
                resolved_user_site = str(user_site)
            if normalized.startswith(resolved_user_site.replace("\\", "/")):
                return PIP_USER, f"package is under the user site directory: {user_site}"
    except Exception:  # noqa: BLE001 - detection must never crash the upgrade
        pass

    return PIP, f"package is in a standard site-packages: {pkg_dir}"


def upgrade_command(method: str, python: str = sys.executable) -> list[str] | None:
    """The package-level upgrade command for a method, or None when we must not
    run one (plugin-managed or undetermined installs)."""
    if method == PIPX:
        return [shutil.which("pipx") or "pipx", "upgrade", "m3-memory"]
    if method == PIP:
        return [python, "-m", "pip", "install", "--upgrade", "m3-memory"]
    if method == PIP_USER:
        return [python, "-m", "pip", "install", "--upgrade", "--user", "m3-memory"]
    return None


def run(cmd: list[str], *, dry: bool, timeout: int = 900) -> int:
    printable = " ".join(cmd)
    if dry:
        print(f"      would run: {printable}")
        return 0
    print(f"      $ {printable}")
    try:
        return subprocess.run(cmd, timeout=timeout).returncode  # nosec B603 - argv list, no shell
    except FileNotFoundError:
        print(f"      !! not found: {cmd[0]}")
        return 127
    except subprocess.TimeoutExpired:
        print(f"      !! timed out after {timeout}s")
        return 124


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Upgrade m3-memory using the right command for this install."
    )
    ap.add_argument("--dry-run", action="store_true", help="Show the plan; change nothing.")
    ap.add_argument("--skip-stop", action="store_true", help="Do not stop DB writers first.")
    ap.add_argument("--yes", action="store_true", help="Do not prompt (for scripted use).")
    args = ap.parse_args(argv)

    m3 = shutil.which("m3") or shutil.which("mcp-memory")
    if not m3:
        print("m3 is not on PATH. Install it first, e.g. `pipx install m3-memory`.")
        return 1

    pkg = find_m3_package(m3)
    method, evidence = detect_install_method(pkg)
    print(f"[detect] install method: {method}")
    print(f"         evidence      : {evidence}")

    if method == PLUGIN:
        print(
            "\nThis m3 was installed as an agent PLUGIN. Upgrading it with pip or pipx\n"
            "would fight the host that manages it. Use the host's own flow instead\n"
            "(for example `/plugin` in Claude Code), then run `m3 doctor`."
        )
        return 2
    if method == UNKNOWN:
        print(
            "\nCould not determine how m3 was installed, so there is no safe upgrade\n"
            "command to run. Upgrade with whichever tool you installed with, then run\n"
            "`m3 setup` and `m3 doctor`."
        )
        return 2

    # Upgrade with the interpreter that OWNS the package, not whichever one is
    # running this script. `python -m pip install -U` from the wrong interpreter
    # installs into the wrong environment and reports success.
    owner_python = sys.executable
    if pkg is not None:
        for parent in list(pkg.parents)[:5]:
            for cand in ("Scripts/python.exe", "bin/python3", "bin/python"):
                candidate = parent / cand
                if candidate.exists():
                    owner_python = str(candidate)
                    break
            if owner_python != sys.executable:
                break

    up = upgrade_command(method, owner_python)
    if up is None:  # defensive: PLUGIN/UNKNOWN already returned above
        print(f"\nNo upgrade command is defined for method {method!r}.")
        return 2

    if not args.yes and not args.dry_run:
        print("\nPlan:")
        print("  1. m3 stop            (release DB-writer file locks)")
        print(f"  2. {' '.join(up)}")
        print("  3. m3 setup           (agent configs, schema migrations, services)")
        print("  4. m3 doctor          (verify)")
        try:
            answer = input("\nProceed? [y/N] ").strip().lower()
        except EOFError:
            # No TTY (CI / piped input). Refuse rather than act unattended by
            # accident -- the same trap that stranded install_os.py children.
            print("\nNo TTY to confirm on; re-run with --yes to proceed unattended.")
            return 1
        if answer not in ("y", "yes"):
            print("aborted.")
            return 1

    dry = args.dry_run

    if not args.skip_stop:
        print("\n[1/4] stopping m3 DB writers ...")
        # Non-fatal: nothing may be running, and that is a fine state to upgrade from.
        run([m3, "stop"], dry=dry, timeout=180)

    print("\n[2/4] upgrading the package ...")
    rc = run(up, dry=dry)
    if rc != 0:
        print(
            f"\nUpgrade command failed (exit {rc}). Nothing further was run; m3 is\n"
            "still on its previous version."
        )
        return rc

    # Re-resolve: step 2 may have replaced the executable we started with.
    m3 = shutil.which("m3") or shutil.which("mcp-memory") or m3

    # --force-quiesce: step 1's `m3 stop` is best-effort and non-fatal, so a
    # writer that did not exit would leave `setup --non-interactive` waiting on
    # a quiesce that never completes -- an unattended upgrade that hangs instead
    # of finishing. Reported by antigravity-agent in review of this script.
    print("\n[3/4] finalizing (agent configs, migrations, services) ...")
    rc = run([m3, "setup", "--non-interactive", "--force-quiesce"], dry=dry)
    if rc != 0:
        print(
            f"\n`m3 setup` failed (exit {rc}). The package IS upgraded; re-run\n"
            "`m3 setup` by hand to finish wiring it up."
        )
        return rc

    print("\n[4/4] verifying ...")
    rc = run([m3, "doctor"], dry=dry, timeout=300)
    if rc != 0:
        print(
            f"\n`m3 doctor` reported problems (exit {rc}). The upgrade completed;\n"
            "read the doctor output above before relying on this install."
        )
        return rc

    print("\nDry run complete; nothing was changed." if dry else "\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
