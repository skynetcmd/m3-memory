"""Doctor probe: is SHARED-embedder mode healthy?

Shared mode is m3's shipped default: every process defers to ONE shared embedder
server (GPU-accelerated where available, CPU-only otherwise) kept alive by a
self-healing scheduled task. Three things must all be true, and each fails
independently and silently:

  1. CONFIG   — <config_root>/.embed_config.json disables the per-process
                embedder and points clients at the shared server.
  2. SERVER   — that server actually answers :8082/health.
  3. KEEPALIVE— the AgentOS_EmbedServer task is registered with its 1-min
                self-heal so the server survives a crash/reboot.

`run(brief, fix)` FLAGS every broken piece with the exact remedy (returns
non-zero so `m3 doctor` surfaces it), and with `fix=True` REPAIRS what it can:
write the config, start the server, register the task — printing the elevated
command when task registration needs an admin shell it doesn't have.

Report-and-fix, never crashes the doctor.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from typing import Any
from urllib.parse import urlparse

_PORT = 8082
_DEFAULT_URL = f"http://127.0.0.1:{_PORT}"
_TASK = "AgentOS_EmbedServer"


def _config_root() -> str:
    # Dependency-free mirror of m3_core.paths.get_m3_config_root (this probe must
    # run from a bare payload without m3_sdk on the path).
    root = os.environ.get("M3_CONFIG_ROOT")
    if not root:
        mem = os.environ.get("M3_MEMORY_ROOT")
        root = (os.path.join(os.path.abspath(os.path.expanduser(mem)), "config")
                if mem else os.path.join(os.path.expanduser("~"), ".m3", "config"))
    return root


def _config_path() -> str:
    return os.path.join(_config_root(), ".embed_config.json")


def _read_config() -> dict | None:
    path = _config_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — a malformed file is "config broken", handled by caller
        return {}


def _known_agent_settings() -> "list[tuple[str, str]]":
    """(label, path) for every client settings file that may carry an m3 MCP
    server env block. Dependency-free mirror of installer._known_agent_settings
    so this probe runs from a bare payload."""
    home = os.path.expanduser("~")
    j = os.path.join
    return [
        ("Claude Code", j(home, ".claude", "settings.json")),
        ("Gemini CLI",  j(home, ".gemini", "settings.json")),
        ("Antigravity", j(home, ".gemini", "antigravity-cli", "settings.json")),
        ("OpenCode",    j(home, ".opencode", "settings.json")),
        ("Aider",       j(home, ".aider", "settings.json")),
    ]


def _detect_inproc_env_leak() -> "list[str]":
    """Locations where M3_EMBED_GGUF is set — each forces a per-process CUDA
    embedder (the hang footgun) when shared mode is on. Returns human-readable
    location strings (empty when clean). Checks the process env AND every client
    settings file's m3 MCP-server env block."""
    hits: list[str] = []
    if (os.environ.get("M3_EMBED_GGUF") or "").strip():
        hits.append("process env (M3_EMBED_GGUF) — persists via User env / shell rc")
    for label, path in _known_agent_settings():
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:  # noqa: BLE001 — a bad settings file is another probe's concern
            continue
        for name, entry in (data.get("mcpServers") or {}).items():
            env_block = (entry or {}).get("env")
            if not isinstance(env_block, dict) or "M3_EMBED_GGUF" not in env_block:
                continue
            args_blob = " ".join((entry or {}).get("args") or [])
            if name == "memory" or "memory_bridge" in args_blob or "m3" in name.lower():
                hits.append(f"{label}: mcpServers.{name}.env ({path})")
    return hits


