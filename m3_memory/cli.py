"""
mcp-memory CLI — entry point for the M3 Memory MCP server.

Usage:
    mcp-memory                     Start the MCP server (stdio transport)
    mcp-memory --version           Print version and exit
    mcp-memory --help              Show this help

    mcp-memory install-m3          Fetch the m3-memory system payload from
                                   GitHub into the M3 root directory
                                   (default: ~/.m3-memory/repo)
    mcp-memory update              Re-fetch the payload for the current
                                   wheel version
    mcp-memory uninstall           Remove the cloned payload and config
    mcp-memory doctor              Print diagnostic info
"""

import argparse
import os
import sys
from pathlib import Path  # noqa: F401 - used in _auto_install return-type comment


def _ensure_utf8() -> None:
    """Guarantee the whole m3 process tree runs in Python UTF-8 mode.

    On Windows the default console code page is cp1252, and both stdio AND
    open() default to it. Any non-cp1252 character (em-dashes, arrows,
    box-drawing, emoji) then crashes with UnicodeEncodeError on print or
    UnicodeDecodeError on a no-encoding open(). `sys.stdout.reconfigure`
    (below) only fixes stdio — NOT open() defaults, and not the bin/ scripts
    this CLI execs/runpys. True UTF-8 mode (PEP 540) fixes all of it, but the
    interpreter reads it only at startup, so we set PYTHONUTF8 and re-exec
    once with -X utf8. The whole tree (CLI, in-process bridge, runpy'd bin
    scripts, child subprocesses inheriting the env) is then UTF-8.

    Safety: no-op if already in UTF-8 mode; a sentinel env var bounds the
    re-exec to exactly once so it can never loop.

    KNOWN LIMITATION — `python -c "<inline code>"` launches:
    Re-execing an inline `-c` payload can mangle on Windows because the OS
    re-quotes the program string when the process image is replaced; a
    multi-statement `-c` string may come back as a SyntaxError in the re-exec'd
    child. This does NOT affect any real m3 launch — the `m3` / `mcp-memory`
    console scripts launch a file path, and `python -m m3_memory.cli` and
    direct file-path launches all re-exec cleanly (verified). The `-c` form is
    only used for ad-hoc one-liners that import this module, which is not a
    supported entry path. If you must run such a one-liner and hit it, either
    set `PYTHONUTF8=1` in the env first (then _ensure_utf8 short-circuits, no
    re-exec) or write the code to a file and run that.
    """
    if sys.flags.utf8_mode:  # already -X utf8 or PYTHONUTF8=1
        return
    if os.environ.get("_M3_UTF8_REEXEC") == "1":  # re-exec already happened — stop
        return
    os.environ["PYTHONUTF8"] = "1"
    os.environ["_M3_UTF8_REEXEC"] = "1"
    # Rebuild the ORIGINAL launch faithfully. sys.orig_argv (3.10+) captures the
    # full command incl. the -c/-m/file form and interpreter flags; sys.argv
    # alone drops the launch form (re-exec'ing it breaks `python -c`/`-m`).
    # Insert -X utf8 right after the executable, preserving everything else.
    orig = list(getattr(sys, "orig_argv", [sys.executable, *sys.argv]))
    if not orig:  # defensive — shouldn't happen
        orig = [sys.executable, *sys.argv]
    new_argv = [orig[0], "-X", "utf8", *orig[1:]]

    # POSIX: os.execv truly REPLACES this process image — the child IS us, so
    # its exit code is the process exit code. Nothing to propagate.
    #
    # Windows: there is no real exec. Python emulates os.execv by SPAWNING a
    # child and returning control to the parent, which then exits 0 by falling
    # through — silently rewriting EVERY non-zero child exit code (argparse
    # errors, destructive-gate refusals, impl failures) to 0. That violates §3
    # ("fail loud, never silent"). So on Windows we run the re-exec'd command as
    # a real subprocess and propagate its exit code explicitly.
    if os.name == "nt":
        import subprocess
        try:
            completed = subprocess.run(new_argv)
        except OSError:
            # Spawn failed (exotic launcher / permissions). Fall through — the
            # reconfigure block below still fixes stdio, the common case.
            return
        sys.exit(completed.returncode)
    else:
        try:
            os.execv(sys.executable, new_argv)
        except OSError:
            # Re-exec failed (rare). Fall through — the reconfigure block below
            # still fixes stdio, the common case.
            pass


_ensure_utf8()

# Belt-and-suspenders for stdio when re-exec didn't run (already-UTF8, sentinel
# set, or re-exec failed). Python 3.7+ .reconfigure() exists on the real
# TextIOWrapper; during tests pytest substitutes a plain StringIO that doesn't,
# so guard with hasattr.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")


def _auto_install(interactive: bool) -> "Path | None":
    """Fetch the m3-memory system payload without an explicit `install-m3`.

    Called from `_run_bridge()` when the bridge resolves to None. Behavior
    depends on whether we're talking to a human:

    - Interactive TTY: ask for confirmation, since auto-fetching a GitHub
      repo on first run is surprising enough to deserve a prompt.
    - Non-interactive (piped stdin, launched as an MCP subprocess, CI):
      auto-fetch silently and log to stderr. Prompting would deadlock
      the parent process waiting for input that will never come.

    Respects M3_AUTO_INSTALL=0 as a hard opt-out for either mode.
    Returns the resolved bridge path on success, None on refusal/failure.
    """
    # Look up via attribute (not `from ... import`) so tests can monkeypatch
    # `m3_memory.installer.install_m3` and have it take effect here.
    from m3_memory import installer

    if os.environ.get("M3_AUTO_INSTALL", "").strip() == "0":
        return None

    dest = installer.default_repo_path()
    if interactive:
        print(
            f"m3-memory system payload not found locally.\n"
            f"Fetch it from GitHub into {dest} now? [Y/n] ",
            end="", flush=True,
        )
        try:
            reply = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("")  # newline after ^C / ^D
            return None
        if reply and reply not in ("y", "yes"):
            return None
    else:
        print(
            "[m3-memory] system payload not found; auto-fetching from GitHub "
            f"into {dest} (set M3_AUTO_INSTALL=0 to disable).",
            file=sys.stderr, flush=True,
        )

    try:
        return installer.install_m3()
    except RuntimeError as e:
        print(f"[m3-memory] auto-install failed: {e}", file=sys.stderr)
        return None


