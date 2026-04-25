"""
mcp-memory CLI — entry point for the M3 Memory MCP server.

Usage:
    mcp-memory                     Start the MCP server (stdio transport)
    mcp-memory --version           Print version and exit
    mcp-memory --help              Show this help

    mcp-memory install-m3          Fetch the m3-memory system payload from
                                   GitHub into ~/.m3-memory/repo (required
                                   once after `pip install m3-memory`)
    mcp-memory update              Re-fetch the payload for the current
                                   wheel version
    mcp-memory uninstall           Remove the cloned payload and config
    mcp-memory doctor              Print diagnostic info
"""

import argparse
import os
import sys
from pathlib import Path  # noqa: F401 - used in _auto_install return-type comment

# On Windows the default console code page is cp1252, which can't encode
# characters outside that 8-bit range (em-dashes, arrows, box-drawing,
# checkmarks, most non-Latin scripts). Any accidental non-ASCII in a
# CLI print() would crash the whole command. Force the stdio streams
# onto UTF-8 so user-facing output is safe to internationalize without
# auditing every print site. Python 3.7+ .reconfigure() exists on the
# real TextIOWrapper; during tests pytest substitutes a plain StringIO
# that doesn't, so guard with hasattr.
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
    with open(bridge) as f:
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
        )
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


def _cmd_doctor(_args: argparse.Namespace) -> int:
    from m3_memory.installer import doctor
    return doctor()


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
            # argparse / main() calls sys.exit — propagate the code.
            code = e.code
            return int(code) if isinstance(code, int) else (0 if code is None else 1)
    finally:
        sys.argv = saved_argv


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


def main() -> None:
    from m3_memory import __version__

    parser = argparse.ArgumentParser(
        prog="mcp-memory",
        description=(
            "M3 Memory - local-first agentic memory MCP server.\n"
            "Add to your agent's MCP config to get persistent, private memory."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # First-time setup after `pip install m3-memory`:
  mcp-memory install-m3

  # Start the MCP server (used by Claude Code / Gemini CLI / Aider):
  mcp-memory

  # Suggested mcp.json snippet:
  {
    "mcpServers": {
      "memory": {
        "command": "mcp-memory"
      }
    }
  }

  # Diagnose setup issues:
  mcp-memory doctor

Docs: https://github.com/skynetcmd/m3-memory
""",
    )
    parser.add_argument("--version", action="version", version=f"m3-memory {__version__}")

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    p_install = subparsers.add_parser(
        "install-m3",
        help="Fetch the m3-memory system payload from GitHub (~/.m3-memory/repo)",
    )
    p_install.add_argument(
        "--force", action="store_true",
        help="Wipe an existing ~/.m3-memory/repo before re-fetching.",
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
    p_install.set_defaults(func=_cmd_install_m3)

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

    p_doctor = subparsers.add_parser(
        "doctor",
        help="Print paths, resolved bridge, and install status.",
    )
    p_doctor.set_defaults(func=_cmd_doctor)

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

    # Use parse_known_args so flags after `chatlog <sub>` (e.g. --non-interactive,
    # --reconfigure, --json) aren't fought over by the outer parser — they're
    # passed through to the child script. args.rest carries them.
    args, extras = parser.parse_known_args()
    if getattr(args, "command", None) == "chatlog":
        args.rest = extras
    elif extras:
        # For any other subcommand, unknown flags are genuine errors.
        parser.error(f"unrecognized arguments: {' '.join(extras)}")

    if args.command is None:
        # Bare `mcp-memory` → run the bridge (unchanged default behavior).
        _run_bridge()
        return

    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