def _fix_scrub_env_leak() -> tuple[bool, bool]:
    """Scrub M3_EMBED_GGUF from m3 MCP-server env blocks in every client settings
    file (backing each up first). Returns (settings_scrubbed, process_env_remains).
    A persistent User/shell env var CANNOT be auto-removed from another process's
    environment — we report it so the user removes it (with the exact command)."""
    settings_scrubbed = False
    for _label, path in _known_agent_settings():
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                original = f.read()
            data = json.loads(original) or {}
        except Exception:  # noqa: BLE001
            continue
        changed = False
        for name, entry in (data.get("mcpServers") or {}).items():
            env_block = (entry or {}).get("env")
            if not isinstance(env_block, dict) or "M3_EMBED_GGUF" not in env_block:
                continue
            args_blob = " ".join((entry or {}).get("args") or [])
            if name == "memory" or "memory_bridge" in args_blob or "m3" in name.lower():
                env_block.pop("M3_EMBED_GGUF", None)
                changed = True
        if changed:
            try:
                with open(path + ".bak", "w", encoding="utf-8") as f:
                    f.write(original)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                    f.write("\n")
                print(f"  [fix] scrubbed M3_EMBED_GGUF from {path} (backup: {path}.bak)")
                settings_scrubbed = True
            except Exception as e:  # noqa: BLE001
                print(f"  [fix] could not rewrite {path}: {e}")
    process_env_remains = bool((os.environ.get("M3_EMBED_GGUF") or "").strip())
    if process_env_remains:
        print("  [fix] M3_EMBED_GGUF is ALSO set in the process/User env — that cannot")
        print("        be removed from here. Remove it so new shells + MCP servers stop")
        print("        inheriting it:")
        if sys.platform == "win32":
            print("          PowerShell:  [Environment]::SetEnvironmentVariable("
                  "'M3_EMBED_GGUF', $null, 'User')")
        else:
            print("          remove the `export M3_EMBED_GGUF=...` line from your shell rc "
                  "(~/.zshrc / ~/.bashrc / ~/.profile)")
        print("        then start a NEW shell and restart the MCP client.")
    return settings_scrubbed, process_env_remains


def _server_health(url: str, timeout: float = 3.0) -> tuple[str, dict]:
    """Return (state, body). state in {'ok','loading','bad-scheme','down'}."""
    if urlparse(url).scheme not in ("http", "https"):
        return "bad-scheme", {}
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=timeout) as r:  # nosec B310 — scheme checked
            body = json.loads(r.read())
        status = body.get("status")
        if status in ("ok", "loading"):
            return status, body
        return "down", body
    except Exception:  # noqa: BLE001
        return "down", {}


def _payload_bin() -> str:
    # bin/ is this file's grandparent-parent: bin/doctor/this.py -> bin/.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _task_registered_ok() -> bool | None:
    """True/False if the keep-alive task matches spec; None on non-Windows (no
    embed-server service unit exists on Unix yet — reported honestly, not faked)."""
    if sys.platform != "win32":
        return None
    try:
        sys.path.insert(0, _payload_bin())
        import contextlib
        import io

        import install_schedules
        # _verify_windows_task prints its own [OK]/[FAIL] lines; silence them so
        # this probe emits a single clean 'keepalive:' verdict instead of two.
        with contextlib.redirect_stdout(io.StringIO()):
            return install_schedules._verify_windows_task(_TASK)
    except Exception:  # noqa: BLE001
        return False


def _rust_service_present() -> bool:
    """True if the Rust m3-embed-server binary is installed — the PREFERRED,
    cross-platform keep-alive (registered as a systemd/launchd/Windows Service
    with OS-native restart). When present it, not the Python scheduled task, owns
    :8082, so the task legitimately does not exist."""
    try:
        sys.path.insert(0, os.path.join(_payload_bin(), "..", "m3_memory"))
        from m3_memory import embedder_admin
        return embedder_admin._server_binary() is not None
    except Exception:  # noqa: BLE001
        return False


def _keepalive() -> tuple[str, bool]:
    """Return (kind, ok). kind in {'rust-service','windows-task','none'};
    ok=True means a keep-alive that will restart the server is in place.

    Preference order matches setup: the Rust OS service wins; the Python
    scheduled task is the fallback. 'none' with a live server means someone
    started it by hand — it works now but won't survive a crash/reboot."""
    if _rust_service_present():
        return "rust-service", True
    task = _task_registered_ok()
    if task is True:
        return "windows-task", True
    if task is None:
        # Non-Windows, no Rust binary: the only keep-alive would be a Unix unit
        # from `m3 embedder install`, which needs the Rust binary — so none.
        return "none", False
    return "none", False