def _run_bridge() -> None:
    """Locate and execute the MCP server bridge.

    If no bridge can be resolved, try to auto-install the payload before
    giving up. See `_auto_install` for interactive vs non-interactive
    semantics.
    """
    from m3_memory.installer import config_file, find_bridge

    # Trigger self-healing for local embedder if it moved
    if _resolve_bin_script("setup_embedder.py"):
        _run_bin_script("setup_embedder.py", ["--heal"])

    bridge = find_bridge()
    if bridge is None:
        # Nothing on disk, no env override, no sibling — try to auto-install.
        bridge = _auto_install(interactive=sys.stdin.isatty())

    if bridge is None:
        print(
            "Error: m3-memory system payload is not installed.\n"
            "\n"
            "Run this once to fetch it:\n"
            "    mcp-memory install-m3\n"
            "\n"
            f"Or, if you already have a clone, set M3_BRIDGE_PATH or edit\n"
            f"    {config_file()}\n"
            "\n"
            "(Auto-install is gated by M3_AUTO_INSTALL; set M3_AUTO_INSTALL=0\n"
            "to permanently disable the prompt, or leave unset to accept.)\n",
            file=sys.stderr,
        )
        sys.exit(1)

    # Put bin/ on sys.path so the bridge's siblings (memory_core, etc.) import.
    bin_dir = str(bridge.parent)
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)

    # Execute the bridge in this process - equivalent to `python memory_bridge.py`,
    # not dynamic code from an untrusted source; it's a file path from our own
    # config (or an explicit env var, or a sibling of this package).
    with open(bridge, encoding="utf-8") as f:
        code = compile(f.read(), str(bridge), "exec")
    exec(code, {"__file__": str(bridge), "__name__": "__main__"})  # nosec B102


def _cmd_install_m3(args: argparse.Namespace) -> int:
    from m3_memory.installer import install_m3
    try:
        install_m3(
            force=args.force,
            tag=args.tag,
            interactive=(False if args.non_interactive else None),
            endpoint=args.endpoint,
            capture_mode=args.capture_mode,
            cognitive_loop=args.cognitive_loop,
            db_backend=args.db_backend,
        )
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def _cmd_reinstall(args: argparse.Namespace) -> int:
    """Wipe and reinstall the system payload (alias for install-m3 --force)."""
    from m3_memory.installer import install_m3
    try:
        install_m3(force=True, interactive=sys.stdin.isatty())
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    from m3_memory.installer import install_m3
    try:
        install_m3(force=True, tag=args.tag)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    from m3_memory.installer import uninstall_m3
    uninstall_m3(yes=args.yes)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    """`m3 status` — a one-line health verdict (run `m3 doctor` for detail)."""
    from m3_memory.installer import status
    return status()


def _cmd_doctor(args: argparse.Namespace) -> int:
    from m3_memory.installer import doctor
    # Brief is the DEFAULT; --verbose opts into the full detail.
    verbose = getattr(args, "verbose", False)
    brief = not verbose
    code = doctor(fix=getattr(args, "fix", False), brief=brief)

    # Also run the project-specific doctor if payload is installed. Forward
    # --verbose so the payload probes match; it may already be in args.rest
    # (parse_known_args), so only add it if absent.
    if _resolve_bin_script("memory_doctor.py"):
        rest = list(getattr(args, "rest", []) or [])
        if verbose and "--verbose" not in rest:
            rest.append("--verbose")
        if verbose:
            print("\n--- Project Payload Diagnostics ---")
        return _run_bin_script("memory_doctor.py", rest)

    return code


def _cmd_governor(args: argparse.Namespace) -> int:
    """Dispatch `m3 governor <status|migrate>` to bin/governor_cli.py."""
    if not _resolve_bin_script("governor_cli.py"):
        print("governor command requires the project payload (run `m3 install`).")
        return 1
    sub = getattr(args, "governor_cmd", None) or "status"
    argv = [sub]
    if sub == "migrate" and getattr(args, "yes", False):
        argv.append("--yes")
    return _run_bin_script("governor_cli.py", argv)


def _cmd_fips(args: argparse.Namespace) -> int:
    """Dispatch `m3 fips <install-wolfssl|status>`."""
    sub = getattr(args, "fips_cmd", None)
    if sub == "install-wolfssl":
        if not _resolve_bin_script("install_wolfssl.py"):
            print("fips install-wolfssl requires the project payload (run `m3 install`).")
            return 1
        argv: list = []
        if getattr(args, "ref", None):
            argv += ["--ref", args.ref]
        if getattr(args, "dest", None):
            argv += ["--dest", args.dest]
        if getattr(args, "print_sha", False):
            argv.append("--print-sha")
        return _run_bin_script("install_wolfssl.py", argv)
    # Default / `m3 fips status`: show the doctor crypto section.
    try:
        from m3_memory.installer import _crypto_section
        _crypto_section()
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"could not read crypto status: {e}")
        return 1


