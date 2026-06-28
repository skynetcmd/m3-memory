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
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

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
]


def _say(msg: str) -> None:
    print(f"[install-wolfssl] {msg}", flush=True)


def _run(cmd: list[str], cwd: str | None = None) -> None:
    _say("$ " + " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True)


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
    if os.path.isfile(os.path.join(src, "autogen.sh")):
        _run(["sh", "autogen.sh"], cwd=src)
    _run(["sh", "./configure", *_AUTOTOOLS_FLAGS], cwd=src)
    _run(["make", f"-j{os.cpu_count() or 2}"], cwd=src)
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
    if os.name == "nt":
        gen_args = ["-G", "Visual Studio 17 2022", "-A", "x64"]
    _run([cmake, *gen_args, *_CMAKE_FLAGS, src], cwd=build)
    _run([cmake, "--build", build, "--config", "Release"], cwd=build)
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
