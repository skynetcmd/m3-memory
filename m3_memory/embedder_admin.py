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


def _embed_server_port() -> int:
    """The configured tier-2 embed-server port (M3_EMBED_SERVER_PORT, def 8082)."""
    try:
        return int(os.environ.get("M3_EMBED_SERVER_PORT", "8082"))
    except ValueError:
        return 8082


def _port_in_use(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    """True if something is already accepting TCP connections on host:port.

    A quick connect probe — used to warn the operator that an embed server (or
    some other process) is already listening before we (re)start one, so a second
    instance doesn't silently fail to bind."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _warn_if_port_busy(action: str) -> None:
    """Print a heads-up if the embed-server port is already in use. Non-fatal —
    the underlying service manager owns the real start/stop; this just makes an
    already-running instance visible instead of a confusing bind failure."""
    port = _embed_server_port()
    if _port_in_use(port):
        print(f"[i] a process is already listening on port {port} — an m3-embed-server "
              f"may already be running. `{action}` will hand off to the service "
              "manager, which is idempotent; use `m3 embedder status` to check.")


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
        # Detect whether a prebuilt wheel is likely to exist for this platform
        # so we can give an actionable next step rather than a raw git URL.
        try:
            from m3_memory.rust_core_install import detect_backend
            choice = detect_backend()
            prebuilt_hint = f"m3-core-rs-{choice.os_tok}-{choice.backend}"
        except Exception:
            prebuilt_hint = None

        has_cargo = shutil.which("cargo") is not None

        lines = [
            "Error: m3-embed-server binary not found.",
            "",
            "  Fix: run `m3 embedder install-gpu` — it installs the right prebuilt",
            "  wheel from PyPI automatically (no Rust toolchain required for CPU):",
            "    m3 embedder install-gpu",
        ]
        if prebuilt_hint:
            lines.append(f"  (will install: {prebuilt_hint})")
        if not has_cargo:
            lines += [
                "",
                "  NOTE: Rust/cargo is NOT installed on this machine. The prebuilt",
                "  wheel path above does not require it. Only a from-source build does.",
            ]
        lines += [
            "",
            "  Alternative: set M3_EMBED_SERVER_BIN to point at a prebuilt binary.",
            "",
            "  NOTE: Tier-2 (this service) is optional. Tier-1 in-process GGUF",
            "  embedding is already active and fully functional — m3 works without",
            "  this service. Install it only for faster cold-start performance.",
        ]
        print("\n".join(lines), file=sys.stderr)
        return 1

    gguf = _locate_gguf_or_explain()
    if not gguf:
        return 2
    size_mb = _gguf_size_bytes(gguf) // (1024 * 1024)
    print(f"[=] using bundled GGUF: {gguf} ({size_mb} MB)")

    _warn_if_port_busy("install")
    print(f"[~] registering m3-embed-server (concurrency={args.concurrency})")
    extra: list[str] = []
    if args.concurrency:
        extra += ["--concurrency", str(args.concurrency)]
    rc = _service_cmd(binary, gguf, "install", *extra)
    if rc != 0:
        print(
            f"[!] `m3-embed-server install` exited {rc}\n"
            "  This usually means systemd --user is unavailable (container, SSH session,\n"
            "  or system without a D-Bus user session).\n"
            "  You can run the embed server directly instead:\n"
            f"    M3_EMBED_GGUF={gguf} nohup m3-embed-server > ~/.m3/engine/embed-server.log 2>&1 &\n"
            "  To start it automatically on boot, add to crontab (crontab -e):\n"
            f"    @reboot M3_EMBED_GGUF={gguf} m3-embed-server >> ~/.m3/engine/embed-server.log 2>&1 &\n"
            "  Tier-1 in-process GGUF is active and sufficient — this step is optional.",
            file=sys.stderr,
        )
        return rc

    print("[~] starting m3-embed-server")
    rc = _service_cmd(binary, gguf, "start")
    if rc != 0:
        print(
            f"[!] `m3-embed-server start` exited {rc}\n"
            "  If systemd --user is unavailable, start the server directly:\n"
            f"    M3_EMBED_GGUF={gguf} nohup m3-embed-server > ~/.m3/engine/embed-server.log 2>&1 &",
            file=sys.stderr,
        )
        return rc

    print("[OK] sovereign CPU embedder running on port 8082")
    return 0


def _binary_and_gguf_or_fail() -> Optional[tuple[Path, Path]]:
    binary = _server_binary()
    if not binary:
        print(
            "Error: m3-embed-server not installed.\n"
            "  Run `m3 embedder install-gpu` to install it (prebuilt wheel, no Rust needed for CPU).\n"
            "  Tier-1 in-process GGUF embedding is active and sufficient — this service is optional.",
            file=sys.stderr,
        )
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
    _warn_if_port_busy("start")
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
    backend = getattr(args, "backend", None) or None
    force = getattr(args, "force", False)
    rc = rust_core_install.install_rust_core(
        allow_source_fallback=allow_source,
        backend=backend,
        force=force,
    )
    if rc == 0:
        print("[OK] m3-core-rs installed; restart any running embedder service.")
    return rc


# ── argparse wiring ───────────────────────────────────────────────────────────

def _print_stop_proc_hint(script_name: str) -> None:
    """Print the platform-appropriate one-liner to find + stop a python process
    running `script_name`, so a user isn't left guessing how to 'restart the loop'."""
    if sys.platform == "win32":
        print("         Find + stop it (PowerShell):")
        print("           Get-CimInstance Win32_Process | ? { $_.CommandLine -like "
              f"'*{script_name}*' }} | ForEach {{ Stop-Process -Id $_.ProcessId -Force }}")
    else:
        print(f"           pkill -f {script_name}   # find + stop it")


def _embed_config_path() -> str:
    """<config_root>/.embed_config.json — read at import by bin/memory/embed.py.

    Resolves the config root the same way m3_core.paths.get_m3_config_root does:
    M3_CONFIG_ROOT > M3_MEMORY_ROOT/config > ~/.m3/config. Kept dependency-free
    (no m3_sdk import) so this CLI helper works from a bare package install."""
    root = os.environ.get("M3_CONFIG_ROOT")
    if not root:
        mem_root = os.environ.get("M3_MEMORY_ROOT")
        root = (os.path.join(os.path.abspath(os.path.expanduser(mem_root)), "config")
                if mem_root else os.path.join(os.path.expanduser("~"), ".m3", "config"))
    return os.path.join(root, ".embed_config.json")


def cmd_shared(args: argparse.Namespace) -> int:
    """Route ALL m3 processes to ONE shared GPU embedder (one CUDA context).

    Writes <config_root>/.embed_config.json so every m3 process (MCP server,
    cognitive loop) disables its OWN in-process embedder and defers to a single
    shared server (bin/embed_server_inproc.py) over localhost HTTP. This reclaims
    ~9-10 GB on a box where several processes would otherwise each open their own
    CUDA context (contexts can't cross process boundaries — the only way to load
    the GPU model once is one owner + thin clients). Localhost HTTP overhead is
    <2% of the ~10-31 ms GPU embed.

    After running this, (re)start the shared server and restart the m3 processes:
      - the AgentOS_EmbedServer scheduled task (install_schedules.py) runs it on
        boot; or start it manually: `python bin/embed_server_inproc.py --port 8082`.
      - restart the MCP server + cognitive loop so they re-read the config."""
    import json
    port = getattr(args, "port", 8082) or 8082
    url = f"http://127.0.0.1:{port}"
    cfg = {
        "disable_inproc_embedder": True,
        "fallback_url": url,
        "_comment": ("Route all m3 processes to the shared GPU embedder "
                     "(bin/embed_server_inproc.py) so only ONE CUDA context exists. "
                     "Written by `m3 embedder shared`. Revert with `m3 embedder unshared`."),
    }
    path = _embed_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    print(f"[OK] wrote {path}")
    print(f"     -> in-process embedder DISABLED; clients defer to {url}")
    print("\nThe config is read at process START, so already-running m3 processes")
    print("keep their OWN embedder until restarted. Do these THREE steps:\n")
    print(f"  1. Start the shared server (the SOLE GPU embedder, on {url}):")
    if sys.platform == "win32":
        print("       Installed as the AgentOS_EmbedServer scheduled task by:")
        print("           python bin/install_schedules.py --repair")
        print("       ^ run from an ELEVATED (Administrator) shell — the ONSTART")
        print("         task registration fails with 'Access is denied' otherwise.")
        print("       Or start it directly for this session:")
        print(f"           python bin/embed_server_inproc.py --port {port}")
    else:
        print(f"           python bin/embed_server_inproc.py --port {port}")
        print("       (or wire a launchd/systemd unit so it starts on boot).")
    print("\n  2. Restart the m3 processes so they DROP their own embedder:")
    print("       - Cognitive loop: stop the running m3_cognitive_loop.py, then")
    print("         let its scheduled task / your launcher relaunch it.")
    _print_stop_proc_hint("m3_cognitive_loop.py")
    print("       - MCP memory server: restart it. In Claude Code, killing it DROPS")
    print("         the client connection — run `/mcp` to reconnect afterward (a")
    print("         plain reconnect alone does NOT reload code; the process must restart).")
    print("\n  3. Verify:  m3 doctor   (reports shared mode + server health), or")
    print(f"              curl {url}/health   ->  {{\"status\":\"ok\"}}")
    return 0


def cmd_unshared(args: argparse.Namespace) -> int:
    """Revert to per-process in-process embedders (remove .embed_config.json).

    Each m3 process goes back to loading its OWN GPU embedder (more RAM, but no
    dependency on a shared server). Restart the m3 processes after."""
    path = _embed_config_path()
    if os.path.exists(path):
        os.remove(path)
        print(f"[OK] removed {path} — processes will use their own in-process embedder again.")
    else:
        print(f"[~] {path} not present — already unshared (per-process embedders).")
    print("     Restart the MCP server + cognitive loop to apply.")
    return 0


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
    p_install_gpu.add_argument(
        "--backend", choices=["cpu", "cuda", "vulkan", "metal"], default=None,
        help="Override backend detection (cpu/cuda/vulkan/metal). Use when "
             "auto-detection picks the wrong backend — e.g. Vulkan tools are "
             "installed system-wide but no Vulkan GPU is present, so pass "
             "--backend cpu to force the CPU prebuilt wheel.",
    )
    p_install_gpu.add_argument(
        "--force", action="store_true",
        help="Reinstall even if the target m3-core-rs version is already present "
             "(default: skip the re-download when already current).",
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

    p_shared = sub.add_parser(
        "shared",
        help="Route all m3 processes to ONE shared GPU embedder (one CUDA context, "
             "~9-10 GB reclaimed). Writes .embed_config.json; then run the shared "
             "server + restart the MCP server & cognitive loop.",
    )
    p_shared.add_argument("--port", type=int, default=8082,
                          help="Port the shared embedder server listens on (default: 8082).")
    p_shared.set_defaults(func=cmd_shared)

    p_unshared = sub.add_parser(
        "unshared",
        help="Revert to per-process in-process embedders (remove .embed_config.json).",
    )
    p_unshared.set_defaults(func=cmd_unshared)