def _cmd_serve(args: argparse.Namespace) -> int:
    """Run the bridge with the streamable-http transport for claude.ai connectors.

    The default `mcp-memory` invocation is stdio (used by Claude Code, Gemini CLI,
    etc.). This subcommand starts the same bridge in HTTP mode so remote MCP
    clients (claude.ai web/desktop, Anthropic API mcp connector tool) can reach
    it after the user exposes the port via cloudflared / tailscale / ngrok / a
    reverse proxy.

    Env vars override flags so the same config works under systemd / docker.
    """
    os.environ["M3_TRANSPORT"] = "http"
    if args.host:
        os.environ["M3_HTTP_HOST"] = args.host
    if args.port:
        os.environ["M3_HTTP_PORT"] = str(args.port)
    if args.path:
        os.environ["M3_HTTP_PATH"] = args.path
    _run_bridge()
    return 0


def _cmd_dashboard(args: argparse.Namespace) -> int:
    """`m3 dashboard` — start the local web dashboard as a DETACHED background service.

    By default the server is launched WINDOWLESS and DETACHED (via pythonw on
    Windows / a new session on POSIX): no startup window, no flash, no periodic
    flashes, and it KEEPS RUNNING after you close the terminal. The command
    prints the URL and returns. Control it with:
      m3 dashboard            start (or report an already-running instance)
      m3 dashboard --stop     stop the running dashboard
      m3 dashboard --status   report URL + pid
      m3 dashboard --foreground   run in THIS process (debugging; blocks)

    Loopback-only by design (no authentication) — do not expose without auth.
    """
    # --stop / --status don't need the web deps (they only read the PID registry
    # / kill a pid), so handle them before the fastapi preflight.
    passthrough: list = []
    if getattr(args, "stop", False):
        passthrough = ["--stop"]
    elif getattr(args, "status", False):
        passthrough = ["--status"]
    else:
        # Starting a server DOES need the web deps — preflight for a clear message.
        missing = []
        for mod in ("fastapi", "uvicorn"):
            try:
                __import__(mod)
            except ModuleNotFoundError:
                missing.append(mod)
        if missing:
            print(
                "m3 dashboard needs the web dependencies "
                f"({', '.join(missing)} not installed).\n"
                "Install them with:\n"
                "    pip install \"m3-memory[dashboard]\"",
                file=sys.stderr,
            )
            return 1
        if getattr(args, "foreground", False):
            passthrough = ["--foreground"]

    if not _resolve_bin_script("dashboard_server.py"):
        print(
            "dashboard requires the project payload (run `m3 install`).",
            file=sys.stderr,
        )
        return 1

    if getattr(args, "host", None):
        passthrough += ["--host", args.host]
    if getattr(args, "port", None):
        passthrough += ["--port", str(args.port)]

    # runpy dashboard_server.py as __main__; its argparse handles the flags. The
    # default (no --foreground) detaches a windowless server and returns.
    return _run_bin_script("dashboard_server.py", passthrough)


def _resolve_bin_script(name: str) -> "Path | None":
    """Find bin/<name> relative to the resolved bridge (installed or dev).

    Returns an absolute Path or None if no bridge is resolvable (meaning
    install-m3 hasn't run and we're not in a dev checkout either).
    """
    from m3_memory.installer import find_bridge
    bridge = find_bridge()
    if bridge is None:
        return None
    candidate = bridge.parent / name
    return candidate if candidate.is_file() else None


def _run_bin_script(script_name: str, argv: list) -> int:
    """Execute bin/<script_name> as __main__ with the given argv.

    Uses runpy so the script's own argparse handles its flags unchanged.
    sys.argv is rewritten for the duration of the call so the script sees
    its own name in argv[0] (what it would see when invoked directly).
    """
    import runpy

    script = _resolve_bin_script(script_name)
    if script is None:
        print(
            f"Error: {script_name} not found.\n"
            "Run `mcp-memory install-m3` first.",
            file=sys.stderr,
        )
        return 1

    # Put bin/ on sys.path so the script's sibling imports (chatlog_config,
    # m3_sdk, ...) resolve the same way they do when invoked directly.
    bin_dir = str(script.parent)
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)

    saved_argv = sys.argv
    sys.argv = [str(script)] + list(argv)
    try:
        try:
            runpy.run_path(str(script), run_name="__main__")
            return 0
        except SystemExit as e:
            # argparse / main() calls sys.exit — propagate the code VERBATIM.
            # NOTE (single-instance contract): a service run via `m3 <svc>
            # --foreground` may exit m3_halt.EXIT_ALREADY_RUNNING (4) meaning
            # "another instance already holds the lock". That is a real, honest
            # non-zero signal — do NOT translate it to 0 here. Callers/supervisors
            # that must tolerate it handle it at their layer (systemd
            # SuccessExitStatus=4, launchd KeepAlive:Crashed-only, doctor probes
            # confirm by port not by exit code). The interactive `m3 <svc>`
            # (background) path already returns 0 for "already up" itself.
            code = e.code
            return int(code) if isinstance(code, int) else (0 if code is None else 1)
    finally:
        sys.argv = saved_argv


