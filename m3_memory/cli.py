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


def _run_bridge() -> None:
    """Locate and execute the MCP server bridge."""
    from m3_memory.installer import find_bridge, config_file

    bridge = find_bridge()
    if bridge is None:
        print(
            "Error: m3-memory system payload is not installed.\n"
            "\n"
            "Run this once after pip install:\n"
            "    mcp-memory install-m3\n"
            "\n"
            f"Or, if you already have a clone, set M3_BRIDGE_PATH or edit\n"
            f"    {config_file()}\n",
            file=sys.stderr,
        )
        sys.exit(1)

    # Put bin/ on sys.path so the bridge's siblings (memory_core, etc.) import.
    bin_dir = str(bridge.parent)
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)

    # Execute the bridge in this process — equivalent to `python memory_bridge.py`,
    # not dynamic code from an untrusted source; it's a file path from our own
    # config (or an explicit env var, or a sibling of this package).
    with open(bridge) as f:
        code = compile(f.read(), str(bridge), "exec")
    exec(code, {"__file__": str(bridge), "__name__": "__main__"})  # nosec B102


def _cmd_install_m3(args: argparse.Namespace) -> int:
    from m3_memory.installer import install_m3
    try:
        install_m3(force=args.force, tag=args.tag)
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

    args = parser.parse_args()

    if args.command is None:
        # Bare `mcp-memory` → run the bridge (unchanged default behavior).
        _run_bridge()
        return

    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
