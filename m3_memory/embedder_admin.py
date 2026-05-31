"""Install / control the sovereign m3 CPU embedder (BGE-M3, port 8082).

This replaces the prior LM Studio sovereign path (bin/setup_embedder.py).
The CPU embedder is now our own: m3-embed-server binary from m3-core-rs,
serving an OpenAI-compatible /embedding endpoint on port 8082.

Operations:
    install   — locate GGUF model, register as OS service, start.
    start     — start the OS service (assumes already installed).
    stop      — stop the OS service.
    status    — query service status.
    uninstall — remove the OS service registration.
    install-gpu — build m3-core-rs with the appropriate embedded-<gpu> feature.

The BGE-M3 GGUF (~438MB) ships with the repo via Git LFS under
`_assets/models/bge-m3-Q4_K_M.gguf`. The installer locates it via the
m3-memory payload root (set by `install-m3`); no network fetch.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

# ── locations ─────────────────────────────────────────────────────────────────

def _m3_root() -> Path:
    root = os.environ.get("M3_MEMORY_ROOT")
    if root:
        return Path(root).expanduser().resolve()
    return Path.home() / ".m3-memory"


# ── BGE-M3 GGUF discovery ────────────────────────────────────────────────────

# Q4_K_M is the sovereign default: ~438 MB, BGE-M3 quality intact, runs on
# CPU at ~30-80 emb/sec on modern hardware. Shipped with the repo via Git LFS.
BGE_M3_FILENAME = "bge-m3-Q4_K_M.gguf"


def _find_bundled_gguf() -> Optional[Path]:
    """Locate the LFS-bundled GGUF inside the m3-memory payload.

    Resolution order:
      1. $M3_EMBED_GGUF (explicit override — still honored for advanced users)
      2. <payload>/_assets/models/bge-m3-Q4_K_M.gguf  (LFS-tracked, the new default)
      3. Walk up from this file looking for `_assets/models/<filename>`
         (developer case: `pip install -e .` from a clone)
    """
    env_path = os.environ.get("M3_EMBED_GGUF")
    if env_path and Path(env_path).is_file():
        return Path(env_path)

    # 2. Resolve via the install-m3 payload root.
    try:
        from m3_memory.installer import find_bridge
        bridge = find_bridge()
        if bridge:
            # bridge points at <payload>/bin/memory_bridge.py; up two = payload root.
            candidate = bridge.parent.parent / "_assets" / "models" / BGE_M3_FILENAME
            if candidate.is_file():
                return candidate
    except Exception:
        pass

    # 3. Developer fallback — walk up from this file.
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        candidate = parent / "_assets" / "models" / BGE_M3_FILENAME
        if candidate.is_file():
            return candidate

    return None


def _gguf_size_bytes(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _is_lfs_pointer(p: Path) -> bool:
    """LFS pointer files are tiny (<200 bytes) and start with `version`."""
    try:
        if p.stat().st_size > 1024:
            return False
        head = p.read_bytes()[:200]
        return head.startswith(b"version https://git-lfs")
    except OSError:
        return False


# ── m3-embed-server binary discovery ─────────────────────────────────────────

def _server_binary() -> Optional[Path]:
    """Locate the m3-embed-server binary.

    Resolution order:
      1. $M3_EMBED_SERVER_BIN (explicit override)
      2. Inside the m3_core_rs Python wheel (installed as the `oxidation` extra)
      3. PATH lookup for `m3-embed-server`
    """
    env_bin = os.environ.get("M3_EMBED_SERVER_BIN")
    if env_bin and Path(env_bin).is_file():
        return Path(env_bin)

    try:
        import m3_core_rs  # type: ignore
        wheel_dir = Path(m3_core_rs.__file__).parent
        exe_name = "m3-embed-server.exe" if sys.platform == "win32" else "m3-embed-server"
        for candidate in (
            wheel_dir / exe_name,
            wheel_dir / "bin" / exe_name,
            wheel_dir.parent / exe_name,
        ):
            if candidate.is_file():
                return candidate
    except ImportError:
        pass

    on_path = shutil.which("m3-embed-server")
    return Path(on_path) if on_path else None


def _service_cmd(binary: Path, gguf: Path, sub: str, *extra: str) -> int:
    """Run `<binary> <sub> [extra...]` with the GGUF path in env."""
    env = os.environ.copy()
    env.setdefault("M3_EMBED_GGUF", str(gguf))
    env.setdefault("M3_EMBED_SERVER_PORT", "8082")
    return subprocess.run([str(binary), sub, *extra], env=env, check=False).returncode


def _locate_gguf_or_explain() -> Optional[Path]:
    """Locate the bundled GGUF; print actionable guidance if not found."""
    gguf = _find_bundled_gguf()
    if gguf and not _is_lfs_pointer(gguf):
        return gguf
    if gguf and _is_lfs_pointer(gguf):
        print(
            f"Error: {gguf} is an LFS pointer, not the actual model file.\n"
            "  Pull LFS-tracked assets:\n"
            "    cd <m3-memory repo>; git lfs install; git lfs pull\n"
            "  Or set M3_EMBED_GGUF to a hand-downloaded GGUF.",
            file=sys.stderr,
        )
        return None
    print(
        f"Error: bundled GGUF not found ({BGE_M3_FILENAME}).\n"
        "  Expected location: <payload>/_assets/models/" + BGE_M3_FILENAME + "\n"
        "  If you installed via `pip install m3-memory`, run `m3 install-m3` first.\n"
        "  If you installed from source, run `git lfs install; git lfs pull`.",
        file=sys.stderr,
    )
    return None


# ── subcommands ───────────────────────────────────────────────────────────────

def cmd_install(args: argparse.Namespace) -> int:
    """End-to-end CPU embedder install: locate GGUF + register service + start."""
    binary = _server_binary()
    if not binary:
        print(
            "Error: m3-embed-server binary not found.\n"
            "  Install m3-core-rs from git (Rust >=1.94 + maturin required):\n"
            "    pip install 'm3-core-rs @ git+https://github.com/skynetcmd/m3-core-rs.git@v0.9.0#subdirectory=crates/m3-core-py'\n"
            "  Or set M3_EMBED_SERVER_BIN to point at a prebuilt binary.",
            file=sys.stderr,
        )
        return 1

    gguf = _locate_gguf_or_explain()
    if not gguf:
        return 2
    size_mb = _gguf_size_bytes(gguf) // (1024 * 1024)
    print(f"[=] using bundled GGUF: {gguf} ({size_mb} MB)")

    print(f"[~] registering m3-embed-server (concurrency={args.concurrency})")
    extra: list[str] = []
    if args.concurrency:
        extra += ["--concurrency", str(args.concurrency)]
    rc = _service_cmd(binary, gguf, "install", *extra)
    if rc != 0:
        print(f"[!] `m3-embed-server install` exited {rc}", file=sys.stderr)
        return rc

    print("[~] starting m3-embed-server")
    rc = _service_cmd(binary, gguf, "start")
    if rc != 0:
        print(f"[!] `m3-embed-server start` exited {rc}", file=sys.stderr)
        return rc

    print("[OK] sovereign CPU embedder running on port 8082")
    return 0


def _binary_and_gguf_or_fail() -> Optional[tuple[Path, Path]]:
    binary = _server_binary()
    if not binary:
        print("Error: m3-embed-server not installed. Run `m3 embedder install`.", file=sys.stderr)
        return None
    # For start/stop/status/uninstall the service config already has the GGUF
    # baked in via `install`, so a missing GGUF here is non-fatal. Pass a
    # placeholder if needed; the binary ignores env when the service config
    # exists.
    gguf = _find_bundled_gguf() or Path("/")
    return binary, gguf


def cmd_start(args: argparse.Namespace) -> int:
    pair = _binary_and_gguf_or_fail()
    if not pair: return 1
    return _service_cmd(*pair, "start")


def cmd_stop(args: argparse.Namespace) -> int:
    pair = _binary_and_gguf_or_fail()
    if not pair: return 1
    return _service_cmd(*pair, "stop")


def cmd_status(args: argparse.Namespace) -> int:
    pair = _binary_and_gguf_or_fail()
    if not pair: return 1
    return _service_cmd(*pair, "status")


def cmd_uninstall(args: argparse.Namespace) -> int:
    pair = _binary_and_gguf_or_fail()
    if not pair: return 1
    return _service_cmd(*pair, "uninstall")


def cmd_install_gpu(args: argparse.Namespace) -> int:
    """Install the m3-core-rs Rust core with GPU acceleration for this host.

    Detects the backend (macOS->Metal, NVIDIA->CUDA, Vulkan->Vulkan, else CPU)
    and installs the matching prebuilt wheel from PyPI
    (``m3-core-rs-<os>-<backend>``), falling back to a from-source build only
    when no prebuilt wheel exists for this platform/Python.

    Backend detection and install logic live in ``rust_core_install`` so the
    setup wizard and this command share one code path; that module also holds
    the (os, backend) -> package-name mapping that mirrors the m3-core-rs
    ``build_wheel.py`` used to publish the wheels.
    """
    from m3_memory import rust_core_install

    allow_source = not getattr(args, "no_source_fallback", False)
    rc = rust_core_install.install_rust_core(allow_source_fallback=allow_source)
    if rc == 0:
        print("[OK] m3-core-rs installed; restart any running embedder service.")
    return rc


# ── argparse wiring ───────────────────────────────────────────────────────────

def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Add `m3 embedder <sub>` subcommands to an argparse subparser."""
    sub = parser.add_subparsers(dest="embedder_cmd", metavar="<subcommand>")

    p_install = sub.add_parser(
        "install",
        help="Install the sovereign CPU embedder (BGE-M3 on port 8082) as an OS service.",
    )
    p_install.add_argument(
        "--concurrency", type=int, default=2,
        help="Max concurrent embed requests (default: 2). Higher = more RAM.",
    )
    p_install.set_defaults(func=cmd_install)

    p_install_gpu = sub.add_parser(
        "install-gpu",
        help="Install the GPU-accelerated Rust core (CUDA/Vulkan/Metal autodetected); "
             "prebuilt wheel from PyPI, source build fallback.",
    )
    p_install_gpu.add_argument(
        "--no-source-fallback", action="store_true",
        help="Fail instead of building from source when no prebuilt wheel matches "
             "this platform/Python.",
    )
    p_install_gpu.set_defaults(func=cmd_install_gpu)

    p_start = sub.add_parser("start", help="Start the CPU embedder service.")
    p_start.set_defaults(func=cmd_start)

    p_stop = sub.add_parser("stop", help="Stop the CPU embedder service.")
    p_stop.set_defaults(func=cmd_stop)

    p_status = sub.add_parser("status", help="Show the CPU embedder service status.")
    p_status.set_defaults(func=cmd_status)

    p_uninstall = sub.add_parser("uninstall", help="Remove the CPU embedder service registration.")
    p_uninstall.set_defaults(func=cmd_uninstall)
