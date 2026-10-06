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
  3. ``m3 stop``   -- AGAIN, now that the package is replaced. Anything still up
                      is running OLD code, and step 4 only restarts what it finds
                      STOPPED, so a survivor would be reported "running" and keep
                      serving stale code. The cognitive loop is the one that
                      matters: stale code there writes stale derived knowledge.
  4. ``m3 setup``  -- rewire agent configs, migrate schemas, restart services --
                      which brings the daemons back on the NEW version.
  5. ``m3 doctor --fix --fix-hooks``
                   -- verify AND repair, exiting nonzero if still unhappy. An
                      upgrade is the one moment repair is unambiguously wanted:
                      the user asked for a new version and is waiting. A bare
                      verify left self-repairable state broken behind a warning
                      (embed-server exec bit, dead agent MCP configs, a wedged
                      dashboard), and --fix-hooks matters because hook entries
                      point at the payload step 2 just REPLACED and nothing else
                      in the upgrade rewires them.

``m3 upgrade`` launches this script. Guessing ``pipx`` for a pip install is the
failure it exists to prevent: ``pipx upgrade`` against a pip install exits 0
having upgraded NOTHING, which reads as success.

On Windows, any process running from one of the venv's launcher .exe files
(``m3 upgrade``'s own launcher when ``~/.local/bin/m3.exe`` is a symlink, an
agent's m3 MCP server, a hook) blocks pip from replacing it, and pip then leaves
the package uninstalled. ``locked_launchers`` checks for that before anything
is stopped or replaced.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess  # nosec B404 - orchestrates known package managers, never a shell
import sys
import time

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



def cognitive_loop_installed(exe: str) -> "bool | None":
    """Is the cognitive loop installed on this host RIGHT NOW?

    Needed because `m3 setup --non-interactive` defaults `plan.cognitive_loop`
    to False, and `_step_verify_daemons` only adds a role to `expected` when its
    plan field is TRUE. So an upgrade that passes neither flag STOPS the loop
    (step 1, and again at step 3) and then never restarts or verifies it — the
    loop stays down until its watchdog fires. Passing the flag that matches the
    CURRENT state preserves the operator's choice and gets the loop back on the
    NEW version, which is the whole point of restarting it.

    Delegates to ``governor_migration``, the single owner of per-OS service/task
    detection (systemd --user unit, launchd plist, schtasks) — a second copy of
    that here would drift (§10a). Runs it in a SUBPROCESS against the CURRENT
    payload, BEFORE the upgrade replaces it, and never imports it into this
    process: this script must keep working while the package it would import is
    being deleted.

    Returns True / False, or None when it cannot be determined — in which case
    the caller passes neither flag and SAYS so, rather than guessing.
    """
    pkg = find_m3_package(exe)
    if pkg is None:
        return None
    # Inline rather than governor_migration.cognitive_loop_installed(): this runs
    # against the payload being REPLACED, which may predate that function.
    for bd in (pkg / "bin", pkg.parent / "bin"):
        if (bd / "governor_migration.py").is_file():
            code = (
                "import sys; sys.path.insert(0, %r);"
                "import governor_migration as gm;"
                "d = gm.detect_scheduled_tasks();"
                "print('YES' if 'AgentOS_CognitiveLoop' in "
                "(d.get('not_migratable_present') or []) else 'NO')"
            ) % str(bd)
            try:
                r = subprocess.run(  # nosec B603 - argv list, no shell
                    [sys.executable, "-c", code],
                    capture_output=True, text=True, timeout=60,
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            out = (r.stdout or "").strip().splitlines()
            if r.returncode == 0 and out and out[-1] in ("YES", "NO"):
                return out[-1] == "YES"
            return None
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


def pipx_source(pkg_dir: pathlib.Path | None) -> str | None:
    """What `pipx upgrade` reinstalls from: pipx_metadata.json's package spec."""
    if pkg_dir is None:
        return None
    for parent in list(pkg_dir.parents)[:5]:
        meta = parent / "pipx_metadata.json"
        if meta.exists():
            try:
                with open(meta, encoding="utf-8") as f:
                    spec = (json.load(f).get("main_package") or {}).get("package_or_url")
            except (OSError, ValueError):
                return None
            return str(spec) if spec else None
    return None


def source_is_pypi(spec: str) -> bool:
    """True for an unpinned PyPI name; False for a path, URL, VCS or pin."""
    return spec.strip().lower() in ("m3-memory", "m3_memory")


def source_is_missing_path(spec: str) -> bool:
    """True when the spec names a local file or directory that is gone."""
    s = spec.strip()
    if "://" in s or s.startswith("git+"):
        return False
    looks_local = (os.path.isabs(s) or s.startswith((".", "~"))
                   or os.sep in s or "/" in s or s.endswith(".whl"))
    return looks_local and not os.path.exists(os.path.expanduser(s))


def upgrade_command(method: str, python: str = sys.executable,
                    from_pypi: bool = False) -> list[str] | None:
    """The package-level upgrade command for a method, or None when we must not
    run one (plugin-managed or undetermined installs).

    ``from_pypi`` (pipx only): reinstall from the PyPI name, which also moves the
    source pipx records from a wheel/path to PyPI. pip installs already resolve
    against the index, so the flag changes nothing for them.
    """
    if method == PIPX:
        pipx = shutil.which("pipx") or "pipx"
        if from_pypi:
            return [pipx, "install", "--force", "m3-memory"]
        return [pipx, "upgrade", "m3-memory"]
    if method == PIP:
        return [python, "-m", "pip", "install", "--upgrade", "m3-memory"]
    if method == PIP_USER:
        return [python, "-m", "pip", "install", "--upgrade", "--user", "m3-memory"]
    return None


def _entry_point_names(pkg: pathlib.Path | None) -> list[str]:
    """Console/GUI script names m3-memory installs, read from its dist-info."""
    names: list[str] = []
    if pkg is not None:
        for ep in sorted(pkg.parent.glob("m3_memory-*.dist-info/entry_points.txt")):
            section = ""
            for line in ep.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith("["):
                    section = line
                elif "=" in line and section in ("[console_scripts]", "[gui_scripts]"):
                    names.append(line.split("=", 1)[0].strip())
    return names or ["m3", "m3-team", "mcp-memory"]


def installed_version(pkg: pathlib.Path | None) -> str | None:
    """The installed m3-memory version, from its dist-info directory name."""
    if pkg is None:
        return None
    for d in sorted(pkg.parent.glob("m3_memory-*.dist-info")):
        return d.name[len("m3_memory-"):-len(".dist-info")]
    return None


def locked_launchers(scripts_dir: pathlib.Path, names: list[str]) -> list[pathlib.Path]:
    """Windows only: the m3 launchers that cannot be replaced right now.

    A process running FROM one of these .exe files (an agent's m3 MCP server, a
    hook, or `m3 upgrade`'s own launcher when ~/.local/bin/m3.exe is a symlink
    into the venv) holds it against rename. pip removes the package first and
    then fails on the held file, leaving m3 uninstalled. Probing with the same
    rename pip performs measures exactly that condition.
    """
    if os.name != "nt":
        return []
    locked = []
    for name in names:
        exe = scripts_dir / f"{name}.exe"
        if not exe.is_file():
            continue
        probe = exe.with_name(exe.name + ".m3-upgrade-probe")
        try:
            os.rename(exe, probe)
        except OSError:
            locked.append(exe)
            continue
        try:
            os.rename(probe, exe)
        except OSError as e:
            raise SystemExit(f"could not restore {exe} after a lock probe ({e}); "
                             f"rename {probe.name} back to {exe.name} by hand")
    return locked


def describe_holders(locked: list[pathlib.Path]) -> list[str]:
    """One line per process holding a locked launcher (best effort: psutil)."""
    try:
        import psutil
    except ImportError:
        return []
    want = {os.path.normcase(str(p)) for p in locked}
    try:
        ancestors = {p.pid for p in psutil.Process().parents()}
    except psutil.Error:
        ancestors = set()
    lines = []
    for proc in psutil.process_iter(["pid", "exe"]):
        exe = proc.info.get("exe") or ""
        if os.path.normcase(exe) not in want:
            continue
        if proc.info["pid"] in ancestors:
            who = "this command's own launcher"
        else:
            try:
                who = f"started by {proc.parent().name()}"
            except (psutil.Error, AttributeError):
                who = "parent unknown"
        lines.append(f"    pid {proc.info['pid']:>6}  {pathlib.Path(exe).name}  ({who})")
    return lines


def launcher_holders(locked: list[pathlib.Path]) -> tuple[list[int], list[int]]:
    """(pids of OUR OWN ancestors, pids of OTHER processes) running from a locked
    launcher. Both empty when the holders cannot be identified: nothing is
    handed off or stopped on a guess."""
    try:
        import psutil
    except ImportError:
        return [], []
    want = {os.path.normcase(str(p)) for p in locked}
    try:
        ancestors = {p.pid for p in psutil.Process().parents()}
    except psutil.Error:
        return [], []
    own: list[int] = []
    others: list[int] = []
    for proc in psutil.process_iter(["pid", "exe"]):
        if os.path.normcase(proc.info.get("exe") or "") not in want:
            continue
        (own if proc.info["pid"] in ancestors else others).append(proc.info["pid"])
    return own, others


def stop_holders(pids: list[int], timeout: float = 10.0) -> None:
    """Stop these m3 launcher processes and their children (the real server is
    the launcher's child). Precise pids only, never a name match."""
    import psutil
    procs = []
    for pid in pids:
        try:
            p = psutil.Process(pid)
            procs.extend(p.children(recursive=True))
            procs.append(p)
        except psutil.Error:
            continue
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(procs, timeout=timeout)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    for pid in pids:
        print(f"  stopped m3 in an agent session (pid {pid}); reconnect that agent "
              f"afterwards (Claude Code: /mcp)")


def interactive_console() -> bool:
    """True when a person is at this console.

    On Windows, isatty() is not enough: the NUL device reports itself as a
    character device, so `< NUL` (how scripts and schedulers detach stdin) reads
    as a terminal. GetConsoleMode succeeds only for a real console input handle.
    """
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if os.name != "nt":
        return True
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    handle = kernel32.GetStdHandle(wintypes.DWORD(-10 & 0xFFFFFFFF))  # STD_INPUT_HANDLE
    mode = wintypes.DWORD()
    return bool(kernel32.GetConsoleMode(handle, ctypes.byref(mode)))


def wait_for_exit(pids: list[int], timeout: float = 60.0) -> bool:
    """True once none of ``pids`` is running (or ``timeout`` passed: False)."""
    import psutil
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(psutil.pid_exists(p) for p in pids):
            return True
        time.sleep(0.5)
    return False


def hand_off_to_new_window(owner_python: str, argv: list[str], pids: list[int]) -> str:
    """Continue the upgrade in a new console, run by Python rather than m3.exe.

    The only holder is this command's own launcher, which cannot be released
    while this process runs. The new window waits for it to exit, then upgrades,
    stays open until Enter, and records everything in the returned log file.
    """
    import tempfile
    log = os.path.join(tempfile.gettempdir(),
                       time.strftime("m3-upgrade-%Y%m%d-%H%M%S.log"))
    cmd = [owner_python, str(pathlib.Path(__file__).resolve()), *argv, "--yes",
           "--wait-for-pid", ",".join(str(p) for p in pids), "--pause-at-end",
           "--log", log]
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    subprocess.Popen(cmd, creationflags=flags, close_fds=True)  # nosec B603 - argv list, no shell
    return log


def explain_locked(locked: list[pathlib.Path], owner_python: str,
                   user_flags: "list[str] | None" = None) -> None:
    print("\nCannot upgrade now: Windows keeps these m3 programs locked while they")
    print("run, and replacing them would remove m3 and then fail:")
    for p in locked:
        print(f"    {p}")
    holders = describe_holders(locked)
    if holders:
        print("held by:")
        print("\n".join(holders))
    print("\nNothing was changed. To upgrade:")
    if any("own launcher" in h for h in holders) or not holders:
        print("  - run the upgrade through Python, which holds none of them:")
        unattended = "" if interactive_console() else " --yes"
        # Carry the user's own flags (--from-pypi, --stop-agents) so the printed
        # command does what the refused one was asked to do.
        for flag in (user_flags or []):
            unattended += f" {flag}"
        print(f'      "{owner_python}" "{pathlib.Path(__file__).resolve()}"{unattended}')
    if any("own launcher" not in h for h in holders) or not holders:
        print("  - close the agent sessions using m3 (or end the processes above),")
        print("    then re-run; agents reconnect afterwards (Claude Code: /mcp).")
    if any("own launcher" not in h for h in holders):
        print("  - or let the upgrade stop m3 in those sessions for you:")
        print("      m3 upgrade --stop-agents")


class _Tee:
    """Write to the console and to a log file (the hand-off window's record)."""

    def __init__(self, stream, path: str) -> None:
        self._stream = stream
        self._log = open(path, "a", encoding="utf-8")  # noqa: SIM115 - lives for the run

    def write(self, text: str) -> int:
        self._log.write(text)
        self._log.flush()
        return self._stream.write(text)

    def flush(self) -> None:
        self._stream.flush()
        self._log.flush()


def run(cmd: list[str], *, dry: bool, timeout: int = 900,
        env: dict[str, str] | None = None) -> int:
    printable = " ".join(cmd)
    if dry:
        print(f"      would run: {printable}")
        return 0
    print(f"      $ {printable}")
    try:
        if isinstance(sys.stdout, _Tee):
            # Logging: relay the child's output so it reaches the log as well.
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,  # nosec B603
                                    text=True, encoding="utf-8", errors="replace", env=env)
            deadline = time.monotonic() + timeout
            assert proc.stdout is not None
            for line in proc.stdout:
                print(line, end="")
                if time.monotonic() > deadline:
                    proc.kill()
                    raise subprocess.TimeoutExpired(cmd, timeout)
            return proc.wait()
        return subprocess.run(cmd, timeout=timeout, env=env).returncode  # nosec B603 - argv list, no shell
    except FileNotFoundError:
        print(f"      !! not found: {cmd[0]}")
        return 127
    except subprocess.TimeoutExpired:
        print(f"      !! timed out after {timeout}s")
        return 124


def summary_lines(*, old: str | None, new: str | None, unchanged: bool,
                  agents_stopped: int, failed_step: str, rc: int, log: str) -> list[str]:
    """The end-of-run summary: the lines a user needs after the step output
    has scrolled away."""
    if unchanged:
        version = f"m3 {old}: already installed, nothing was upgraded"
    else:
        version = f"m3 {old or '?'} -> {new or '?'}"
    lines = ["", "Upgrade incomplete." if failed_step else "Done.",
             f"  version : {version}"]
    if agents_stopped:
        lines.append(f"  agents  : m3 was stopped in {agents_stopped} agent session(s); "
                     "reconnect them (Claude Code: /mcp)")
    elif not unchanged:
        # Setup leaves this to us when we call it (its own list is suppressed).
        lines.append("  agents  : restart your agents, or reconnect m3 (Claude Code: /mcp), "
                     "to load the new version")
    if failed_step:
        lines.append(f"  health  : {failed_step} reported problems (exit {rc}); "
                     "see its output above")
    else:
        lines.append("  health  : m3 doctor passed")
    if log:
        lines.append(f"  log     : {log}")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Upgrade m3-memory using the right command for this install."
    )
    ap.add_argument("--dry-run", action="store_true", help="Show the plan; change nothing.")
    ap.add_argument("--skip-stop", action="store_true", help="Do not stop DB writers first.")
    ap.add_argument("--yes", action="store_true", help="Do not prompt (for scripted use).")
    ap.add_argument("--from-pypi", dest="from_pypi", action="store_true",
                    help="pipx installs: reinstall from PyPI, moving the recorded "
                         "source off a local wheel or path.")
    # Internal: set by the Windows hand-off (see hand_off_to_new_window).
    ap.add_argument("--wait-for-pid", dest="wait_for_pid", default="", help=argparse.SUPPRESS)
    ap.add_argument("--pause-at-end", dest="pause_at_end", action="store_true",
                    help=argparse.SUPPRESS)
    ap.add_argument("--log", dest="log", default="", help=argparse.SUPPRESS)
    ap.add_argument("--stop-agents", dest="stop_agents", action="store_true",
                    help="Windows: stop m3 in agent sessions that hold its launcher "
                         "(they reconnect afterwards). Asked interactively otherwise.")
    ap.add_argument("--stop-pids", dest="stop_pids", default="", help=argparse.SUPPRESS)
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = ap.parse_args(raw_argv)
    user_flags = [f for f in ("--from-pypi", "--stop-agents") if f in raw_argv]
    if args.log:
        sys.stdout = _Tee(sys.stdout, args.log)  # type: ignore[assignment]
    # Line-buffer our own output: the steps run child processes that write to the
    # same stream, and block-buffered prints would appear after their output.
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

    m3 = shutil.which("m3") or shutil.which("mcp-memory")
    if not m3:
        print("m3 is not on PATH. Install it first, e.g. `pipx install m3-memory`.")
        return 1

    pkg = find_m3_package(m3)
    method, evidence = detect_install_method(pkg)
    print(f"[detect] install method: {method}")
    print(f"         evidence      : {evidence}")
    spec = pipx_source(pkg) if method == PIPX else None
    if method == PIPX:
        print(f"         source        : {spec or 'unknown (no pipx metadata)'}")
        if args.from_pypi:
            print("         --from-pypi   : reinstalling from PyPI (the source becomes m3-memory)")
        elif spec and not source_is_pypi(spec):
            if source_is_missing_path(spec):
                # pipx upgrade would fail after the services were already
                # stopped; stop here instead, with the way back to PyPI.
                print(f"\nThe source pipx installed m3 from no longer exists:\n"
                      f"    {spec}\n"
                      f"so `pipx upgrade` cannot run. Upgrade from PyPI instead:\n"
                      f"    m3 upgrade --from-pypi\n"
                      f"Your memories and settings under ~/.m3 are kept.")
                return 2
            # `pipx upgrade` rebuilds from the recorded spec, not from PyPI.
            print("  [!] pipx will upgrade from this source, NOT the PyPI release.\n"
                  "      To move to the PyPI release instead: m3 upgrade --from-pypi")

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

    up = upgrade_command(method, owner_python, from_pypi=args.from_pypi)
    if up is None:  # defensive: PLUGIN/UNKNOWN already returned above
        print(f"\nNo upgrade command is defined for method {method!r}.")
        return 2

    scripts_dir = pathlib.Path(owner_python).parent
    launchers = _entry_point_names(pkg)
    old_version = installed_version(pkg)
    if args.wait_for_pid:
        waited = [int(p) for p in args.wait_for_pid.split(",") if p.strip().isdigit()]
        print("waiting for the m3 command that started this window to exit ...")
        if waited and not wait_for_exit(waited):
            print("  it is still running after 60s; checking the launchers anyway.")
    # Agent sessions this run was asked to stop (passed on by the hand-off).
    handed_stop = [int(p) for p in args.stop_pids.split(",") if p.strip().isdigit()]
    if handed_stop and not args.dry_run:
        stop_holders(handed_stop)
    locked = locked_launchers(scripts_dir, launchers)
    hand_off_pids: list[int] = []
    agent_pids: list[int] = []
    console = interactive_console()
    if locked:
        own, others = launcher_holders(locked)
        if not own and not others:
            explain_locked(locked, owner_python, user_flags)  # holders unknown: never guess
            return 2
        if others:
            # Agent sessions (an MCP server, a hook) run from m3.exe. Stop them
            # only with consent: --stop-agents, or the Proceed prompt below,
            # which lists them. Never in a hand-off window (consent was given
            # for the pids it received; anything newer is left alone).
            if args.wait_for_pid or not (args.stop_agents or (console and not args.yes)):
                explain_locked(locked, owner_python, user_flags)
                return 2
            agent_pids = others
        if own:
            # This command's own m3.exe holds a launcher: continue from Python in
            # a new window once it has exited. Only with a person at the console:
            # an unattended caller would read this command's exit 0 as
            # "upgraded" while the upgrade has not started.
            if args.wait_for_pid or not console:
                explain_locked(locked, owner_python, user_flags)
                return 2
            hand_off_pids = own
            print("\nWindows will not let pip replace m3.exe while this command runs "
                  "from it,\nso the upgrade continues in a new window once this one exits.")
        if agent_pids:
            print("\nThese agent sessions run m3 and must stop for the upgrade "
                  "(they reconnect afterwards; Claude Code: /mcp):")
            print("\n".join(h for h in describe_holders(locked) if "own launcher" not in h))

    if not args.yes and not args.dry_run:
        print("\nPlan:")
        if agent_pids:
            print(f"  first: stop m3 in the {len(agent_pids)} agent session(s) listed above")
        print("  1. m3 stop            (release DB-writer file locks)")
        print(f"  2. {' '.join(up)}")
        print("  3. m3 stop            (again: no daemon may survive on OLD code)")
        print("  4. m3 setup           (agent configs, schema migrations, services)")
        print("  5. m3 doctor --fix --fix-hooks   (verify AND repair)")
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
    if hand_off_pids and not dry:
        argv_out = raw_argv + (["--stop-pids", ",".join(str(p) for p in agent_pids)]
                               if agent_pids else [])
        log = hand_off_to_new_window(owner_python, argv_out, hand_off_pids)
        print("Opened the upgrade window. Nothing has changed yet in this one.")
        print(f"Its full output is also written to:\n    {log}")
        return 0
    if agent_pids:
        if dry:
            print(f"\n  would stop m3 in {len(agent_pids)} agent session(s)")
        else:
            stop_holders(agent_pids)

    # Read the current choice BEFORE anything is stopped or replaced: detection
    # looks for an installed SERVICE (unit / plist / scheduled task), which a
    # stop does not remove, but the payload that owns the detector is about to
    # be deleted.
    loop_was_installed = cognitive_loop_installed(m3)
    print(f"\ncognitive loop currently installed: "
          f"{'yes' if loop_was_installed else 'no' if loop_was_installed is False else 'unknown'}")

    if not args.skip_stop:
        print("\n[1/5] stopping m3 DB writers ...")
        # Non-fatal: nothing may be running, and that is a fine state to upgrade from.
        run([m3, "stop"], dry=dry, timeout=180)

    print("\n[2/5] upgrading the package ...")
    # Re-probe: a hook or agent may have started an m3 launcher since the check
    # above, and step 1 stops DB writers, not those.
    locked = [] if dry else locked_launchers(scripts_dir, launchers)
    if locked:
        explain_locked(locked, owner_python, user_flags)
        if not args.skip_stop:
            print("Step 1 stopped m3's services; `m3 setup` starts them again.")
        return 2
    rc = run(up, dry=dry)
    if rc != 0:
        importable = subprocess.run(  # nosec B603 - argv list, no shell
            # In the OWNING interpreter, never this process (see module docstring).
            [owner_python, "-c", "import importlib; importlib.import_module('m3_memory.cli')"],
            capture_output=True
        ).returncode == 0
        if importable:
            print(f"\nUpgrade command failed (exit {rc}). Nothing further was run; "
                  f"m3 {old_version or ''} is still installed.")
        else:
            print(f"\nUpgrade command failed (exit {rc}) after removing the old "
                  "package: m3 is NOT installed now.\nRestore it once nothing "
                  "runs from the m3 launchers:")
            pin = f"m3-memory=={old_version}" if old_version else "m3-memory"
            print(f'    "{owner_python}" -m pip install --force-reinstall --no-deps {pin}')
            print("then: m3 setup")
        return rc

    # Re-resolve: step 2 may have replaced the executable we started with.
    m3 = shutil.which("m3") or shutil.which("mcp-memory") or m3

    # Say plainly when step 2 changed nothing. A package manager exits 0 when the
    # source already holds the installed version, and without this the run ends
    # in "Done." with the old version still installed.
    new_version = None if dry else installed_version(find_m3_package(m3) or pkg)
    unchanged = (not dry and old_version is not None and new_version == old_version)
    if unchanged:
        local_source = bool(spec) and not source_is_pypi(spec) and not args.from_pypi
        where = spec if local_source else "the package index"
        print(f"\n  [!] m3 is still {old_version}: {where} has no newer version.")
        if spec and not source_is_pypi(spec) and not args.from_pypi:
            print("      This install tracks that source, not PyPI. To move to the "
                  "PyPI release: m3 upgrade --from-pypi")

    # A long-lived daemon that SURVIVED step 1 now runs the OLD code against a
    # NEW package, and step 4's verify only restarts services it finds STOPPED —
    # so a survivor is reported "running" and keeps serving stale code
    # indefinitely. That is exactly what setup warns about for the embedder
    # ("these keep running their OLD binary until restarted"), and the cognitive
    # loop is the one that matters most: it is the engine that writes derived
    # knowledge, so stale code there silently produces stale derivations.
    #
    # Step 1 is best-effort AND --skip-stop-able, so it cannot be relied on.
    # Stop again now that the package is replaced: whatever is still up is
    # old-code by definition, and step 4 restarts everything it finds stopped —
    # on the new version. Cross-platform, no elevation, no new platform logic
    # (`m3 stop` already owns the per-OS mechanism), and non-fatal because
    # "nothing running" is a fine state.
    if not args.skip_stop:
        print("\n[3/5] stopping any daemon that survived on OLD code ...")
        # --quiet: step 1 already reported anything left running.
        run([m3, "stop", "--quiet"], dry=dry, timeout=180)

    # --force-quiesce: step 1's `m3 stop` is best-effort and non-fatal, so a
    # writer that did not exit would leave `setup --non-interactive` waiting on
    # a quiesce that never completes -- an unattended upgrade that hangs instead
    # of finishing. Reported by antigravity-agent in review of this script.
    print("\n[4/5] finalizing (agent configs, migrations, services) ...")
    setup_cmd = [m3, "setup", "--non-interactive", "--force-quiesce"]
    # Carry the CURRENT cognitive-loop choice across the upgrade. Without this,
    # `--non-interactive` leaves plan.cognitive_loop False, the role never
    # reaches `expected`, and setup neither restarts nor verifies the loop we
    # just stopped — so a host that had it running comes back WITHOUT it until a
    # watchdog fires. Detected before step 2 replaced the payload.
    if loop_was_installed is True:
        setup_cmd.append("--cognitive-loop")
    elif loop_was_installed is False:
        setup_cmd.append("--no-cognitive-loop")
    else:
        print("  note: could not determine whether the cognitive loop is "
              "installed; leaving that choice untouched. If it was running, "
              "`m3 doctor --fix` or `m3 schedules repair` will bring it back.")
    stopped_agents = 0 if dry else len(handed_stop) + len(agent_pids)

    def _summary(failed_step: str = "", rc: int = 0) -> None:
        if not dry:
            print("\n".join(summary_lines(
                old=old_version, new=new_version, unchanged=unchanged,
                agents_stopped=stopped_agents, failed_step=failed_step, rc=rc,
                log=args.log)))

    # Tells setup that this run verifies and summarizes (see setup_wizard._called_by_upgrade).
    rc = run(setup_cmd, dry=dry, env={**os.environ, "M3_SETUP_CALLER": "upgrade"})
    if rc != 0:
        print(
            f"\n`m3 setup` failed (exit {rc}). The package IS upgraded; re-run\n"
            "`m3 setup` by hand to finish wiring it up."
        )
        _summary("m3 setup", rc)
        return rc

    # --fix --fix-hooks, not a bare `doctor`. An upgrade is the one moment when
    # repairing is unambiguously wanted: the user asked for a new version and is
    # waiting on this command. A report-only verify left self-repairable state
    # broken behind a warning the user had no reason to act on — the embed-server
    # exec bit (a packaging defect in m3's OWN file), a dead agent MCP config, a
    # wedged dashboard. --fix-hooks is included deliberately: hook entries point
    # at the payload that was just REPLACED, and nothing else in the upgrade
    # rewires them, so skipping it is how an upgrade leaves capture silently
    # pointing at the old install. It backs ~/.claude/settings.json up with a
    # timestamp before writing (see environment_probe.repair).
    print("\n[5/5] verifying and repairing ...")
    rc = run([m3, "doctor", "--fix", "--fix-hooks"], dry=dry, timeout=300)
    if rc != 0:
        print(
            f"\n`m3 doctor --fix --fix-hooks` reported problems (exit {rc}). The\n"
            "upgrade completed and repairs were attempted; read the doctor output\n"
            "above before relying on this install."
        )
        _summary("m3 doctor", rc)
        return rc

    if dry:
        print("\nDry run complete; nothing was changed.")
    _summary()
    return 0


if __name__ == "__main__":
    _rc = main()
    if "--pause-at-end" in sys.argv[1:]:
        try:
            input(f"\nm3 upgrade finished (exit code {_rc}). Press Enter to close this window.")
        except EOFError:
            pass
    sys.exit(_rc)
