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


def _is_sovereign_path(p: Path) -> bool:
    """True if `p` lives under a model root m3 owns (~/.m3/models by default).

    "Sovereign" means the file survives the user uninstalling LM Studio, Ollama,
    or llama.cpp — the whole point of M3_MODELS_ROOT. A configured path already
    inside it needs no migration.
    """
    roots: list[Path] = []
    fn = _import_model_fetch("m3_models_dir")
    if fn is not None:
        try:
            roots.append(Path(fn()))
        except Exception:  # noqa: BLE001 — advisory
            pass
    if not roots:  # model_fetch unimportable (bare install): resolve inline.
        env = os.environ.get("M3_MODELS_ROOT")
        mem = os.environ.get("M3_MEMORY_ROOT")
        if env:
            roots.append(Path(os.path.expanduser(env)))
        elif mem:
            roots.append(Path(os.path.expanduser(mem)) / "models")
        else:
            roots.append(Path.home() / ".m3" / "models")
    for root in roots:
        try:
            p.resolve().relative_to(root.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def _migrate_embed_config_gguf(new: Path, *, previous: Path) -> None:
    """Repoint the shared embedder config from `previous` to `new`, preserving
    every other key.

    Only called when a config already names a NON-sovereign model, so it never
    creates a config from nothing (that is seed_shared_config's job) and never
    fires under M3_EMBED_GGUF_AUTODETECT=0.

    Advisory and never fatal: resolution already has its answer by the time this
    runs, so a read-only or malformed config must not break `install`. Silent
    when there is nothing to change; announces a real switch, because quietly
    repointing a user's configured model would be a surprise (§3 fail loud).
    """
    if previous == new:
        return
    try:
        import json

        path = Path(_embed_config_path())
        data: dict = {}
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        if (data.get("gguf_path") or "").strip() == str(new):
            return
        data["gguf_path"] = str(new)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        print(f"[~] embedder model migrated to m3's own copy: {new}")
        print(f"    (was: {previous} — a third-party path that could vanish)")
    except Exception:  # noqa: BLE001 — config is advisory; resolution stands
        pass


def _find_bundled_gguf() -> Optional[Path]:
    """Locate the LFS-bundled GGUF inside the m3-memory payload.

    Resolution order:
      1.  $M3_EMBED_GGUF (explicit override — still honored for advanced users)
      1b. The shared embedder config's `gguf_path`, IF it is already sovereign
          (under ~/.m3/models). A third-party path is remembered but does not
          win outright — see 2.
      2.  m3's OWN model dir (~/.m3/models). Beats a third-party configured
          path and rewrites the config to match, so ditching LM Studio / Ollama
          / llama.cpp cannot strand the installer.
      2b. Otherwise the third-party configured path from 1b — preferred over
          failing when m3 owns no copy.
      3.  <payload>/_assets/models/bge-m3-Q4_K_M.gguf  (LFS-tracked)
      4.  Walk up from this file looking for `_assets/models/<filename>`
          (developer case: `pip install -e .` from a clone)
    """
    env_path = os.environ.get("M3_EMBED_GGUF")
    if env_path and Path(env_path).is_file():
        return Path(env_path)

    # 1b. The SHARED embedder config. Setup's GGUF auto-discovery finds any
    # bge-m3 GGUF on the machine (an LM Studio download, a hand-placed file)
    # and writes its path here — but this resolver used to check only the
    # BUNDLED filename in the payload's _assets dir, so a perfectly good,
    # already-configured model was reported missing.
    #
    # The user-visible contradiction, in ONE setup run (2026-07-27):
    #     discovered BGE-M3 GGUF: .../bge-m3-GGUF-Q4_K_M.gguf
    #     Use this GGUF for the shared embedder? [Y/n]      -> yes, config written
    #     ...
    #     the optional CPU-embedder GGUF (bge-m3-Q4_K_M.gguf) isn't present
    # It accepted the model, then skipped the embedder install as if none
    # existed. Two resolvers with different filenames and no shared state.
    # Honour the same opt-out the other resolvers use (memory/doctor.py,
    # memory/embed.py): M3_EMBED_GGUF_AUTODETECT=0 means "use only what I named
    # explicitly", so a discovered-and-configured model must not sneak in.
    # Without this the flag silently stopped meaning what it says.
    #
    # SOVEREIGNTY MIGRATION (2026-07-27, .10). Two defects showed up on a real
    # `.9` upgrade, both because a config written BEFORE ~/.m3/models existed
    # outranked m3's own copy forever:
    #   (a) If the configured path is deleted (the user ditches LM Studio — the
    #       exact scenario ~/.m3/models was built for), this returned nothing
    #       useful and install broke, while a perfectly good model sat unused.
    #   (b) A `.8`-era config pinned to a third-party path meant only FRESH
    #       installs ever got sovereignty; every upgrader silently kept
    #       depending on a tool they may uninstall tomorrow.
    # So: an m3-owned copy wins over a third-party configured path, and the
    # config is rewritten to match. An explicitly configured path that lives
    # UNDER a model root we own is already sovereign and is left alone, as is
    # $M3_EMBED_GGUF (handled above) and autodetect=0.
    configured: Optional[Path] = None
    try:
        import json

        if os.environ.get("M3_EMBED_GGUF_AUTODETECT", "1") == "0":
            raise RuntimeError("autodetect disabled")
        cfg = Path(_embed_config_path())
        if cfg.is_file():
            raw = (json.loads(cfg.read_text(encoding="utf-8"))
                   .get("gguf_path") or "").strip()
            if raw and Path(raw).is_file():
                configured = Path(raw)
                if _is_sovereign_path(configured):
                    return configured
    except Exception:  # noqa: BLE001 — config is advisory; fall through
        configured = None

    # 2. m3's OWN model dir (~/.m3/models) — the copy that survives the user
    # uninstalling LM Studio / Ollama / llama.cpp, and where fetch-model writes.
    own = _import_model_fetch("existing_m3_gguf")
    if own is not None:
        try:
            found = own()
            if found:
                # Prefer it over a third-party configured path, and record the
                # switch so every later resolver agrees (doctor, embed, setup).
                # Only when something WAS configured: with no config there is
                # nothing to migrate away from, and writing one here would fire
                # even under M3_EMBED_GGUF_AUTODETECT=0 — which skips the read
                # above and so leaves `configured` None. Seeding the config is
                # seed_shared_config's job, not a resolver's side effect.
                if configured is not None and Path(found) != configured:
                    _migrate_embed_config_gguf(found, previous=configured)
                return found
        except Exception:  # noqa: BLE001 — advisory; fall through
            pass

    # 2b. No m3-owned copy: honour the third-party configured path rather than
    # failing. Sovereignty is the preference, not a hard requirement.
    if configured is not None:
        return configured

    # 3. Resolve via the install-m3 payload root.
    try:
        from m3_memory.installer import assets_dir
        assets = assets_dir()
        if assets:
            candidate = assets / "models" / BGE_M3_FILENAME
            if candidate.is_file():
                return candidate
    except Exception:
        pass

    # 4. Developer fallback — walk up from this file.
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


def _service_reports_installed(binary: Path, gguf: Path) -> bool:
    """True if the service manager says the service exists. `status` prints
    'running'/'stopped' when registered and 'not installed' otherwise, so parse
    its stdout rather than trusting the exit code alone."""
    env = os.environ.copy()
    env.setdefault("M3_EMBED_GGUF", str(gguf))
    env.setdefault("M3_EMBED_SERVER_PORT", "8082")
    try:
        out = subprocess.run(
            [str(binary), "status"], env=env, check=False,
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    blob = f"{out.stdout}\n{out.stderr}".lower()
    if "not installed" in blob:
        return False
    return "running" in blob or "stopped" in blob


def _service_binary_is_stale(binary: Path) -> bool:
    """True if a running embed-server process started BEFORE its own binary was
    last written — i.e. an upgrade replaced the executable on disk while the
    service kept executing the old image.

    Windows keeps the original file mapped after a replace, so the SCM path and
    the wheel path can be the same file yet the process still runs old code.
    Comparing process start time to the binary mtime is the portable signal.
    Returns False when psutil is unavailable or nothing matches — never guess
    'stale' and trigger a needless restart.

    Windows caveat (2026-07-27): the embed server runs as a LocalSystem service,
    and an *unelevated* query cannot read another user's image path — psutil's
    `exe` comes back empty (so does WMI's ExecutablePath). Matching only on the
    resolved path therefore found nothing and returned False on the exact
    machine state this check exists to catch: a `.9` upgrade left the pre-upgrade
    process serving stale code and the restart never fired. `create_time` IS
    readable unelevated, so when `exe` is blank we fall back to matching on the
    process NAME. That is a weaker identity check, so it is used only as a
    fallback — a readable, non-matching `exe` still disqualifies the process.
    """
    try:
        import psutil  # type: ignore
    except ImportError:
        return False
    try:
        bin_mtime = binary.stat().st_mtime
        target = binary.resolve()
    except OSError:
        return False
    # "m3-embed-server.exe" -> "m3-embed-server"; Linux/macOS already match.
    target_stem = target.stem.lower()
    for proc in psutil.process_iter(["name", "exe", "create_time"]):
        try:
            exe = proc.info.get("exe")
            if exe:
                if Path(exe).resolve() != target:
                    continue
            else:
                # Unreadable image path — fall back to name identity.
                name = (proc.info.get("name") or "").strip().lower()
                if not name or Path(name).stem != target_stem:
                    continue
            # 5s grace: mtime and start time can land within the same tick of a
            # legitimate fresh start.
            create_time = proc.info.get("create_time")
            if create_time is not None and create_time < bin_mtime - 5:
                return True
        except (psutil.Error, OSError):
            continue
    return False


def _install_failure_hint(gguf: Path) -> str:
    """OS-correct recovery advice. The previous text printed systemd/nohup/
    crontab unconditionally — none of which exist on Windows, where the
    embedder registers as a Windows Service instead."""
    if sys.platform == "win32":
        return (
            "  Registering the Windows Service needs Administrator rights.\n"
            "  Open an *Administrator* terminal and run:\n"
            "    m3 embedder install\n"
            "  Check what SCM currently thinks:  m3 embedder status"
        )
    if sys.platform == "darwin":
        return (
            "  This usually means the launchd user domain is unavailable (SSH session\n"
            "  with no GUI login). Run the server directly instead:\n"
            f"    M3_EMBED_GGUF={gguf} nohup m3-embed-server > ~/.m3/engine/embed-server.log 2>&1 &"
        )
    return (
        "  This usually means systemd --user is unavailable (container, SSH session,\n"
        "  or system without a D-Bus user session).\n"
        "  You can run the embed server directly instead:\n"
        f"    M3_EMBED_GGUF={gguf} nohup m3-embed-server > ~/.m3/engine/embed-server.log 2>&1 &\n"
        "  To start it automatically on boot, add to crontab (crontab -e):\n"
        f"    @reboot M3_EMBED_GGUF={gguf} m3-embed-server >> ~/.m3/engine/embed-server.log 2>&1 &"
    )


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
    # No GGUF anywhere — offer to fetch m3's own copy into ~/.m3/models. This is
    # the only model location that survives the user dropping LM Studio/Ollama,
    # so it is where a downloaded model belongs.
    fetched = _offer_model_download()
    if fetched:
        return fetched

    print(
        f"Note: the optional CPU-embedder GGUF ({BGE_M3_FILENAME}) isn't present, so\n"
        "  the always-on :8082 server can't be installed. This is NOT a failure —\n"
        "  m3 embeds via its in-process / HTTP tiers regardless.\n"
        "  To add it later:\n"
        "    • m3 embedder fetch-model     (downloads ~418 MB into ~/.m3/models)\n"
        "    • or point M3_EMBED_GGUF at a hand-downloaded bge-m3 GGUF,\n"
        "  then re-run `m3 embedder install`.",
        file=sys.stderr,
    )
    return None


def _offer_model_download() -> Optional[Path]:
    """Ask (default YES) then download BGE-M3 into ~/.m3/models.

    Non-interactive runs download without prompting: the user asked for the
    model to be the default outcome, and a headless install that silently ends
    up with no embedder is the worse failure. M3_NO_MODEL_DOWNLOAD=1 opts out.
    """
    fetch_bge_m3 = _import_model_fetch("fetch_bge_m3")
    if fetch_bge_m3 is None:
        return None

    if os.environ.get("M3_NO_MODEL_DOWNLOAD", "") == "1":
        return None

    prompt = ("  No BGE-M3 model found on this machine.\n"
              "  Download it (~418 MB) into ~/.m3/models so m3 owns its own copy? [Y/n] ")
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            answer = input(prompt).strip().lower()
            if answer in ("n", "no"):
                return None
    except (EOFError, KeyboardInterrupt, ValueError):
        pass  # no console: fall through and fetch (the default outcome)

    return fetch_bge_m3()


# ── subcommands ───────────────────────────────────────────────────────────────

def cmd_fetch_model(args: argparse.Namespace) -> int:
    """Download the BGE-M3 GGUF into m3's own model directory."""
    fetch_bge_m3 = _import_model_fetch("fetch_bge_m3")
    m3_models_dir = _import_model_fetch("m3_models_dir")
    if fetch_bge_m3 is None or m3_models_dir is None:
        print("Error: could not load the model fetcher (bin/memory/model_fetch.py).",
              file=sys.stderr)
        return 1

    dest = Path(args.dest).expanduser() if getattr(args, "dest", None) else m3_models_dir()
    path = fetch_bge_m3(dest)
    if not path:
        return 1
    print(f"[OK] {path}")
    print("  Next: `m3 embedder install` to register the shared :8082 server.")
    return 0



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
        # An already-registered service is NOT a failure. Older embed-server
        # builds error out of `install` with an opaque winapi/IO message when
        # the service exists, so ask the service manager what is actually true
        # before reporting anything. (2026-07-27: setup declared the embedder
        # "SKIPPED (not installed)" while it was Automatic, running, and
        # serving :8082.)
        if _service_reports_installed(binary, gguf):
            print("[OK] m3-embed-server already registered — install is a no-op")
            # An upgrade REPLACES the binary on disk, but the running service
            # keeps executing the old image until it is restarted. Leaving it
            # alone silently pairs a new payload with a stale embed server, so
            # bounce it whenever the on-disk binary is newer than the process.
            if _service_binary_is_stale(binary):
                print("[~] on-disk binary is newer than the running service — restarting it")
                _service_cmd(binary, gguf, "stop")
                if _service_cmd(binary, gguf, "start") == 0:
                    print(f"[OK] sovereign CPU embedder restarted on the new binary "
                          f"(port {_embed_server_port()})")
                    return 0
                print("[!] restart failed — the service is still on the OLD binary.\n"
                      f"{_install_failure_hint(gguf)}", file=sys.stderr)
                return rc
            if _port_in_use(_embed_server_port()):
                print(f"[OK] sovereign CPU embedder already serving on port {_embed_server_port()}")
                return 0
            print("[~] starting the existing service")
            if _service_cmd(binary, gguf, "start") == 0:
                print(f"[OK] sovereign CPU embedder running on port {_embed_server_port()}")
                return 0
        print(
            f"[!] `m3-embed-server install` exited {rc}\n"
            f"{_install_failure_hint(gguf)}\n"
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


def _import_model_fetch(name: str):
    """Pull `name` out of bin/memory/model_fetch.py, or None.

    Two on-disk layouts must both work (the bug that kept the dashboard down for
    three releases): DEV has `repo/bin` BESIDE `repo/m3_memory`, while an
    INSTALLED payload has `m3_memory/bin` INSIDE the package. Probe both instead
    of assuming either. Returns None rather than raising — a missing fetcher
    must degrade to "no download offered", never abort setup.
    """
    import importlib

    here = Path(__file__).resolve().parent
    for bin_dir in (here / "bin", here.parent / "bin"):
        if not (bin_dir / "memory" / "model_fetch.py").is_file():
            continue
        if str(bin_dir) not in sys.path:
            sys.path.insert(0, str(bin_dir))
        try:
            mod = importlib.import_module("memory.model_fetch")
            return getattr(mod, name, None)
        except Exception:  # noqa: BLE001 - optional path, never fatal
            return None
    return None


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


def seed_shared_config(
    config_root: str | None = None,
    *,
    gguf_path: str | None = None,
    port: int = 8082,
    overwrite: bool = False,
) -> tuple[str, bool]:
    """Idempotently seed <config_root>/.embed_config.json for SHARED mode.

    The single source of truth for what the shared-embedder config looks like, so
    the installer, updater, setup wizard, `m3 embedder shared`, and `m3 doctor
    --fix` all write an identical, correct file (§4 DRY; §3 idempotent seed).

    - Sets ``disable_inproc_embedder: true`` so every client defers to the shared
      server instead of opening its own CUDA context.
    - Records ``fallback_url`` (where the shared server listens) and, when known,
      ``gguf_path`` (which model the SERVER should load — clients never load it).
    - overwrite=False (default) preserves an existing file's keys, only filling in
      what's missing, so a hand-tuned config is never clobbered.

    Returns (path, wrote) — wrote is False when an already-correct file was left
    as-is. Pure/deterministic apart from the file write; safe to call repeatedly.
    """
    import json
    if config_root is None:
        path = _embed_config_path()
    else:
        path = os.path.join(config_root, ".embed_config.json")
    url = f"http://127.0.0.1:{port or 8082}"

    existing: dict = {}
    if os.path.exists(path) and not overwrite:
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f) or {}
        except Exception:  # noqa: BLE001 — a corrupt file is replaced, not trusted
            existing = {}

    desired = dict(existing)
    desired["disable_inproc_embedder"] = True
    desired.setdefault("fallback_url", url)
    if gguf_path:
        desired.setdefault("gguf_path", gguf_path.replace("\\", "/"))
    desired["_comment"] = (
        "Route all m3 processes to the shared GPU embedder "
        "(bin/embed_server_inproc.py) so only ONE CUDA context exists. "
        "Seeded by the installer/doctor; edit via `m3 embedder shared|unshared`.")

    if desired == existing:
        return path, False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(desired, f, indent=2)
    return path, True


def cmd_shared(args: argparse.Namespace) -> int:
    """Route ALL m3 processes to ONE shared GPU embedder (one CUDA context).

    Writes <config_root>/.embed_config.json so every m3 process (MCP server,
    cognitive loop) disables its OWN in-process embedder and defers to a single
    shared server (bin/embed_server_inproc.py) over localhost HTTP. This reclaims
    ~9-10 GB on a box where several processes would otherwise each open their own
    CUDA context (contexts can't cross process boundaries — the only way to load
    the GPU model once is one owner + thin clients). The win is host RAM, not
    latency: a single small embed is ~33 ms via the server vs ~28 ms in-process
    (the localhost round-trip adds a few ms); that per-call cost amortises across
    a batch, so bulk paths see negligible overhead.

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


def cmd_reembed(args: argparse.Namespace) -> int:
    """Retire vectors from a non-current embedding model (delegates to bin/).

    Fixes the mixed embed space that `m3 doctor` reports: cosine across two
    embedding models is meaningless, so minority-model rows rank wrongly with
    no error. The bin/ script deletes those vectors and hands off to the
    existing embed_backfill.py sweeper to regenerate them. Dry-run by default —
    it prints what it would delete and exits until --apply is passed.
    """
    from m3_memory.cli import _resolve_bin_script, _run_bin_script

    if _resolve_bin_script("reembed_space.py") is None:
        print("error: bin/reembed_space.py not found — is the payload installed? "
              "Try `m3 update`.")
        return 1

    argv: "list[str]" = []
    if getattr(args, "db", None):
        argv += ["--db", args.db]
    if getattr(args, "keep", None):
        argv += ["--keep", args.keep]
    for flag in ("apply", "no_backup", "no_backfill"):
        if getattr(args, flag, False):
            argv.append("--" + flag.replace("_", "-"))
    return _run_bin_script("reembed_space.py", argv)


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

    p_fetch = sub.add_parser(
        "fetch-model",
        help="Download the BGE-M3 GGUF (~418 MB) into m3's own model dir (~/.m3/models).",
    )
    p_fetch.add_argument("--dest", default=None,
                         help="Override the destination dir (default: ~/.m3/models).")
    p_fetch.set_defaults(func=cmd_fetch_model)

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

    p_reembed = sub.add_parser(
        "reembed",
        help="Retire vectors from a non-current embedding model so they can be "
             "regenerated (fixes a mixed embed space reported by `m3 doctor`). "
             "Dry-run unless --apply is given.",
    )
    p_reembed.add_argument("--db", default=None,
                           help="Target DB (default: the resolved engine agent_memory.db).")
    p_reembed.add_argument("--keep", default=None,
                           help="Model family to KEEP (e.g. 'bge-m3'). "
                                "Default: the family holding the most vectors.")
    p_reembed.add_argument("--apply", action="store_true",
                           help="Actually delete. Without this it only reports.")
    p_reembed.add_argument("--no-backup", action="store_true",
                           help="Skip the pre-delete DB copy (not recommended).")
    p_reembed.add_argument("--no-backfill", action="store_true",
                           help="Do not chain embed_backfill.py after deleting.")
    p_reembed.set_defaults(func=cmd_reembed)