# ── fix actions ───────────────────────────────────────────────────────────────
def _fix_write_config() -> bool:
    try:
        sys.path.insert(0, os.path.join(_payload_bin(), "..", "m3_memory"))
        import argparse

        from m3_memory import embedder_admin
        # cmd_shared reads only args.port; pass a real Namespace to satisfy its
        # argparse.Namespace signature (SimpleNamespace is not a subtype).
        embedder_admin.cmd_shared(argparse.Namespace(port=_PORT))
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  [fix] could not write .embed_config.json: {e}")
        return False


def _fix_start_server() -> bool:
    script = os.path.join(_payload_bin(), "embed_server_inproc.py")
    if not os.path.exists(script):
        print("  [fix] embed_server_inproc.py not found; cannot start the server.")
        return False
    # The server's own _already_serving pre-flight makes a redundant start a no-op,
    # so starting is always safe. Detached so it outlives the doctor process.
    try:
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(  # noqa: S603 — fixed argv, our own script
            [sys.executable, script, "--port", str(_PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs,
        )
        print(f"  [fix] launched embed server on {_DEFAULT_URL} (loading may take a few seconds).")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  [fix] could not start the embed server: {e}")
        return False


def _fix_register_task() -> bool:
    # The scheduled-task fallback is WINDOWS-ONLY (schtasks). On Unix the only
    # cross-boot keep-alive is the Rust m3-embed-server OS service; there is no
    # launchd/systemd unit for the Python server, so don't pretend to register
    # one (§1 3-OS, §3 never-silent) — point at the Rust install instead.
    if sys.platform != "win32":
        print("  [fix] no scheduled-task fallback on this OS — install the Rust")
        print("        sovereign embedder to get a systemd/launchd keep-alive:")
        print("            m3 embedder install-gpu && m3 embedder install")
        return False
    script = os.path.join(_payload_bin(), "install_schedules.py")
    if not os.path.exists(script):
        print("  [fix] install_schedules.py not found; cannot register the keep-alive task.")
        return False
    rc = subprocess.run(  # noqa: S603 — fixed argv
        [sys.executable, script, "--add", "embed-server"], check=False,
    ).returncode
    if rc != 0:
        # install_schedules already printed the elevation hint on Windows.
        print("  [fix] task not registered — on Windows this needs an ADMIN shell:")
        print("            python bin/install_schedules.py --repair   # from an elevated terminal")
    return rc == 0


def run(brief: bool = False, fix: bool = False) -> int:
    """Check config + server + keep-alive task. Return non-zero if shared mode is
    not fully healthy. With fix=True, repair what's repairable first."""
    cfg = _read_config()
    shared_on = bool(cfg and cfg.get("disable_inproc_embedder"))
    url = ((cfg or {}).get("fallback_url") or _DEFAULT_URL).rstrip("/")

    problems: list[str] = []

    # 1. Config
    if cfg is None:
        problems.append("config-missing")
    elif not shared_on:
        problems.append("config-not-shared")

    # 2. Server (only meaningful once we know the URL we'd use)
    health, body = _server_health(url)
    if health in ("down", "bad-scheme"):
        problems.append("server-down")

    # 3. Keep-alive: a Rust OS service OR the Python scheduled task must exist so
    #    the server survives a crash/reboot. Flag only if NEITHER is present.
    ka_kind, ka_ok = _keepalive()
    if not ka_ok:
        problems.append("keepalive-missing")

    # 4. Inproc env leak: M3_EMBED_GGUF anywhere forces a per-process CUDA embedder
    #    — the unbounded-init hang. Flag it whenever shared mode is the intent
    #    (config missing or shared-on), so the exact footgun is never silent.
    leak_locations = _detect_inproc_env_leak()
    if leak_locations:
        problems.append("inproc-env-leak")

    if fix and problems:
        print()
        print("=== shared-embedder: applying fixes ===")
        if "config-missing" in problems or "config-not-shared" in problems:
            if _fix_write_config():
                problems = [p for p in problems if not p.startswith("config")]
        if "server-down" in problems:
            if _fix_start_server():
                # re-probe after a short grace so the verdict reflects reality
                import time
                for _ in range(10):
                    time.sleep(1)
                    health, body = _server_health(url, timeout=2)
                    if health in ("ok", "loading"):
                        problems = [p for p in problems if p != "server-down"]
                        break
        if "keepalive-missing" in problems:
            # Prefer the Rust OS service; only register the Python task fallback
            # when the Rust binary is absent (mutually exclusive — both bind :8082).
            if _rust_service_present():
                print("  [fix] Rust m3-embed-server present — register its OS service "
                      "with `m3 embedder install` (keeps :8082 up cross-platform).")
                # Re-evaluate: if the service is now the keep-alive, clear it.
                if _keepalive()[1]:
                    problems = [p for p in problems if p != "keepalive-missing"]
            elif _fix_register_task():
                problems = [p for p in problems if p != "keepalive-missing"]
        if "inproc-env-leak" in problems:
            scrubbed, env_remains = _fix_scrub_env_leak()
            # Clear the problem only when nothing leaks anymore. A persistent
            # process/User env var still needs the manual step printed above, so
            # keep the flag (loud, not silent) until it's actually gone.
            if not _detect_inproc_env_leak():
                problems = [p for p in problems if p != "inproc-env-leak"]

    healthy = not problems

    if brief:
        if healthy:
            print("✅ shared-embedder: OK (config + server + keep-alive)")
        else:
            print(f"⚠️  shared-embedder: {len(problems)} issue(s) — run `m3 doctor --fix`")
        return 0 if healthy else 1

    print()
    print("=== shared-embedder mode (shipped default) ===")
    print(f"  config   : {_config_path()}")
    if "config-missing" in problems:
        print("  mode     : [FAIL] .embed_config.json MISSING — shared mode not enabled.")
        print("             fix: `m3 setup` (auto), or `m3 embedder shared`.")
    elif "config-not-shared" in problems:
        print("  mode     : [FAIL] config present but disable_inproc_embedder is false.")
        print("             fix: `m3 embedder shared` (rewrites it correctly).")
    else:
        print(f"  mode     : SHARED — clients defer to {url}")

    if "server-down" in problems:
        print(f"  server   : [FAIL] {url}/health not answering — embeds will slow-cascade")
        print("             or fail fleet-wide until it serves.")
        print(f"             fix: start it (`python bin/embed_server_inproc.py --port {_PORT}`)")
        print("                  or register the keep-alive task below.")
    else:
        print(f"  server   : OK (model={body.get('model')}, dim={body.get('dim')}, "
              f"status={health})")

    if ka_kind == "rust-service":
        print("  keepalive: OK — Rust m3-embed-server OS service "
              "(systemd/launchd/Windows Service, OS-native restart).")
    elif ka_kind == "windows-task":
        print(f"  keepalive: OK — {_TASK} scheduled task (1-min self-heal, "
              "Python-server fallback).")
    else:  # none
        print("  keepalive: [FAIL] nothing keeps :8082 alive — no Rust m3-embed-server")
        print("             service and no embed-server task. A crash/reboot silently")
        print("             kills embedding fleet-wide.")
        print("             fix (preferred): `m3 embedder install` (Rust OS service), OR")
        print("             fallback: `python bin/install_schedules.py --add embed-server`")
        print("                       (Windows: from an ADMIN shell — ONSTART needs it).")

    if "inproc-env-leak" in problems:
        print("  inproc   : [FAIL] M3_EMBED_GGUF is set — forces a per-process GPU")
        print("             context (CUDA/Metal/Vulkan) or heavy CPU load per process")
        print("             (the read/write HANG risk). Locations:")
        for loc in leak_locations:
            print(f"               - {loc}")
        print("             fix: `m3 doctor --fix` scrubs the settings blocks (backs")
        print("                  them up). A persistent User/shell env var must be")
        print("                  removed by hand — the fix prints the exact command.")
    else:
        print("  inproc   : OK — no M3_EMBED_GGUF leak (clients defer to the server).")

    if healthy:
        print("  status   : OK — shared mode fully configured and serving.")
    elif not fix:
        print()
        print("  Run `m3 doctor --fix` to repair the above automatically.")
    return 0 if healthy else 1
