#!/usr/bin/env python3
"""install_wolfssl.py — build the OPEN-SOURCE wolfSSL library from official
source and install it where M3's secure crypto loader finds it (~/.m3/lib).

Why build instead of download a binary:
  - M3 is Apache-2.0; wolfSSL is GPLv2-or-commercial. M3 must NOT bundle or
    redistribute the binary. Building from the OFFICIAL source on the user's own
    machine keeps M3 license-clean — this script just automates the steps you'd
    run by hand.
  - For a crypto library, provenance matters. We clone only the official
    wolfSSL/wolfssl repo and you can audit/verify every step.

What you get: the OPEN-SOURCE wolfCrypt build — usable with M3_FIPS_MODE=1
(hardened, fail-closed, KAT-checked). It is NOT the CMVP-validated FIPS module
(that is commercial + NDA-gated; M3_FIPS_STRICT requires it). See
docs/FIPS_MODULE_BOUNDARY.md.

Usage:
    python bin/install_wolfssl.py            # clone, build, install to ~/.m3/lib
    python bin/install_wolfssl.py --print-sha # also print the SHA-256 to self-pin
    python bin/install_wolfssl.py --ref v5.9.2  # pin a specific wolfSSL tag

Prerequisites: git, plus a C toolchain —
    Linux/macOS: autoconf/automake/libtool + make + a C compiler (autotools), OR
                 cmake + a generator (Ninja/Make).
    Windows:     cmake + Visual Studio Build Tools (C++ workload).
    Optional:    ninja. If `ninja` is on PATH it is auto-detected and used as
                 the cmake generator, which shows a true percentage progress bar
                 during the build (Ninja emits per-step "[N/M]" counts) and
                 compiles a little faster. It is NEVER required or auto-installed
                 — absent ninja the build uses the platform default generator
                 (Visual Studio on Windows) and shows a spinner instead. This
                 keeps air-gapped/sovereign installs working with no extra
                 dependency: an operator who wants the bar adds ninja to the
                 same build environment that already provides cmake + a compiler.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

WOLFSSL_REPO = "https://github.com/wolfSSL/wolfssl.git"
# Default to a known release tag rather than a moving HEAD, for reproducibility.
# wolfSSL's release tags are suffixed "-stable" (e.g. v5.9.2-stable) — verified
# via `git ls-remote --tags`. Override with --ref. Bump deliberately.
DEFAULT_REF = "v5.9.2-stable"

# Cargo-cult-proof feature set: exactly what crypto_provider.py uses.
#   AES-GCM (vault), SHA-256 (hashing/audit), PBKDF2 (key derivation).
_AUTOTOOLS_FLAGS = [
    "--enable-aesgcm", "--enable-sha256", "--enable-pwdbased",
    "--disable-examples", "--disable-crypttests",
]
_CMAKE_FLAGS = [
    "-DBUILD_SHARED_LIBS=ON",
    "-DWOLFSSL_AESGCM=yes", "-DWOLFSSL_PWDBASED=yes",
    "-DWOLFSSL_EXAMPLES=no",
    # Quiet the per-target "Building Custom Rule …" chatter from the generated
    # build files. The real noise is MSVC's analysis warnings (C4820 padding,
    # C5045 Spectre, C4711/C4710 inlining) — those are filtered at the
    # build-command level (see _build_cmake) and captured to the log file.
    "-DCMAKE_RULE_MESSAGES=OFF",
]


def _say(msg: str) -> None:
    print(f"[install-wolfssl] {msg}", flush=True)


def _run(cmd: list[str], cwd: str | None = None) -> None:
    _say("$ " + " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True)


def _log_path() -> str:
    """Path to the wolfSSL build log under M3's lib dir's sibling logs/ dir."""
    log_dir = os.path.join(os.path.dirname(_m3_lib_dir()), "logs")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "wolfssl-build.log")


# Ninja prefixes each build step with "[N/M] ..." — the only honest source of a
# percentage. The VS/MSBuild generator does not emit a counter, so the bar is
# shown ONLY when the build was driven by Ninja (auto-detected, never forced —
# see _build_cmake). Match e.g. "[88/214] Building C object ...".
_NINJA_STEP = re.compile(r"^\[(\d+)/(\d+)\]\s+(.*)$")
_SRC_FILE = re.compile(r"([^/\\]+\.(?:c|cc|cpp))\b")

_BAR_W = 20          # width of the drawn bar (user asked for ~20 chars)
_HEARTBEAT_S = 15.0  # non-TTY: seconds between plain progress lines


def _short_step(text: str) -> str:
    """Condense a step description to the salient filename so the inline tail
    stays short and stable. Ninja emits 'Building C object .../foo.c.obj' or
    'Linking ...'; MSBuild emits a bare 'foo.c'."""
    if text[:7].lower() == "linking":
        return "linking"
    m = _SRC_FILE.search(text)
    return m.group(1) if m else text[:24]


def _render_status(frac: "float | None", spin: str, tail: str, width: int) -> str:
    """One status line: 20-char bar+percent when a real fraction is known, else
    a spinner; then the inline tail. Hard-truncated to `width` so it never wraps
    (wrapping would break the in-place \\r redraw)."""
    if frac is not None:
        filled = max(0, min(_BAR_W, int(round(_BAR_W * frac))))
        head = f"[{'█' * filled}{'░' * (_BAR_W - filled)}] {frac * 100:3.0f}% | "
    else:
        head = f"building {spin} | "
    line = "[install-wolfssl]   " + head + tail
    return line if len(line) <= width else line[: width - 1] + "…"


def _run_quiet(cmd: list[str], cwd: str | None, log: str, note: str,
               progress: bool = False) -> None:
    """Run a long, noisy build command without flooding the terminal. Full
    output is streamed to `log` line-by-line (no polling/seek race, nothing
    lost). On a TTY we redraw a single in-place status line — a real bar+percent
    when `progress` and the stream carries Ninja [N/M] steps, else a spinner +
    the latest filename. On a non-TTY we emit a plain heartbeat every ~15s (no
    \\r — so redirected/CI/captured output stays clean, the very case that
    motivated this). The live renderer is best-effort: any error in it degrades
    to silent log-only capture rather than failing the install (§1/§3 — a
    cosmetic feature must never break a background build). On a non-zero exit we
    surface the log tail and raise CalledProcessError so main()'s handler still
    catches the failure."""
    is_tty = False
    try:
        is_tty = sys.stdout.isatty()
    except (ValueError, OSError):
        is_tty = False
    try:
        width = max(40, min(shutil.get_terminal_size((100, 24)).columns, 120))
    except OSError:
        width = 100

    _say(f"  {note} (full output -> {log})")
    spinner = itertools.cycle("|/-\\")
    start = time.monotonic()
    last_beat = start
    frac: "float | None" = None
    tail = note
    drew_status = False

    with open(log, "a", encoding="utf-8", errors="replace") as fh:
        fh.write(f"\n$ {' '.join(cmd)}\n")
        fh.flush()
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        stream = proc.stdout
        if stream is None:  # never expected; degrade rather than assert (-O strips asserts)
            proc.wait()
        else:
            for raw in stream:
                fh.write(raw)
                line = raw.rstrip()
                if not line:
                    continue
                m = _NINJA_STEP.match(line) if progress else None
                if m:
                    done, total = int(m.group(1)), int(m.group(2))
                    frac = (done / total) if total else None
                    tail = _short_step(m.group(3))
                elif line[0] not in " \t":
                    # Non-indented MSBuild/compile line — usually a filename;
                    # keep it as the inline tail so there's liveness sans %.
                    tail = line[:48]
                now = time.monotonic()
                try:
                    if is_tty:
                        sys.stdout.write(
                            "\r\033[K"
                            + _render_status(frac, next(spinner), tail, width)
                        )
                        sys.stdout.flush()
                        drew_status = True
                    elif now - last_beat >= _HEARTBEAT_S:
                        pct = f"{frac * 100:.0f}% " if frac is not None else ""
                        _say(f"  {note}… {pct}{now - start:.0f}s elapsed")
                        last_beat = now
                except (OSError, ValueError):
                    # Terminal went away / closed stream — stop drawing, keep
                    # capturing to the log so the build still completes.
                    is_tty = False
            proc.wait()

    if drew_status:
        try:
            sys.stdout.write("\r\033[K")  # clear the in-place line
            sys.stdout.flush()
        except (OSError, ValueError):
            pass
    secs = time.monotonic() - start

    if proc.returncode != 0:
        _say(f"  FAILED (exit {proc.returncode}) — last lines of {log}:")
        try:
            with open(log, encoding="utf-8", errors="replace") as rf:
                for ln in rf.readlines()[-40:]:
                    print(ln.rstrip(), flush=True)
        except OSError:
            pass
        raise subprocess.CalledProcessError(proc.returncode, cmd)
    _say(f"  done ({secs:.0f}s)")


# Common toolchain dirs that a NON-LOGIN / non-interactive shell often misses
# (e.g. Homebrew on Apple Silicon /opt/homebrew/bin, Intel /usr/local/bin, and
# CMake.app on macOS). `m3 fips install-wolfssl` may run from such a shell, where
# cmake/clang are installed but not on PATH — probe these too so we don't falsely
# report a missing prerequisite.
_EXTRA_TOOL_DIRS = (
    "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
    "/Applications/CMake.app/Contents/bin",
)


def _which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    # Fall back to well-known install dirs not on this shell's PATH.
    for d in _EXTRA_TOOL_DIRS:
        cand = os.path.join(d, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _m3_lib_dir() -> str:
    """Mirror crypto_provider._m3_lib_dir so we install where the loader looks."""
    cfg = os.environ.get("M3_CONFIG_ROOT")
    if cfg:
        base = os.path.dirname(os.path.abspath(os.path.expanduser(cfg)))
    else:
        mem = os.environ.get("M3_MEMORY_ROOT")
        base = (os.path.abspath(os.path.expanduser(mem)) if mem
                else os.path.join(os.path.expanduser("~"), ".m3"))
    return os.path.join(base, "lib")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_prereqs() -> tuple[str, list[str]]:
    """Return (build_system, missing_tools). build_system is 'autotools',
    'cmake', or '' if nothing usable is present."""
    if not _which("git"):
        return "", ["git"]
    if os.name == "nt":
        # Windows: cmake + a VS generator is the realistic path.
        missing = [t for t in ("cmake",) if not _which(t)]
        return ("cmake" if not missing else ""), missing
    # Unix: prefer autotools (wolfSSL's native build), fall back to cmake.
    if _which("make") and (_which("cc") or _which("gcc") or _which("clang")):
        if _which("autoconf") and _which("automake") and _which("libtool"):
            return "autotools", []
    if _which("cmake"):
        return "cmake", []
    return "", ["autoconf+automake+libtool+make (or cmake)"]


def _print_macos_prereq_help() -> None:
    """Tiered, minimal macOS guidance. Macs vary — some have Xcode, some
    Homebrew, some neither — so recommend the SMALLEST next step for THIS Mac.

    The lightest viable build needs: clang + make + git (from Xcode Command Line
    Tools, ~few-hundred-MB, NOT the multi-GB Xcode IDE) PLUS either cmake, or the
    autotools (autoconf+automake+libtool). We tell the user exactly which pieces
    they're missing and the quickest way to get just those."""
    have_clt = bool(_which("clang") and _which("make"))
    have_brew = bool(_which("brew"))
    have_cmake = bool(_which("cmake"))
    have_autotools = bool(_which("autoconf") and _which("automake") and _which("libtool"))

    _say("  macOS — install the SMALLEST missing piece for your setup:")
    if not have_clt:
        _say("    1) Command Line Tools (clang+make+git; NOT the full Xcode IDE):")
        _say("         xcode-select --install")
    else:
        _say("    1) [ok] clang + make present (Xcode Command Line Tools).")
    if have_cmake or have_autotools:
        _say("    2) [ok] a build system is present — re-run; PATH may just need "
             "/opt/homebrew/bin (already auto-probed).")
        return
    # Need a build system. cmake is one binary (smallest add); autotools = 3.
    if have_brew:
        _say("    2) Add a build system via Homebrew (cmake is the single smallest add):")
        _say("         brew install cmake")
        _say("       (or the autotools trio:  brew install autoconf automake libtool)")
    else:
        _say("    2) No Homebrew detected. Two light options (pick one):")
        _say("       a) Install Homebrew (one command), then `brew install cmake`:")
        _say('            /bin/bash -c "$(curl -fsSL '
             'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"')
        _say("       b) Or download a standalone CMake .dmg (no Homebrew) from")
        _say("          https://cmake.org/download/ and drag CMake.app to /Applications.")
        _say("          (M3 auto-finds /Applications/CMake.app/Contents/bin.)")


