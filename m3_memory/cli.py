"""
mcp-memory CLI — entry point for the M3 Memory MCP server.

Usage:
    mcp-memory            # Start the MCP server (stdio transport)
    mcp-memory --version  # Print version and exit
    mcp-memory --help     # Show this help
"""

import sys
import os
import argparse


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mcp-memory",
        description=(
            "M3 Memory — local-first agentic memory MCP server.\n"
            "Add to your agent's MCP config to get persistent, private memory."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
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

Docs: https://github.com/skynetcmd/m3-memory
""",
    )
    parser.add_argument(
        "--version", action="version", version="m3-memory 2026.4.8"
    )
    parser.parse_args()

    # Locate memory_bridge.py relative to this package
    package_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(package_dir)
    bridge = os.path.join(repo_root, "bin", "memory_bridge.py")

    if not os.path.exists(bridge):
        print(
            f"Error: memory_bridge.py not found at {bridge}\n"
            "If you installed via pip, clone the repo and set M3_BRIDGE_PATH:\n"
            "  git clone https://github.com/skynetcmd/m3-memory\n"
            "  export M3_BRIDGE_PATH=/path/to/m3-memory/bin/memory_bridge.py",
            file=sys.stderr,
        )
        sys.exit(1)

    # Insert bin/ onto sys.path so relative imports in memory_bridge work
    bin_dir = os.path.dirname(bridge)
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)

    # Execute the bridge in the current process
    with open(bridge) as f:
        code = compile(f.read(), bridge, "exec")
    exec(code, {"__file__": bridge, "__name__": "__main__"})


if __name__ == "__main__":
    main()
