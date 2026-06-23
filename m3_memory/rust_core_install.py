"""Resolve and install the right prebuilt `m3-core-rs` wheel for this host.

m3-core-rs is published to PyPI as several differently-named packages, one
per (OS, backend) pair, all installing the same `m3_core_rs` import module:

    m3-core-rs-windows-cpu      m3-core-rs-linux-cpu
    m3-core-rs-windows-cuda     m3-core-rs-linux-cuda
    m3-core-rs-windows-vulkan   m3-core-rs-linux-vulkan
                                m3-core-rs-macos-metal

The user's single entry point is the m3 setup wizard / `m3 install-gpu`,
which detects (os, backend) here, installs the matching prebuilt wheel from
PyPI, and only falls back to a from-source build when no prebuilt wheel is
available for the host's platform + Python.

The (os, backend) -> package-name mapping MUST stay byte-identical to
`crates/m3-core-py/build_wheel.py::package_name` in the m3-core-rs repo —
that script names the wheels this module installs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

from m3_memory._platform import os_name as _os_name

# Release this m3-memory build expects. Bump in lockstep with the m3-core-rs
# release tag (v2026.06.22 == 3.6.22). Used as the version pin for both the
# prebuilt PyPI install and the source-build fallback.
M3_CORE_RS_VERSION = "3.6.22"
M3_CORE_RS_GIT_TAG = "v2026.06.22"

# Cargo features per backend, mirroring build_wheel.py's _MATRIX (the source
# fallback passes these to maturin via pip's config-settings).
# CPU uses `embedded` (CPU-only llama.cpp) so every build ships an in-process
# BGE-M3 EmbeddedEmbedder — m3 must always have a default bge-m3 embedder, not
# depend on the embed-server being present. A source-fallback CPU build thus
# needs a C/C++ compiler + cmake.
_BACKEND_FEATURES: dict[str, list[str]] = {
    "cpu": ["embedded"],
    "cuda": ["embedded-cuda"],
    "vulkan": ["embedded-vulkan"],
    "metal": ["embedded-metal"],
}

# Valid (os, backend) combinations. macOS is Metal-only by design.
_VALID: set[tuple[str, str]] = {
    ("windows", "cpu"), ("windows", "cuda"), ("windows", "vulkan"),
    ("linux", "cpu"), ("linux", "cuda"), ("linux", "vulkan"),
    ("macos", "metal"),
}


@dataclass(frozen=True)
class BackendChoice:
    os_tok: str          # windows | linux | macos
    backend: str         # cpu | cuda | vulkan | metal
    reason: str          # human-readable why this backend was picked

    @property
    def package(self) -> str:
        return package_name(self.os_tok, self.backend)

    @property
    def features(self) -> list[str]:
        return _BACKEND_FEATURES[self.backend]


def package_name(os_tok: str, backend: str) -> str:
    """PyPI project name for an (os, backend) pair. Mirrors build_wheel.py."""
    return f"m3-core-rs-{os_tok}-{backend}"


def host_os() -> str:
    """OS token (windows/linux/macos) from the WMI-safe platform helper."""
    name = _os_name()  # 'Windows' | 'Darwin' | 'Linux'
    return {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}[name]


def _vulkan_has_device() -> bool:
    """Return True if vulkaninfo reports at least one physical GPU device.

    vulkaninfo is often installed on headless Linux boxes (as part of the
    mesa/vulkan-tools package) without any Vulkan-capable GPU. Presence of
    the binary alone is not a reliable signal. We run `vulkaninfo --summary`
    (fast, no display required) and look for a GPU name line, which only
    appears when a real device is enumerated.

    Returns False on any error (timeout, permission denied, parse failure)
    so the caller always falls back to CPU safely.
    """
    try:
        result = subprocess.run(
            ["vulkaninfo", "--summary"],
            capture_output=True, text=True, timeout=5,
        )
        output = result.stdout + result.stderr
        # vulkaninfo --summary prints "GPU id : 0 (Device Name)" for each device.
        # "No devices available" or empty deviceName means no real GPU.
        for line in output.splitlines():
            lo = line.lower()
            if "gpu id" in lo and "no device" not in lo:
                return True
            # Also catch "deviceName" from the full JSON-style output.
            if "devicename" in lo and lo.split("=")[-1].strip() not in ("", "unknown"):
                return True
    except Exception:
        pass
    return False


def detect_backend(os_tok: Optional[str] = None) -> BackendChoice:
    """Pick the best backend for this host.

    Order: macOS -> Metal (always). Else NVIDIA toolchain (nvcc) -> CUDA;
    else a Vulkan runtime/SDK -> Vulkan; else CPU. This intentionally matches
    the legacy detection in embedder_admin.cmd_install_gpu so behavior is
    unchanged for callers that relied on it — only the *install action*
    (prebuilt wheel first) changes.
    """
    os_tok = os_tok or host_os()

    if os_tok == "macos":
        return BackendChoice(os_tok, "metal", "macOS — Metal is the only backend")

    if shutil.which("nvcc") or os.environ.get("CUDA_PATH"):
        return BackendChoice(os_tok, "cuda", "NVIDIA CUDA toolchain detected")

    # Vulkan: SDK env var (explicit build-time config) OR vulkaninfo reports
    # at least one real device. vulkaninfo presence alone is not enough —
    # the Vulkan loader/tools are often installed system-wide on headless
    # Linux boxes without any Vulkan-capable GPU. Probe the output.
    if os.environ.get("VULKAN_SDK"):
        return BackendChoice(os_tok, "vulkan", "VULKAN_SDK env var set")
    if shutil.which("vulkaninfo") and _vulkan_has_device():
        return BackendChoice(os_tok, "vulkan", "Vulkan device detected via vulkaninfo")

    return BackendChoice(os_tok, "cpu", "no GPU toolchain detected — CPU build")


def _pip(*args: str, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pip", *args],
        env=env if env is not None else os.environ.copy(),
        capture_output=True,
        text=True,
    )


def _is_pep668(result: subprocess.CompletedProcess) -> bool:
    """Return True if pip refused due to PEP 668 (externally-managed-environment)."""
    return result.returncode != 0 and "externally-managed-environment" in result.stderr


def _can_sudo() -> bool:
    """Return True if the current user can run sudo without a password prompt.

    Uses `sudo -n true` — the -n flag makes sudo fail immediately rather than
    prompting, so this is safe to call non-interactively.
    """
    return subprocess.run(
        ["sudo", "-n", "true"],
        capture_output=True,
    ).returncode == 0


def _in_privileged_group() -> bool:
    """Return True if the user is in the 'sudo' or 'wheel' group."""
    try:
        import grp
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        privileged = {"sudo", "wheel"}
        for g in grp.getgrall():
            if g.gr_name in privileged and user in g.gr_mem:
                return True
    except Exception:
        pass
    return False


def _pip_install_with_pep668_fallback(*pip_args: str) -> int:
    """Run pip install, retrying with --user on PEP 668 systems.

    Strategy:
      1. Try pip install <args> as-is.
      2. If pip rejects with PEP 668 (externally-managed-environment),
         retry with --user (installs to ~/.local, no root needed).
      3. If --user also fails, detect whether the user can sudo and
         print the exact command(s) to run as root, then return non-zero.

    Returns 0 on success, non-zero otherwise.
    """
    result = _pip(*pip_args)
    if result.returncode == 0:
        return 0

    if not _is_pep668(result):
        # Some other pip error — print stderr and propagate.
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        return result.returncode

    print("[rust-core] pip refused (PEP 668 externally-managed-environment); "
          "retrying with --user ...", file=sys.stderr)
    user_result = _pip("--user", *pip_args)
    if user_result.returncode == 0:
        return 0

    # --user also failed. Build the exact sudo command and advise the user.
    sudo_cmd = (
        f"sudo pip install --break-system-packages {' '.join(pip_args)}"
    )
    can_sudo = _can_sudo()
    in_group = _in_privileged_group()

    if can_sudo:
        print(
            "[rust-core] --user install also failed. You have sudo access — "
            "run this command to install system-wide:\n"
            f"    {sudo_cmd}",
            file=sys.stderr,
        )
    elif in_group:
        print(
            "[rust-core] --user install also failed. You are in the sudo/wheel "
            "group but sudo requires your password. Open another shell and run:\n"
            f"    {sudo_cmd}",
            file=sys.stderr,
        )
    else:
        print(
            "[rust-core] --user install also failed and you do not appear to "
            "have sudo access.\n"
            "Ask a system administrator to open a root shell and run:\n"
            f"    {sudo_cmd}\n"
            "Or ask them to add you to the 'sudo' (Debian/Ubuntu) or 'wheel' "
            "(RHEL/Fedora/Arch) group, then log out and back in.",
            file=sys.stderr,
        )

    if user_result.stderr:
        print(user_result.stderr, file=sys.stderr, end="")
    return user_result.returncode


def install_prebuilt(choice: BackendChoice, *, version: str = M3_CORE_RS_VERSION) -> int:
    """Try to install the matching prebuilt wheel from PyPI.

    Returns 0 on success, non-zero otherwise. A non-zero result (other than a
    PEP 668 advisory already printed) signals to fall back to a source build.
    """
    spec = f"{choice.package}=={version}"
    print(f"[rust-core] installing prebuilt wheel: {spec}  ({choice.reason})")
    return _pip_install_with_pep668_fallback(
        "install", "--upgrade", "--only-binary=:all:", spec
    )


def _find_executable(candidates: list[str]) -> Optional[str]:
    """Return the first executable from candidates that exists and runs.

    A binary that exists but isn't executable by the current user is
    treated as absent (same end-user symptom: the build can't invoke it).
    """
    for cmd in candidates:
        path = shutil.which(cmd)
        if path:
            try:
                subprocess.run([path, "--version"], capture_output=True, timeout=5)
                return path
            except (PermissionError, OSError):
                pass  # binary exists but not executable for this user
    return None


def _find_cargo() -> Optional[str]:
    """Locate cargo, including rustup-installed toolchains not on PATH.

    The friction case (2026-06-07): user had rustup-installed Rust but
    ~/.cargo/bin wasn't sourced, so `shutil.which("cargo")` returned None
    even though cargo was at ~/.rustup/toolchains/<triple>/bin/cargo. The
    source-build then failed mid-compile with a cryptic error after pip
    had already pulled all deps. Probing rustup's toolchain dirs catches
    this and lets us report a clean missing-prereq error up front.
    """
    found = _find_executable(["cargo"])
    if found:
        return found

    # ~/.cargo/bin is rustup's "current toolchain" symlink dir
    candidate = os.path.expanduser("~/.cargo/bin/cargo")
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate

    # Toolchain-specific dirs (rustup default install w/o PATH wiring)
    rustup_home = os.path.expanduser(os.environ.get("RUSTUP_HOME", "~/.rustup"))
    toolchains_dir = os.path.join(rustup_home, "toolchains")
    if os.path.isdir(toolchains_dir):
        try:
            for toolchain in os.listdir(toolchains_dir):
                candidate = os.path.join(toolchains_dir, toolchain, "bin", "cargo")
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    return candidate
        except OSError:
            pass
    return None


def _check_build_tools() -> list[str]:
    """Return a list of missing build tools needed for a source build.

    Checks for cmake, a C++ compiler, and the Rust toolchain (cargo). All
    must be executable by the current user — a binary that exists but
    isn't executable is reported as missing (same symptom as absent).

    Rust check probes rustup's toolchain dirs in addition to PATH — see
    _find_cargo for the rationale.
    """
    missing = []
    if not _find_executable(["cmake"]):
        missing.append("cmake")
    if not _find_executable(["c++", "g++", "clang++"]):
        missing.append("C++ compiler")
    if not _find_cargo():
        missing.append("Rust (cargo)")
    return missing


def _print_manual_build_recommendation(
    choice: BackendChoice, *, pypi_rc: int, release_rc: int
) -> None:
    """Print a multi-line, actionable recommendation when both prebuilt
    paths missed and the caller has disabled auto-source-build.

    Two audiences read this:
      - The curl-install.sh user who saw "Project Oxidation" prompt say yes.
        For them, the wheel isn't critical — tier-2 HTTP keeps embeddings
        working. They need to know that (so they don't think they're broken)
        AND how to opt into the optional build if they want the speed.
      - Operators triaging a CI/non-interactive deploy. They need the exact
        repro: package name, version, and the explicit command to run.
    """
    feats = ",".join(choice.features) if choice.features else "(none)"
    install_rust_cmd = (
        "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y"
    )
    print(
        f"\n[rust-core] No prebuilt wheel available for {choice.package} "
        f"{M3_CORE_RS_VERSION} on this Python.\n"
        f"            PyPI returned exit {pypi_rc}; "
        f"GitHub Release returned exit {release_rc}.\n"
        f"\n"
        f"            Embeddings still work without it: the tier-2 HTTP\n"
        f"            embedder (m3-embed-server on port 8082) serves every\n"
        f"            request. The native wheel just makes embeddings\n"
        f"            5-10x faster (in-process Rust vs HTTP round-trip).\n"
        f"\n"
        f"            To install the native wheel by compiling from source:\n"
        f"\n"
        f"            1. Install prerequisites:\n"
        f"                 macOS:          xcode-select --install && brew install cmake\n"
        f"                 Debian/Ubuntu:  sudo apt install cmake build-essential\n"
        f"                 Fedora/RHEL:    sudo dnf install cmake gcc-c++\n"
        f"                 Arch:           sudo pacman -S cmake base-devel\n"
        f"                 Windows:        install Visual Studio Build Tools (C++ workload)\n"
        f"\n"
        f"            2. Install the Rust toolchain (if not already present):\n"
        f"                 {install_rust_cmd}\n"
        f"                 source \"$HOME/.cargo/env\"\n"
        f"\n"
        f"            3. Run the source-build path explicitly:\n"
        f"                 m3 embedder install-gpu\n"
        f"\n"
        f"            (Will build {choice.package} from "
        f"{M3_CORE_RS_GIT_TAG} with features: {feats}.)\n",
        file=sys.stderr,
    )


# GitHub Release fallback — owner/repo for the published-wheels release.
# Kept module-level so tests can monkeypatch and so a fork can override
# without touching the install logic.
M3_CORE_RS_GH_REPO = "skynetcmd/m3-core-rs"


def install_from_github_release(
    choice: BackendChoice, *,
    version: str = M3_CORE_RS_VERSION,
    git_tag: str = M3_CORE_RS_GIT_TAG,
    repo: str = M3_CORE_RS_GH_REPO,
) -> int:
    """Try to install the matching prebuilt wheel from the GitHub Release.

    Sits between install_prebuilt (PyPI) and install_from_source. The
    GitHub Release is the canonical home for wheels too large for PyPI's
    100 MiB cap (Linux CUDA static build is 464 MB) and a defensive
    fallback for every other backend when PyPI is missing the right
    version. Public release only — unauthenticated GitHub API; draft
    releases are invisible here by design.

    Wheel naming convention (set by m3-core-rs/crates/m3-core-py/build_wheel.py):
        m3_core_rs_<os>_<backend>-<version>-cp<py>-cp<py>-<platform_tag>.whl
    The platform tag varies per backend (manylinux_2_17 for Linux CPU,
    bare linux_x86_64 for Linux CUDA static, macosx_*_arm64, win_amd64, ...).
    We match by the deterministic prefix — `m3_core_rs_<os>_<backend>-<ver>-cp<py>-`
    — and pick the single asset that starts with it. If multiple match,
    pick the first (sorted) so the choice is deterministic across runs.

    Returns 0 on success, non-zero on any failure (API miss, no asset,
    download failure, pip rejection). On non-zero the caller falls through
    to install_from_source.
    """
    import json
    import tempfile
    import urllib.error
    import urllib.request

    py_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    asset_prefix = f"m3_core_rs_{choice.os_tok}_{choice.backend}-{version}-{py_tag}-"

    print(f"[rust-core] looking for GitHub Release asset matching "
          f"{asset_prefix}*.whl  (repo={repo}, tag={git_tag})")

    api_url = f"https://api.github.com/repos/{repo}/releases/tags/{git_tag}"
    try:
        req = urllib.request.Request(
            api_url,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "m3-memory-installer",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310 - static https GitHub API URL
            release = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"[rust-core] release {git_tag} not found on GitHub "
                  f"(may be draft or not yet published); skipping Release fallback",
                  file=sys.stderr)
        else:
            print(f"[rust-core] GitHub API HTTP {e.code} fetching {git_tag}; "
                  f"skipping Release fallback", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"[rust-core] GitHub API fetch failed ({type(e).__name__}: {e}); "
              f"skipping Release fallback", file=sys.stderr)
        return 1

    assets = release.get("assets") or []
    matches = sorted(
        (a for a in assets if str(a.get("name", "")).startswith(asset_prefix)),
        key=lambda a: a["name"],
    )
    if not matches:
        print(f"[rust-core] no Release asset matches {asset_prefix}*.whl "
              f"({len(assets)} assets in {git_tag})", file=sys.stderr)
        return 1

    asset = matches[0]
    wheel_url = asset["browser_download_url"]
    wheel_name = asset["name"]
    wheel_size = asset.get("size", 0)

    # The download URL comes from the GitHub API response, i.e. external data.
    # Pin the scheme to https before opening so a tampered/unexpected response
    # can't redirect the installer to file:// (local-file read) or a plaintext
    # http downgrade. Fail loud rather than fetch an untrusted scheme.
    if not wheel_url.lower().startswith("https://"):
        print(f"[rust-core] refusing non-https asset URL ({wheel_url!r}); "
              f"skipping Release fallback", file=sys.stderr)
        return 1

    print(f"[rust-core] downloading {wheel_name} "
          f"({wheel_size / (1024*1024):.1f} MiB)...")

    # Download into a temp DIR, keeping the original filename: pip parses
    # the wheel filename per PEP 427 to identify the package, so the file
    # must be named e.g. m3_core_rs_macos_metal-3.6.6-cp314-cp314-macosx_11_0_arm64.whl
    # — a random NamedTemporaryFile path like /tmp/tmpXXXX.whl is rejected
    # by pip with "Invalid wheel filename (wrong number of parts)".
    tmp_dir = tempfile.mkdtemp(prefix="m3-core-rs-")
    wheel_path = os.path.join(tmp_dir, wheel_name)
    try:
        downloaded = 0
        try:
            resp = urllib.request.urlopen(wheel_url, timeout=300)  # nosec B310 - https scheme validated above
            with resp, open(wheel_path, "wb") as out:
                chunk_size = 1024 * 1024            # 1 MiB
                next_progress = 10 * 1024 * 1024    # heartbeat every 10 MiB
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    if downloaded >= next_progress:
                        print(f"[rust-core]   ... "
                              f"{downloaded / (1024*1024):.0f} MiB",
                              file=sys.stderr)
                        next_progress += 10 * 1024 * 1024
        except (urllib.error.URLError, OSError) as e:
            print(f"[rust-core] wheel download failed "
                  f"({type(e).__name__}: {e})", file=sys.stderr)
            return 1

        if downloaded == 0:
            print("[rust-core] wheel download yielded 0 bytes", file=sys.stderr)
            return 1

        print(f"[rust-core] downloaded {downloaded / (1024*1024):.1f} MiB; "
              f"installing via pip...")
        return _pip_install_with_pep668_fallback(
            "install", "--force-reinstall", "--no-deps", wheel_path,
        )
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except OSError:
            pass


def install_from_source(choice: BackendChoice, *,
                        git_tag: str = M3_CORE_RS_GIT_TAG) -> int:
    """Build m3-core-rs from the git source with the backend's Cargo features.

    Fallback when no prebuilt wheel matches. Requires a Rust toolchain
    (>=1.94) + maturin, and the backend's native toolchain (CUDA/Vulkan/Metal
    + a C/C++ compiler). Features are passed to maturin via pip's PEP 517
    config-settings — NOT the old M3_CORE_RS_BUILD_FEATURES env var, which the
    crate never read (latent no-op bug in the prior implementation).
    """
    # Pre-flight: check build tools before launching a multi-minute compile
    # that will fail with a cryptic Permission denied buried in 1000+ lines.
    missing = _check_build_tools()
    if missing:
        print(
            f"[rust-core] source build requires: {', '.join(missing)}\n"
            "  cmake + C++:\n"
            "    Debian/Ubuntu:  sudo apt install cmake build-essential\n"
            "    Fedora/RHEL:    sudo dnf install cmake gcc-c++\n"
            "    Arch:           sudo pacman -S cmake base-devel\n"
            "    macOS:          xcode-select --install && brew install cmake\n"
            "  Rust toolchain:\n"
            "    All platforms:  curl --proto '=https' --tlsv1.2 -sSf "
            "https://sh.rustup.rs | sh -s -- -y\n"
            "    Then source the env: source \"$HOME/.cargo/env\"\n"
            "  If cmake/c++/cargo exists but gives 'Permission denied', the\n"
            "  binary is not executable by this user — ask an admin to fix\n"
            "  permissions or install the package for this user's distro.\n"
            "  Embeddings still work without this: Tier-2 HTTP fallback serves\n"
            "  every embed request (no native build needed).",
            file=sys.stderr,
        )
        return 1

    url = (f"m3-core-rs @ git+https://github.com/skynetcmd/m3-core-rs.git"
           f"@{git_tag}#subdirectory=crates/m3-core-py")
    args = ["install", "--force-reinstall", "--no-deps", url]
    feats = choice.features
    if feats:
        # maturin reads build args from --config-settings build-args=...
        args += ["--config-settings", f"build-args=--features {','.join(feats)}"]
    print(f"[rust-core] building from source @ {git_tag} "
          f"(features={feats or '(none)'}); this needs Rust + a compiler")
    return _pip_install_with_pep668_fallback(*args)


def install_rust_core(os_tok: Optional[str] = None, *,
                      allow_source_fallback: bool = True,
                      backend: Optional[str] = None) -> int:
    """Top-level: detect backend, install prebuilt wheel, fall back to source.

    Args:
        os_tok: Override the OS token (windows/linux/macos). Defaults to
            auto-detection via host_os().
        allow_source_fallback: If False, fail instead of building from source
            when no prebuilt wheel matches this platform/Python.
        backend: Explicit backend override (cpu/cuda/vulkan/metal). Skips
            auto-detection entirely. Use when detection picks the wrong backend
            (e.g. Vulkan tools present but no Vulkan GPU).

    Returns 0 on success, non-zero otherwise. Used by the wizard and the
    `m3 embedder install-gpu` CLI command.
    """
    if backend is not None:
        os_tok = os_tok or host_os()
        if (os_tok, backend) not in _VALID:
            valid_backends = [b for o, b in _VALID if o == os_tok]
            print(
                f"[rust-core] invalid backend '{backend}' for {os_tok}. "
                f"Valid options: {', '.join(sorted(valid_backends))}",
                file=sys.stderr,
            )
            return 2
        choice = BackendChoice(os_tok, backend, "explicit --backend override")
        print(f"[rust-core] backend override: {choice.package}")
    else:
        choice = detect_backend(os_tok)
        print(f"[rust-core] detected backend: {choice.package} ({choice.reason})")

    if (choice.os_tok, choice.backend) not in _VALID:
        print(f"[rust-core] unsupported combination "
              f"{choice.os_tok}-{choice.backend}", file=sys.stderr)
        return 2

    # Three-tier install cascade:
    #   1. PyPI prebuilt — fastest path, no toolchain.
    #   2. GitHub Release prebuilt — defensive fallback for size-capped builds
    #      (Linux CUDA wheel is 464 MB, can never go on PyPI) and for any
    #      backend where PyPI is missing this version.
    #   3. Source build — last resort, needs Rust + cmake + C++ + backend SDK.
    rc = install_prebuilt(choice)
    if rc == 0:
        print(f"[rust-core] installed {choice.package} {M3_CORE_RS_VERSION} "
              f"(PyPI prebuilt)")
        return 0

    print(f"[rust-core] PyPI prebuilt unavailable for {choice.package} "
          f"(pip exit {rc}); trying GitHub Release fallback.", file=sys.stderr)
    rc_gh = install_from_github_release(choice)
    if rc_gh == 0:
        print(f"[rust-core] installed {choice.package} {M3_CORE_RS_VERSION} "
              f"(GitHub Release)")
        return 0

    if not allow_source_fallback:
        _print_manual_build_recommendation(choice, pypi_rc=rc, release_rc=rc_gh)
        return rc_gh

    print(f"[rust-core] no prebuilt wheel available for {choice.package} "
          f"(PyPI={rc}, Release={rc_gh}); falling back to source build.",
          file=sys.stderr)
    rc_src = install_from_source(choice)
    if rc_src != 0:
        print(f"[rust-core] source build failed (exit {rc_src}). The CPU "
              f"embedder still serves embeddings; see docs/EMBED_DEPLOYMENT.md.",
              file=sys.stderr)
    return rc_src