def _build_autotools(src: str) -> str:
    """Configure+make in-tree; return the built shared library path."""
    log = _log_path()
    if os.path.isfile(os.path.join(src, "autogen.sh")):
        _run_quiet(["sh", "autogen.sh"], src, log, "autogen")
    _run_quiet(["sh", "./configure", *_AUTOTOOLS_FLAGS], src, log, "configuring")
    _run_quiet(["make", f"-j{os.cpu_count() or 2}"], src, log, "compiling")
    # Library lands in src/.libs/ as libwolfssl.so -> .so.NN -> .so.NN.N.N (the
    # last is the real file); _find_real_lib follows the symlinks to it.
    libdir = os.path.join(src, "src", ".libs")
    found = _find_real_lib(libdir)
    if found:
        return found
    raise RuntimeError(f"build succeeded but no libwolfssl found under {libdir}")


def _is_shared_lib(fn: str) -> bool:
    """True if `fn` is a wolfSSL shared-library filename, including the VERSIONED
    forms a Linux/macOS build produces (libwolfssl.so.45.0.0, libwolfssl.45.dylib)."""
    if os.name == "nt":
        return fn == "wolfssl.dll"
    if fn == "libwolfssl.so" or fn.startswith("libwolfssl.so."):
        return True
    # .dylib (plain or versioned: libwolfssl.NN.dylib / libwolfssl.dylib)
    return fn.startswith("libwolfssl") and fn.endswith(".dylib")


