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
# release tag (v2026.05.30 == 3.5.30). Used as the version pin for both the
# prebuilt PyPI install and the source-build fallback.
M3_CORE_RS_VERSION = "3.5.30"
M3_CORE_RS_GIT_TAG = "v2026.05.30"

# Cargo features per backend, mirroring build_wheel.py's _MATRIX (the source
# fallback passes these to maturin via pip's config-settings).
_BACKEND_FEATURES: dict[str, list[str]] = {
    "cpu": [],
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

    # Vulkan: SDK env var (build-time) OR the runtime loader/tools present.
    if os.environ.get("VULKAN_SDK") or shutil.which("vulkaninfo"):
        return BackendChoice(os_tok, "vulkan", "Vulkan runtime/SDK detected")

    return BackendChoice(os_tok, "cpu", "no GPU toolchain detected — CPU build")


def _pip(*args: str, env: Optional[dict] = None) -> int:
    return subprocess.run(
        [sys.executable, "-m", "pip", *args],
        env=env if env is not None else os.environ.copy(),
    ).returncode


def install_prebuilt(choice: BackendChoice, *, version: str = M3_CORE_RS_VERSION) -> int:
    """Try to install the matching prebuilt wheel from PyPI.

    Returns pip's exit code. A non-zero result is the signal to fall back to
    a source build — most commonly because no wheel exists for this host's
    platform/Python yet (pip exits non-zero with "no matching distribution").
    """
    spec = f"{choice.package}=={version}"
    print(f"[rust-core] installing prebuilt wheel: {spec}  ({choice.reason})")
    return _pip("install", "--upgrade", "--only-binary=:all:", spec)


def install_from_source(choice: BackendChoice, *,
                        git_tag: str = M3_CORE_RS_GIT_TAG) -> int:
    """Build m3-core-rs from the git source with the backend's Cargo features.

    Fallback when no prebuilt wheel matches. Requires a Rust toolchain
    (>=1.94) + maturin, and the backend's native toolchain (CUDA/Vulkan/Metal
    + a C/C++ compiler). Features are passed to maturin via pip's PEP 517
    config-settings — NOT the old M3_CORE_RS_BUILD_FEATURES env var, which the
    crate never read (latent no-op bug in the prior implementation).
    """
    url = (f"m3-core-rs @ git+https://github.com/skynetcmd/m3-core-rs.git"
           f"@{git_tag}#subdirectory=crates/m3-core-py")
    args = ["install", "--force-reinstall", "--no-deps", url]
    feats = choice.features
    if feats:
        # maturin reads build args from --config-settings build-args=...
        args += ["--config-settings", f"build-args=--features {','.join(feats)}"]
    print(f"[rust-core] building from source @ {git_tag} "
          f"(features={feats or '(none)'}); this needs Rust + a compiler")
    return _pip(*args)


def install_rust_core(os_tok: Optional[str] = None, *,
                      allow_source_fallback: bool = True) -> int:
    """Top-level: detect backend, install prebuilt wheel, fall back to source.

    Returns 0 on success, non-zero otherwise. Used by the wizard and the
    `m3 install-gpu` CLI command.
    """
    choice = detect_backend(os_tok)
    if (choice.os_tok, choice.backend) not in _VALID:
        print(f"[rust-core] unsupported combination "
              f"{choice.os_tok}-{choice.backend}", file=sys.stderr)
        return 2

    rc = install_prebuilt(choice)
    if rc == 0:
        print(f"[rust-core] installed {choice.package} {M3_CORE_RS_VERSION}")
        return 0

    if not allow_source_fallback:
        print(f"[rust-core] no prebuilt wheel for {choice.package} on this "
              f"platform/Python (pip exit {rc}); source fallback disabled.",
              file=sys.stderr)
        return rc

    print(f"[rust-core] no prebuilt wheel for {choice.package} "
          f"(pip exit {rc}); falling back to source build.", file=sys.stderr)
    rc = install_from_source(choice)
    if rc != 0:
        print(f"[rust-core] source build failed (exit {rc}). The CPU embedder "
              f"still serves embeddings; see docs/EMBED_DEPLOYMENT.md.",
              file=sys.stderr)
    return rc
