"""Console-entrypoint probe — does `m3` on PATH actually run THIS install?

m3 ships console scripts (``m3``, ``mcp-memory``, ``m3-team``). Three separate
mechanisms can install them — pipx, ``pip install --user``, and a system-wide
``pip install`` — and each drops its own launcher into a different directory.
Nothing reconciles them, so a machine accumulates shims and **PATH order alone**
decides which one wins. Two failure modes follow, and neither surfaces anywhere
else:

1. **Shadowing.** An older copy earlier on PATH wins over the current install.
   The user upgrades, sees the new version reported by the installer, and keeps
   running months-old code. Observed 2026-07-25: ``mcp-memory`` resolved to a
   3-week-old ``pip --user`` shim while pipx held the current payload, so the
   backwards-compat MCP entrypoint served stale code.

2. **Orphaned launcher.** The backing package is uninstalled but the ``.exe`` /
   script survives (a failed reinstall, a partially-removed user-site install).
   Invoking it raises ``ModuleNotFoundError`` — the launcher exists, so PATH
   lookup "succeeds", and the failure looks like a broken m3 rather than a
   stale file. A failed ``pipx install --force`` can leave exactly this: the
   old package uninstalled, the new one not yet in place.

Both are invisible to every other probe. ``plugin_version_probe`` compares the
plugin manifest against the payload; ``schedule_probe`` checks that scheduled
jobs point at a live interpreter. Neither asks the simpler question: *when the
user types ``m3``, do they get this code?*

DOES bump the doctor exit code. A shadowed or orphaned entrypoint means the
commands in every runbook, doc, and error message do not do what they say —
that is a broken install, not a preference. The fix is printed and is always
one of: remove the stale launcher, or reorder PATH.

Cross-platform, network-free, never raises.
"""
from __future__ import annotations

import os
import subprocess
import sys

# Console scripts m3 declares. `m3` is the primary CLI; `mcp-memory` is the
# backwards-compat MCP entrypoint some client configs still launch directly, so
# a stale copy of it is just as damaging as a stale `m3`.
ENTRYPOINTS = ("m3", "mcp-memory", "m3-team")


def _neutral_cwd() -> str:
    """A directory that shadows nothing — for running version probes from.

    Python puts the working directory at ``sys.path[0]``, so ANY process
    started inside a source checkout imports that checkout's packages and reads
    its ``*.dist-info`` before the installed ones. For a probe whose entire job
    is comparing installed-vs-running versions, that turns the cwd into a hidden
    input: the same probe, same interpreter, reports 0 findings from ``/tmp``
    and 3 "stale launcher" FAILs from a checkout (measured 2026-08-11).

    Falls back to the current directory only if tempdir is unavailable — worse
    than nothing is a crash in a diagnostic.
    """
    try:
        import tempfile
        return tempfile.gettempdir()
    except Exception:  # noqa: BLE001 — a probe must never die on its own setup
        return os.getcwd()


def _installed_version() -> str | None:
    """Version of the m3-memory package the ENTRYPOINTS resolve to.

    Tries several sources: the doctor can run from a bin/ script whose sys.path
    does not include the installed package, and a lookup that quietly returns
    None turns the stale/shadowed checks into a silent no-op — the probe would
    report "OK" precisely when it is least able to tell. Falling back through
    both distribution names and the package attribute keeps it working from a
    repo checkout as well as an installed wheel.

    ⚠ The metadata lookup is deliberately NOT done in-process.
    ``importlib.metadata`` searches ``sys.path``, whose first entry is the
    working directory, so running the doctor from a source checkout reads THAT
    tree's dist-info — a different version from the one the launchers use. The
    probe then compares the checkout against itself and reports every launcher
    as stale. Resolve it in a child pinned to a neutral cwd instead, which is
    the same vantage point ``_probe_version`` now uses for the other side of
    the comparison; both sides must agree on where they are standing.
    """
    import importlib.metadata as md

    try:
        proc = subprocess.run(
            [sys.executable, "-c",
             "import importlib.metadata as m;print(m.version('m3-memory'))"],
            capture_output=True, text=True, timeout=20, cwd=_neutral_cwd(),
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        # Only a genuine spawn failure falls through. A broad `except` here
        # would swallow a NameError/AttributeError in this very block and
        # silently restore the shadowed in-process answer — the bug looks
        # fixed while the probe keeps lying. (It did exactly that during
        # development: a missing `import sys` was caught and hidden.)
        pass

    for dist in ("m3-memory", "m3_memory"):
        try:
            return md.version(dist)
        except Exception:  # noqa: BLE001 — try the next source
            pass
    try:
        from m3_memory import __version__
        return str(__version__)
    except Exception:  # noqa: BLE001 — try the next source
        pass

    # Repo-checkout fallback. The doctor is launched as bin/memory_doctor.py,
    # so when it runs under an interpreter that does not have m3-memory
    # installed (a bare `python bin/memory_doctor.py` on system Python, say)
    # BOTH metadata lookups and the package import fail — there is no
    # distribution to find. Without this, the probe degrades to "SKIPPED"
    # exactly on a dev machine, where mismatched entrypoints are most likely.
    # pyproject.toml is the documented single source of truth for the version.
    try:
        import re
        pyproject = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "pyproject.toml",
        )
        with open(pyproject, "r", encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r'\s*version\s*=\s*["\']([^"\']+)["\']', line)
                if m:
                    return m.group(1)
    except Exception:  # noqa: BLE001 — genuinely undeterminable
        pass
    return None


