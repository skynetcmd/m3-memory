r"""The writer scan must not mistake an m3 COMMAND for the m3 SERVER.

A bare `m3` (no subcommand) starts the MCP server — cli.py falls through to
_run_bridge() — so it must be detected as a DB-writer. But `m3 setup`,
`m3 doctor`, `m3 stop` and `m3 memory ...` are short-lived commands that hold
nothing.

The old signatures were the substrings "/m3.exe", "\\m3.exe", "/m3 ", "\\m3 ",
which match the EXECUTABLE and cannot tell those apart. Two consequences, both
real:

  - `m3 setup` flagged ITSELF as a live MCP server. On Windows that was fatal,
    not cosmetic: preflight found its own parent "holding" the DB, correctly
    refused to kill an ancestor, and aborted the install (exit 2). `m3 setup`
    could not run through the console script at all.
  - the REAL server (memory_bridge.py) was matched by none of them.

The signature was, in effect, inverted. These tests pin both directions.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))

import m3_halt  # noqa: E402

SERVER_CMDLINES = [
    "m3",
    "m3.exe",
    r"C:\Users\u\.local\bin\m3.exe",
    r'"C:\Users\u\.local\bin\m3.exe"',
    "/usr/local/bin/m3",
    # the real shape on this box: the venv interpreter running the m3 shim
    r"C:\x\pipx\venvs\m3-memory\Scripts\python.exe C:\u\.local\bin\m3.exe",
    # `m3 serve` runs the bridge as a streamable-HTTP MCP server — a genuine
    # long-lived writer, and the ONE subcommand that IS the server. A regex that
    # only allowed a bare `m3` would silently stop detecting it.
    r"C:\x\Scripts\m3.exe serve",
    "/home/u/.local/bin/m3 serve --port 8080",
]

COMMAND_CMDLINES = [
    r"C:\x\Scripts\m3.exe setup --non-interactive --terminal",
    r"C:\x\Scripts\m3.exe doctor",
    r"C:\x\Scripts\m3.exe stop",
    "/usr/bin/m3 memory memory_write --content x",
    "/usr/bin/m3 chatlog status",
    # the module form, and the UTF-8 re-exec child it spawns on Windows
    r"C:\py\python.exe -m m3_memory.cli setup",
    r"C:\py\python.exe -X utf8 -m m3_memory.cli setup",
]


@pytest.mark.parametrize("cmd", SERVER_CMDLINES)
def test_bare_m3_is_detected_as_the_mcp_server(cmd):
    assert m3_halt._MCP_BARE_M3_RE.search(cmd), (
        f"a bare `m3` starts the MCP server and must be seen as a writer: {cmd!r}"
    )


@pytest.mark.parametrize("cmd", COMMAND_CMDLINES)
def test_m3_subcommands_are_not_the_server(cmd):
    assert not m3_halt._MCP_BARE_M3_RE.search(cmd), (
        f"an m3 SUBCOMMAND holds no DB and must not be flagged as a writer — "
        f"flagging `m3 setup` made the installer abort on its own parent: {cmd!r}"
    )


def test_the_old_executable_substrings_are_gone():
    """Guard the fix itself: re-adding an exe-substring signature reintroduces
    the self-flagging bug, because it cannot see the subcommand."""
    sigs = m3_halt._WRITER_CMDLINE_SIGNATURES.get("mcp", ())
    for bad in ("/m3.exe", "\\m3.exe", "/m3 ", "\\m3 "):
        assert bad not in sigs, (
            f"{bad!r} matches the executable, not the invocation — `m3 setup` "
            f"would flag itself again"
        )


def test_the_real_bridge_still_matches_a_signature():
    """memory_bridge.py is the actual server process; something must catch it.

    It registers itself via the PID registry (register_process("mcp")), which is
    the primary path — this asserts the roles table still knows the mcp role so
    a registry entry classifies correctly.
    """
    assert "mcp" in m3_halt._WRITER_CMDLINE_SIGNATURES
    assert "mcp-memory" in m3_halt._WRITER_CMDLINE_SIGNATURES["mcp"]