def _cmd_enrich_pending(args: argparse.Namespace) -> int:
    """Execute the enrich-pending subcommand."""
    import asyncio
    import sys

    # Import here to avoid dependency issues if memory_core is not yet installed
    sys.path.insert(0, str(_resolve_bin_script("memory_core.py").parent))
    import memory_core

    limit = args.limit or 0
    allowed_variants = args.allowed_variant or []

    try:
        # First call: dry-run to get count and ETA
        result = asyncio.run(memory_core.enrich_pending_impl(
            dry_run=True,
            limit=limit,
            allowed_variants=allowed_variants,
        ))

        if isinstance(result, dict):
            count = result.get("count", 0)
            eta_seconds = result.get("est_wall_clock_seconds", 0)
            sample_ids = result.get("sample_ids", [])

            # Format wall-clock time
            minutes = int(eta_seconds // 60)
            seconds = int(eta_seconds % 60)
            eta_str = f"{minutes}m {seconds}s"

            print(f"{count} items pending, ETA {eta_str}")
            if sample_ids:
                print(f"Sample IDs: {', '.join(sample_ids[:3])}")

            # If --yes is set and not --no-confirm, ask for confirmation
            if args.yes and not args.no_confirm:
                prompt = f"About to enrich {count} items. Estimated wall-clock: {eta_str}. Proceed? [y/N] "
                try:
                    reply = input(prompt).strip().lower()
                    if reply not in ("y", "yes"):
                        print("Cancelled.")
                        return 0
                except (EOFError, KeyboardInterrupt):
                    print("\nCancelled.")
                    return 0
            elif not args.yes:
                return 0

            # Execute enrichment
            print(f"Enriching {count} items...")
            result = asyncio.run(memory_core.enrich_pending_impl(
                dry_run=False,
                limit=limit,
                allowed_variants=allowed_variants,
            ))

            if isinstance(result, dict):
                processed = result.get("processed", 0)
                succeeded = result.get("succeeded", 0)
                failed = result.get("failed", 0)
                errors = result.get("errors_summary", "")

                print(f"Processed: {processed}, Succeeded: {succeeded}, Failed: {failed}")
                if errors:
                    print(f"Errors: {errors}")
            else:
                print(result)
        else:
            print(result)
            return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


def _cmd_extract_pending(args: argparse.Namespace) -> int:
    """Execute the extract-pending subcommand."""
    import asyncio
    import sys

    # Import here to avoid dependency issues if memory_core is not yet installed
    sys.path.insert(0, str(_resolve_bin_script("memory_core.py").parent))
    import memory_core

    limit = args.limit or 0
    allowed_variants = args.allowed_variant or []

    try:
        # First call: dry-run to get count and ETA
        result = asyncio.run(memory_core.extract_pending_impl(
            dry_run=True,
            limit=limit,
            allowed_variants=allowed_variants,
        ))

        if isinstance(result, dict):
            count = result.get("count", 0)
            eta_seconds = result.get("est_wall_clock_seconds", 0)
            sample_ids = result.get("sample_ids", [])

            # Format wall-clock time
            minutes = int(eta_seconds // 60)
            seconds = int(eta_seconds % 60)
            eta_str = f"{minutes}m {seconds}s"

            print(f"{count} items pending, ETA {eta_str}")
            if sample_ids:
                print(f"Sample IDs: {', '.join(sample_ids[:3])}")

            # If --yes is set and not --no-confirm, ask for confirmation
            if args.yes and not args.no_confirm:
                prompt = f"About to extract entities from {count} items. Estimated wall-clock: {eta_str}. Proceed? [y/N] "
                try:
                    reply = input(prompt).strip().lower()
                    if reply not in ("y", "yes"):
                        print("Cancelled.")
                        return 0
                except (EOFError, KeyboardInterrupt):
                    print("\nCancelled.")
                    return 0
            elif not args.yes:
                return 0

            # Execute extraction
            print(f"Extracting entities from {count} items...")
            result = asyncio.run(memory_core.extract_pending_impl(
                dry_run=False,
                limit=limit,
                allowed_variants=allowed_variants,
            ))

            if isinstance(result, dict):
                processed = result.get("processed", 0)
                succeeded = result.get("succeeded", 0)
                failed = result.get("failed", 0)
                errors = result.get("errors_summary", "")

                print(f"Processed: {processed}, Succeeded: {succeeded}, Failed: {failed}")
                if errors:
                    print(f"Errors: {errors}")
            else:
                print(result)
        else:
            print(result)
            return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


def _cmd_chatlog(args: argparse.Namespace) -> int:
    """Dispatch `mcp-memory chatlog <init|status|doctor>` to bin/ scripts."""
    sub = args.chatlog_cmd
    if sub is None:
        # No subcommand given → show status by default (matches `doctor` ergonomics).
        return _run_bin_script("chatlog_status.py", [])
    if sub == "init":
        return _run_bin_script("chatlog_init.py", args.rest)
    if sub == "status":
        return _run_bin_script("chatlog_status.py", args.rest)
    if sub == "hook-path":
        # Print the absolute path to the chatlog hook script for the current OS.
        # Used by the Claude Code plugin's hooks/hooks.json so plugin hooks
        # don't need to hardcode an install location.
        from m3_memory.installer import find_bridge
        bridge = find_bridge()
        if bridge is None:
            print("", file=sys.stdout)  # empty — caller should treat as no-op
            return 1
        if sys.platform == "win32":
            script = bridge.parent / "hooks" / "chatlog" / "claude_code_precompact.ps1"
        else:
            script = bridge.parent / "hooks" / "chatlog" / "claude_code_precompact.sh"
        if not script.is_file():
            return 1
        print(str(script))
        return 0

    if sub == "doctor":
        # `doctor` = status + nonzero exit if the subsystem reports warnings.
        # Implemented inline so we can inspect the JSON output without forking.
        script = _resolve_bin_script("chatlog_status.py")
        if script is None:
            print("Error: chatlog_status.py not found. Run `mcp-memory install-m3`.", file=sys.stderr)
            return 1
        import json as _json
        import runpy
        bin_dir = str(script.parent)
        if bin_dir not in sys.path:
            sys.path.insert(0, bin_dir)
        # Call the impl directly for clean JSON, bypassing the CLI formatter.
        mod = runpy.run_path(str(script), run_name="_chatlog_status_mod")
        data = _json.loads(mod["chatlog_status_impl"]())
        warnings = data.get("warnings") or []
        # Reuse the human-readable table formatter for output.
        print(mod["_format_table"](data))
        if warnings:
            print(f"\n[X] {len(warnings)} warning(s) — see above.", file=sys.stderr)
            return 1
        print("\n[OK] chatlog healthy.")
        return 0
    print(f"Error: unknown chatlog subcommand {sub!r}", file=sys.stderr)
    return 2


def _cmd_embedder(args: argparse.Namespace) -> int:
    """Manage the sovereign m3 CPU embedder (BGE-M3 on port 8082)."""
    sub = args.embedder_cmd
    if sub is None:
        print("Error: `m3 embedder` requires a subcommand (install, start, stop, status, "
              "uninstall, install-gpu, shared, unshared). Run `m3 embedder --help`.",
              file=sys.stderr)
        return 2
    # Each subcommand registered its own func= via embedder_admin.add_arguments.
    return args.func(args)


# ── Generated tool subcommands: `m3 <domain> <tool>` ─────────────────────────
# The MCP tool catalog (bin/mcp_tool_catalog.py) is the single source of truth.
# We generate one top-level subcommand per tool domain (memory, files, …) and,
# under each, one subcommand per tool. Humans reach the full tool surface the
# same way the LLM reaches it via m3_call — both go through the catalog's
# execute_tool_structured, so behavior cannot drift. See
# to_be_deleted/DUAL_SURFACE_TOOL_ACCESS_PLAN.md (PR 2).
#
# The chatlog DOMAIN is exposed as `m3 chat <tool>` because the top-level
# `m3 chatlog` operational command (init/status/doctor/hook-path) predates this
# and is wired into hooks.json + every install guide — left untouched.
_DOMAIN_CMD = {"chatlog": "chat"}

# Tools the human CLI must not surface (meta/dispatcher — no human use, and
# m3_call's object/array args don't map to flat flags).
_CLI_TOOL_EXCLUDE = frozenset(
    {"m3_call", "m3_index", "tools_list_domains", "tools_load_domain"}
)


def _bin_on_path() -> bool:
    """Put bin/ on sys.path so mcp_tool_catalog + siblings import. Returns
    False if no install/dev checkout is resolvable (tool subcommands are then
    silently skipped — the operational commands still work)."""
    core = _resolve_bin_script("memory_core.py")
    if core is None:
        return False
    bin_dir = str(core.parent)
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    return True


def _json_type_to_argparse(t: str):
    return {"integer": int, "number": float}.get(t)  # str is the argparse default


def _esc(s: str) -> str:
    """Escape % for argparse help (it %-expands help strings; a bare % in a
    tool description otherwise raises 'badly formed help string')."""
    return (s or "").replace("%", "%%")


def _spec_is_complex(spec) -> bool:
    """A tool has a complex arg (object / array-of-object / union) that can't be
    a flat flag — it takes a single --json blob instead."""
    props = spec.parameters.get("properties", {}) or {}
    for name, pdef in props.items():
        if name == "database":
            continue
        typ = pdef.get("type")
        if typ == "object" or "oneOf" in pdef or "anyOf" in pdef:
            return True
        if typ == "array":
            items = pdef.get("items", {}) or {}
            if items.get("type") == "object" or "oneOf" in items or "anyOf" in items:
                return True
    return False


def _add_tool_domain_subcommands(subparsers) -> None:
    """Register `m3 <domain> <tool>` subcommands generated from the catalog.

    Best-effort: if the catalog can't be imported (no install yet), skip
    silently — the operational commands above are unaffected.
    """
    if not _bin_on_path():
        return
    try:
        import mcp_tool_catalog as _cat
        import tool_domains as _td
    except Exception:
        return

    import argparse as _ap
    from collections import defaultdict

    by_domain = defaultdict(list)
    for spec in _cat.TOOLS:
        if spec.name in _CLI_TOOL_EXCLUDE:
            continue
        by_domain[_td.domain_of_tool(spec.name)].append(spec)

    for domain in sorted(by_domain):
        cmd = _DOMAIN_CMD.get(domain, domain)
        desc = _td.DOMAIN_DESCRIPTIONS.get(domain, "")
        dom_parser = subparsers.add_parser(
            cmd, help=_esc(f"{desc} ({len(by_domain[domain])} tools — run `m3 {cmd} --help`).")
        )
        tool_sub = dom_parser.add_subparsers(dest="_tool_cmd", metavar="<tool>")
        dom_parser.set_defaults(func=_cmd_tool_dispatch, _domain_cmd=cmd)

        # The chatlog domain (`m3 chat`) ALSO carries the operational
        # subcommands (init/status/doctor/hook-path) so `chat` is the single
        # chatlog namespace. `m3 chatlog <sub>` remains as a back-compat alias
        # (registered separately, untouched). These route to _cmd_chatlog via
        # chatlog_cmd; their flags are forwarded verbatim (parse_known_args).
        if cmd == "chat":
            for op, op_help in (
                ("init", "Interactive setup — wire hooks, choose DB path, configure redaction."),
                ("status", "Print a summary of chatlog state (row counts, queue, hooks)."),
                ("doctor", "Same as status, but exits nonzero on warnings."),
                ("hook-path", "Print absolute path to the chatlog hook script for plugin hooks."),
            ):
                op_p = tool_sub.add_parser(op, help=_esc(op_help), add_help=False)
                op_p.set_defaults(func=_cmd_chatlog, chatlog_cmd=op)

        for spec in sorted(by_domain[domain], key=lambda s: s.name):
            complex_ = _spec_is_complex(spec)
            summary = _esc((spec.description or "").split(".")[0].strip()[:75])
            tp = tool_sub.add_parser(spec.name, help=summary)
            props = spec.parameters.get("properties", {}) or {}
            required = set(spec.parameters.get("required", []))

            if complex_:
                tp.add_argument(
                    "--json", dest="_json_args", metavar="OBJ",
                    help="Tool arguments as a single JSON object "
                         "(this tool has a structured argument).",
                )
            else:
                for pname, pdef in props.items():
                    if pname == "database":
                        continue  # added via the shared --database helper below
                    ptype = pdef.get("type", "string")
                    phelp = _esc(pdef.get("description", ""))
                    req = pname in required
                    if ptype == "boolean":
                        tp.add_argument(
                            f"--{pname}", dest=pname,
                            action=_ap.BooleanOptionalAction,
                            default=None, help=phelp,
                        )
                    else:
                        tp.add_argument(
                            f"--{pname}", dest=pname,
                            type=_json_type_to_argparse(ptype),
                            required=req, default=None, help=phelp,
                        )

            # Universal extras on every tool subcommand.
            tp.add_argument("--database", dest="database", default=None,
                            help="SQLite DB path. Env: M3_DATABASE. Default: agent_memory.db.")
            tp.add_argument("--dry-run", dest="_dry_run", action="store_true",
                            help="Validate args + check the destructive gate without executing.")
            tp.add_argument("--yes", dest="_yes", action="store_true",
                            help="Confirm a destructive (mutating) tool. Required for such tools.")
            tp.set_defaults(func=_cmd_tool_dispatch, _tool_name=spec.name,
                            _tool_complex=complex_, _tool_destructive=not spec.default_allowed)


def _cmd_tool_dispatch(args: argparse.Namespace) -> int:
    """Run one generated `m3 <domain> <tool>` invocation through the catalog's
    execute_tool_structured — the same path m3_call uses."""
    import asyncio
    import json as _json

    tool = getattr(args, "_tool_name", None)
    if tool is None:
        # `m3 <domain>` with no tool — list the domain's tools.
        cmd = getattr(args, "_domain_cmd", "<domain>")
        print(f"Error: `m3 {cmd}` requires a tool. Run `m3 {cmd} --help`.",
              file=sys.stderr)
        return 2

    if not _bin_on_path():
        print("Error: tool catalog unavailable. Run `m3 install-m3` first.",
              file=sys.stderr)
        return 1
    import mcp_tool_catalog as _cat

    spec = next((t for t in _cat.TOOLS if t.name == tool), None)
    if spec is None:
        print(f"Error: unknown tool {tool!r}.", file=sys.stderr)
        return 1

    # Destructive confirmation (independent of the MCP env gate).
    destructive = getattr(args, "_tool_destructive", False)
    dry_run = getattr(args, "_dry_run", False)
    if destructive and not dry_run and not getattr(args, "_yes", False):
        print(f"Error: {tool} mutates/deletes data — pass --yes to confirm "
              f"(or --dry-run to validate without executing).", file=sys.stderr)
        return 2

    # Assemble the tool args.
    if getattr(args, "_tool_complex", False):
        raw = getattr(args, "_json_args", None)
        if raw is None:
            tool_args = {}
        else:
            try:
                tool_args = _json.loads(raw)
            except (ValueError, _json.JSONDecodeError) as e:
                print(f"Error: --json is not valid JSON: {e}", file=sys.stderr)
                return 2
            if not isinstance(tool_args, dict):
                print("Error: --json must be a JSON object.", file=sys.stderr)
                return 2
    else:
        props = spec.parameters.get("properties", {}) or {}
        tool_args = {}
        for pname in props:
            if pname == "database":
                continue
            val = getattr(args, pname, None)
            if val is not None:
                tool_args[pname] = val

    db = getattr(args, "database", None)
    if db:
        tool_args["database"] = db

    async def _run():
        return await _cat.execute_tool_structured(
            spec, tool_args, agent_id="", dry_run=dry_run)

    try:
        result = asyncio.run(_run())
    except Exception as e:  # validation (ValueError) or impl failure
        print(_json.dumps({"ok": False, "error": "call_failed", "tool": tool,
                           "detail": f"{type(e).__name__}: {e}"}), file=sys.stderr)
        return 1
    print(_json.dumps(result, default=str, indent=2))
    return 0


def main() -> None:
    from m3_memory import __version__

    parser = argparse.ArgumentParser(
        prog="m3",
        description=(
            "M3 Memory - local-first agentic memory MCP server.\n"
            "Add to your agent's MCP config to get persistent, private memory.\n"
            "(Also installed as `mcp-memory` for backwards compatibility.)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # First-time one-command setup (interactive wizard):
  m3 setup

  # Install the sovereign CPU embedder (BGE-M3 on port 8082):
  m3 embedder install

  # Add GPU acceleration to the in-process embedder (CUDA/Vulkan/Metal):
  m3 embedder install-gpu

  # Start the MCP server (used by Claude Code / Gemini CLI / Aider):
  m3
""",
    )
    parser.add_argument("--version", action="version", version=f"m3-memory {__version__}")

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    p_install = subparsers.add_parser(
        "install-m3",
        help="Fetch the m3-memory system payload from GitHub into the M3 root (default: ~/.m3-memory/repo)",
    )
    p_install.add_argument(
        "--force", action="store_true",
        help="Wipe an existing repo before re-fetching.",
    )
    p_install.add_argument(
        "--tag", default=None,
        help=f"Override the GitHub tag to fetch (default: v{__version__}).",
    )
    p_install.add_argument(
        "--non-interactive", action="store_true",
        help="Skip endpoint + capture-mode prompts; use defaults (probe both endpoints; both hooks).",
    )
    p_install.add_argument(
        "--endpoint", default=None, metavar="URL",
        help="Pin LLM_ENDPOINTS_CSV to this URL (skips the endpoint prompt).",
    )
    p_install.add_argument(
        "--capture-mode", default=None, choices=("both", "stop", "precompact", "none"),
        help="Chatlog capture hooks to enable (skips the capture-mode prompt).",
    )
    p_install.add_argument(
        "--cognitive-loop", action="store_true",
        help="Enable the background cognitive loop worker.",
    )
    p_install.add_argument(
        "--db-backend", default=None, choices=("sqlite", "postgres"),
        help="Primary database backend (default: sqlite). 'postgres' reads the "
             "DSN from M3_PRIMARY_PG_URL/M3_PG_URL and skips the backend prompt.",
    )
    p_install.set_defaults(func=_cmd_install_m3)

    # One-command interactive wizard: detects agents, asks a handful of
    # questions, then runs install-m3 + CPU-fallback service + per-agent
    # wiring + chatlog hooks + doctor end-to-end.
    p_setup = subparsers.add_parser(
        "setup",
        help="Interactive one-command setup. Run after `pip install m3-memory`.",
    )
    from m3_memory import setup_wizard as _setup_wizard
    _setup_wizard.add_arguments(p_setup)

    p_reinstall = subparsers.add_parser(
        "reinstall",
        help="Wipe and reinstall the system payload (alias for install-m3 --force).",
    )
    p_reinstall.set_defaults(func=_cmd_reinstall)

    p_update = subparsers.add_parser(
        "update",
        help="Re-fetch the payload, replacing any existing clone.",
    )
    p_update.add_argument(
        "--tag", default=None,
        help=f"Override the GitHub tag to fetch (default: v{__version__}).",
    )
    p_update.set_defaults(func=_cmd_update)

    p_uninstall = subparsers.add_parser(
        "uninstall",
        help="Remove the cloned payload and the config file.",
    )
    p_uninstall.add_argument(
        "-y", "--yes", action="store_true",
        help="Skip confirmation prompt.",
    )
    p_uninstall.set_defaults(func=_cmd_uninstall)

    p_status = subparsers.add_parser(
        "status",
        help="One-line health check (healthy/degraded/broken + memory count).",
    )
    p_status.set_defaults(func=_cmd_status)

    p_doctor = subparsers.add_parser(
        "doctor",
        help="Full diagnostics: paths, resolved bridge, embedder, chatlog, crypto.",
    )
    p_doctor.add_argument(
        "--fix", action="store_true",
        help="Repoint any agent MCP configs (Claude/Gemini/Antigravity/...) "
             "whose bridge/root paths are dead or moved, to the live install.",
    )
    p_doctor.add_argument(
        "--verbose", action="store_true",
        help="Show full detail (DB-repair steps, each probe's expanded report, "
             "model-load logs). Default is a compact high-yield summary.",
    )
    p_doctor.set_defaults(func=_cmd_doctor)

    p_governor = subparsers.add_parser(
        "governor",
        help="Inspect / migrate legacy scheduled tasks to the background governor.",
    )
    gov_sub = p_governor.add_subparsers(dest="governor_cmd", metavar="<status|migrate>")
    gov_sub.add_parser("status", help="Report governor-eligible scheduled tasks still installed.")
    p_gov_mig = gov_sub.add_parser("migrate", help="Remove governor-eligible scheduled tasks.")
    p_gov_mig.add_argument("--yes", "-y", action="store_true",
                           help="Skip the confirmation prompt (headless use).")
    p_governor.set_defaults(func=_cmd_governor)

    p_fips = subparsers.add_parser(
        "fips",
        help="FIPS crypto: build/install open-source wolfSSL, or show crypto status.",
    )
    fips_sub = p_fips.add_subparsers(dest="fips_cmd", metavar="<install-wolfssl|status>")
    p_fips_inst = fips_sub.add_parser(
        "install-wolfssl",
        help="Build the OPEN-SOURCE wolfSSL from official source and install to "
             "~/.m3/lib (license-clean; not the CMVP-validated FIPS module).",
    )
    p_fips_inst.add_argument("--ref", default=None, help="wolfSSL git tag/branch.")
    p_fips_inst.add_argument("--dest", default=None, help="Install dir (default ~/.m3/lib).")
    p_fips_inst.add_argument("--print-sha", action="store_true",
                             help="Print the installed library's SHA-256 to self-pin.")
    fips_sub.add_parser("status", help="Show the active crypto backend / FIPS tier / lib path.")
    p_fips.set_defaults(func=_cmd_fips)

    p_serve = subparsers.add_parser(
        "serve",
        help="Run the bridge as a streamable-HTTP MCP server (for claude.ai connectors).",
    )
    p_serve.add_argument("--host", default="127.0.0.1",
                         help="Bind address (default: 127.0.0.1 — use 0.0.0.0 only behind a reverse proxy).")
    p_serve.add_argument("--port", type=int, default=8080,
                         help="TCP port (default: 8080).")
    p_serve.add_argument("--path", default="/mcp",
                         help="HTTP mount path for the streamable-http endpoint (default: /mcp).")
    p_serve.set_defaults(func=_cmd_serve)

    p_dashboard = subparsers.add_parser(
        "dashboard",
        help="Start the local web dashboard as a detached background service (localhost only).",
    )
    p_dashboard.add_argument(
        "--host", default=None,
        help="Bind address (default: 127.0.0.1 — localhost only; do not expose without auth).",
    )
    p_dashboard.add_argument(
        "--port", type=int, default=None,
        help="TCP port (default: 8088, or $M3_DASHBOARD_PORT).",
    )
    p_dashboard.add_argument(
        "--stop", action="store_true",
        help="Stop the running dashboard (kills the detached server, frees the port).",
    )
    p_dashboard.add_argument(
        "--status", action="store_true",
        help="Report whether the dashboard is running and its URL.",
    )
    p_dashboard.add_argument(
        "--foreground", action="store_true",
        help="Run the server in THIS process (blocks; for debugging). Default detaches.",
    )
    p_dashboard.set_defaults(func=_cmd_dashboard)

    p_enrich = subparsers.add_parser(
        "enrich-pending",
        help="Enrich pending memory items with SLM-distilled facts.",
    )
    p_enrich.add_argument(
        "--yes", action="store_true",
        help="Execute enrichment (default is dry-run).",
    )
    p_enrich.add_argument(
        "--no-confirm", action="store_true",
        help="Skip confirmation prompt (for cron/headless use).",
    )
    p_enrich.add_argument(
        "--limit", type=int, default=0,
        help="Max items to enrich (0 = no limit, default: 0).",
    )
    p_enrich.add_argument(
        "--allowed-variant", action="append", default=[],
        help="Variant name to include (can be used multiple times).",
    )
    p_enrich.set_defaults(func=_cmd_enrich_pending)

    p_extract = subparsers.add_parser(
        "extract-pending",
        help="Extract pending entities from memory items.",
    )
    p_extract.add_argument(
        "--yes", action="store_true",
        help="Execute extraction (default is dry-run).",
    )
    p_extract.add_argument(
        "--no-confirm", action="store_true",
        help="Skip confirmation prompt (for cron/headless use).",
    )
    p_extract.add_argument(
        "--limit", type=int, default=0,
        help="Max items to extract (0 = no limit, default: 0).",
    )
    p_extract.add_argument(
        "--allowed-variant", action="append", default=[],
        help="Variant name to include (can be used multiple times).",
    )
    p_extract.set_defaults(func=_cmd_extract_pending)

    p_chatlog = subparsers.add_parser(
        "chatlog",
        help="Manage the chatlog subsystem (init|status|doctor).",
    )
    chatlog_sub = p_chatlog.add_subparsers(dest="chatlog_cmd", metavar="<subcommand>")
    # Declare the subcommands so help lists them, but accept no flags here —
    # we use parse_known_args below to collect everything after `chatlog <sub>`
    # and forward it verbatim to the underlying bin/ script.
    chatlog_sub.add_parser("init",      help="Interactive setup — wire hooks, choose DB path, configure redaction.", add_help=False)
    chatlog_sub.add_parser("status",    help="Print a summary of chatlog state (row counts, queue, hooks).",         add_help=False)
    chatlog_sub.add_parser("doctor",    help="Same as status, but exits nonzero on warnings.",                       add_help=False)
    chatlog_sub.add_parser("hook-path", help="Print absolute path to the chatlog hook script for plugin hooks.",      add_help=False)
    p_chatlog.set_defaults(func=_cmd_chatlog)

    p_embedder_mgmt = subparsers.add_parser(
        "embedder",
        help="Manage the sovereign m3 CPU embedder (install|start|stop|status|uninstall|install-gpu).",
    )
    from m3_memory import embedder_admin as _embedder_admin
    _embedder_admin.add_arguments(p_embedder_mgmt)
    p_embedder_mgmt.set_defaults(func=_cmd_embedder)

    # Generated tool subcommands: `m3 <domain> <tool>` (and `m3 chat <tool>` for
    # the chatlog domain). Best-effort — skipped if no install/dev checkout.
    _add_tool_domain_subcommands(subparsers)

    # Use parse_known_args so flags after `chatlog <sub>` aren't fought over
    # by the outer parser — they're passed through to the child script.
    # args.rest carries them.
    args, extras = parser.parse_known_args()
    # chatlog (alias) + doctor forward unknown flags verbatim to the child
    # script. `chat` does too, but ONLY for its operational subcommands
    # (chatlog_cmd set) — for generated tool subcommands, unknown flags are
    # real errors just like every other domain.
    _forward = getattr(args, "command", None) in ("chatlog", "doctor") or (
        getattr(args, "command", None) == "chat" and getattr(args, "chatlog_cmd", None)
    )
    if _forward:
        args.rest = extras
    elif extras:
        # For any other subcommand, unknown flags are genuine errors.
        parser.error(f"unrecognized arguments: {' '.join(extras)}")

    if args.command is None:
        # Bare `m3` → run the bridge (the MCP stdio server). This is how agents
        # invoke it, so stdout MUST stay clean (it's the protocol channel).
        # But a HUMAN running bare `m3` in a terminal usually wanted help, not a
        # silent server — print a one-line hint to STDERR (never stdout) so it
        # doesn't corrupt the protocol, only when attached to a TTY.
        if sys.stdin.isatty() and sys.stdout.isatty():
            print(
                "m3: starting the MCP memory server (this is what your agent runs).\n"
                "    New here? Run `m3 setup` to install + wire your agent,\n"
                "    or `m3 --help` for all commands.  Ctrl-C to stop.",
                file=sys.stderr,
            )
        _run_bridge()
        return

    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