def _which_all(name: str) -> list[str]:
    """Every launcher for `name` on PATH, in resolution order.

    shutil.which() returns only the winner; the whole point here is to see the
    losers too, since a shadowed install is precisely "more than one exists".
    """
    found: list[str] = []
    exts = [""]
    if os.name == "nt":
        exts = [e for e in os.environ.get("PATHEXT", ".EXE").split(os.pathsep) if e]
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        for ext in exts:
            candidate = os.path.join(directory, name + ext)
            if os.path.isfile(candidate) and candidate not in found:
                found.append(candidate)
    return found


def _probe_version(path: str, timeout: float = 20.0) -> tuple[str | None, str | None]:
    """Run `<path> --version`. Returns (version, error).

    An orphaned launcher fails here with ModuleNotFoundError while still
    existing on disk — which is exactly the state we need to distinguish from
    "missing". Never raises: a hung or unrunnable launcher is itself a finding.
    """
    # cwd=<neutral>: a child inherits the doctor's working directory, and
    # sys.path[0] is that directory. Run this from a source checkout and the
    # launcher imports THAT tree's m3_memory instead of the one it was
    # installed with, so `--version` reports the checkout — not the launcher.
    # The probe then compares a shadowed number against a shadowed number and
    # invents a "stale launcher" that does not exist (§5). Neutralise the cwd
    # so the launcher resolves the package it actually ships with.
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True, text=True, timeout=timeout,
            cwd=_neutral_cwd(),
        )
    except Exception as e:  # noqa: BLE001 — an unrunnable launcher is a finding
        return None, f"{type(e).__name__}: {e}"

    out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if proc.returncode != 0:
        tail = out.splitlines()[-1].strip() if out else f"exit {proc.returncode}"
        return None, tail[:160]

    # "m3-memory 2026.7.25.0" -> "2026.7.25.0"
    parts = out.split()
    return (parts[-1] if parts else out), None


def check() -> list[dict]:
    """Findings for every console script. Pure detection; no printing."""
    expected = _installed_version()
    findings: list[dict] = []

    for name in ENTRYPOINTS:
        launchers = _which_all(name)
        if not launchers:
            # Not every install exposes every script (m3-team is optional).
            continue

        winner = launchers[0]
        version, error = _probe_version(winner)

        if error is not None:
            findings.append({
                "entrypoint": name, "path": winner, "kind": "broken",
                "detail": error,
            })
        elif expected and version and version != expected:
            findings.append({
                "entrypoint": name, "path": winner, "kind": "stale",
                "detail": f"runs {version}, installed package is {expected}",
            })

        if len(launchers) > 1:
            findings.append({
                "entrypoint": name, "path": winner, "kind": "shadowed",
                "detail": "also on PATH: " + ", ".join(launchers[1:]),
            })

    return findings


def run(brief: bool = False) -> int:
    """Print findings. Returns 1 when the install is broken/stale, else 0."""
    try:
        findings = check()
    except Exception as e:  # noqa: BLE001 — the doctor must never die on a probe
        if not brief:
            print(f"  entrypoints: probe failed (non-fatal): {type(e).__name__}: {e}")
        return 0

    if not findings:
        expected = _installed_version()
        if expected is None:
            # Say so rather than printing a reassuring "OK": with no expected
            # version the stale/shadowed comparisons were skipped, so this is
            # "not checked", not "healthy". Exit-code-neutral — an
            # undeterminable version is an environment quirk, not a broken
            # install.
            print("⚠️  entrypoints: SKIPPED — could not determine the installed "
                  "m3-memory version (stale/shadowed checks did not run)")
            return 0
        # `brief` means ONE LINE, not silence. Printing nothing in the default
        # mode hides the check from every user who does not pass --verbose —
        # which is nearly all of them.
        print(f"✅ entrypoints: OK — `m3` on PATH runs this install ({expected})")
        return 0

    # A source checkout is the common benign cause, and the generic advice
    # ("remove the stale launcher") is actively wrong there — the launchers are
    # fine; the doctor is simply running under a DIFFERENT install than the one
    # on PATH. Naming that case first stops a developer uninstalling a working
    # entrypoint to satisfy a warning about their own venv.
    _running_from_checkout = os.path.exists(
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "pyproject.toml",
        )
    )
    print("⚠️  entrypoints: [FAIL] the m3 commands on PATH do not match this install")
    if _running_from_checkout:
        print("    note: this doctor is running from a SOURCE CHECKOUT, whose venv")
        print("          is a different install from the one on PATH. That alone")
        print("          explains a version difference and is not a broken setup —")
        print("          compare against `m3 doctor` run from the installed copy")
        print("          before changing anything.")
    for f in findings:
        print(f"    - {f['entrypoint']} ({f['kind']}): {f['path']}")
        print(f"      {f['detail']}")

    kinds = {f["kind"] for f in findings}
    print("      fix:")
    if "broken" in kinds:
        print("        orphaned launcher (package gone, script left behind) —")
        print("        remove it, then reinstall:  pipx install --force m3-memory")
    if "stale" in kinds or "shadowed" in kinds:
        print("        a different copy wins on PATH. Remove the stale launcher")
        print("        (`pip uninstall m3-memory` for a pip --user copy), or put")
        print("        the pipx bin dir ahead of it in PATH (`pipx ensurepath`).")

    # Shadowing alone is a real functional defect: the user runs code they did
    # not install. Bump the exit code so `m3 doctor` fails loudly rather than
    # burying it in output.
    return 1