def _find_real_lib(tree: str) -> "str | None":
    """Find the REAL (non-symlink) shared library under `tree`. cmake/libtool
    emit libwolfssl.so -> .so.45 -> .so.45.0.0 (the last is the real file); we
    follow to the regular file so the install copies actual bytes, not a dangling
    link, and so the SHA-256 is of the real library."""
    best = None
    for root, _dirs, files in os.walk(tree):
        for fn in files:
            if not _is_shared_lib(fn):
                continue
            full = os.path.join(root, fn)
            real = os.path.realpath(full)
            if os.path.isfile(real):
                # Prefer the resolved real file; return immediately.
                return real
            best = best or full
    return best


def _build_cmake(src: str) -> str:
    """Configure+build with cmake; return the built shared library path."""
    build = os.path.join(src, "_m3build")
    os.makedirs(build, exist_ok=True)
    # Resolve cmake's ABSOLUTE path — the bare name may not be on this shell's
    # PATH (e.g. Homebrew /opt/homebrew/bin on a non-login macOS SSH session)
    # even though _which() located it.
    cmake = _which("cmake") or "cmake"
    gen_args: list[str] = []
    build_args: list[str] = []
    # Prefer Ninja WHEN IT IS ALREADY PRESENT: it emits "[N/M]" step lines, the
    # only honest source of a build percentage, and builds faster. We never
    # *force* it — absent ninja we fall back to the platform default generator
    # (Visual Studio on Windows) so no working machine regresses for the sake of
    # a progress bar (§1 cross-platform). `use_ninja` gates the bar in _run_quiet.
    use_ninja = bool(_which("ninja"))
    if use_ninja:
        gen_args = ["-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release"]
    elif os.name == "nt":
        gen_args = ["-G", "Visual Studio 17 2022", "-A", "x64"]
        # MSBuild has no step counter, so no bar — but still quiet the console:
        # minimal verbosity + errors-only logger. The C4820/C5045/C4711 wall
        # still lands in the log file (captured by _run_quiet), not the terminal.
        build_args = ["--", "/verbosity:minimal", "/clp:ErrorsOnly;Summary"]
    log = _log_path()
    _run_quiet([cmake, *gen_args, *_CMAKE_FLAGS, src], build, log, "configuring")
    _run_quiet(
        [cmake, "--build", build, "--config", "Release", *build_args],
        build, log, "compiling (this takes ~1-2 min)", progress=use_ninja,
    )
    found = _find_real_lib(build)
    if found:
        return found
    raise RuntimeError(f"cmake build succeeded but no shared library found under {build}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build+install open-source wolfSSL for M3.")
    ap.add_argument("--ref", default=DEFAULT_REF, help=f"wolfSSL git tag/branch (default {DEFAULT_REF}).")
    ap.add_argument("--dest", default=None, help="Install dir (default: M3's ~/.m3/lib).")
    ap.add_argument("--print-sha", action="store_true", help="Print the installed lib's SHA-256.")
    ap.add_argument("--keep-build", action="store_true", help="Don't delete the temp build tree.")
    args = ap.parse_args(argv)

    build_system, missing = _check_prereqs()
    if not build_system:
        _say("ERROR: missing build prerequisites: " + ", ".join(missing))
        if sys.platform == "darwin":
            _print_macos_prereq_help()
        else:
            _say("  Linux/Debian:  sudo apt install git autoconf automake libtool make gcc")
            _say("  Fedora/RHEL:   sudo dnf install git autoconf automake libtool make gcc")
            _say("  Arch:          sudo pacman -S git autoconf automake libtool make gcc")
            _say("  Windows:       install Git, CMake, and Visual Studio Build Tools (C++ workload)")
        return 2

    dest_dir = args.dest or _m3_lib_dir()
    os.makedirs(dest_dir, exist_ok=True)
    lib_filename = "wolfssl.dll" if os.name == "nt" else (
        "libwolfssl.dylib" if sys.platform == "darwin" else "libwolfssl.so")
    dest_path = os.path.join(dest_dir, lib_filename)

    work = tempfile.mkdtemp(prefix="m3-wolfssl-")
    src = os.path.join(work, "wolfssl")
    try:
        _say(f"cloning official wolfSSL {args.ref} (shallow) …")
        _run(["git", "clone", "--depth", "1", "--branch", args.ref, WOLFSSL_REPO, src])

        _say(f"building (system={build_system}) with M3's feature set "
             "(AES-GCM, SHA-256, PBKDF2) …")
        built = _build_autotools(src) if build_system == "autotools" else _build_cmake(src)

        shutil.copy2(built, dest_path)
        digest = _sha256(dest_path)
        _say(f"installed -> {dest_path}")
        _say(f"SHA-256:  {digest}")
        _say("This is the OPEN-SOURCE (non-FIPS) build — works with M3_FIPS_MODE=1.")
        _say("To self-pin your trusted build (recommended):")
        if os.name == "nt":
            _say(f'  setx M3_WOLFSSL_SHA256 {digest}')
        else:
            _say(f"  export M3_WOLFSSL_SHA256={digest}")
        _say("Verify anytime with:  m3 doctor   (crypto section)")
        if args.print_sha:
            print(digest)
        return 0
    except subprocess.CalledProcessError as e:
        _say(f"build/install failed: {e}")
        return 1
    finally:
        if not args.keep_build:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
