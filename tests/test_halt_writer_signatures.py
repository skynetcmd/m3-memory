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
    assert m3_halt.writer_role(cmd.split()) == "mcp", (
        f"a bare `m3` starts the MCP server and must be seen as a writer: {cmd!r}"
    )


@pytest.mark.parametrize("cmd", COMMAND_CMDLINES)
def test_m3_subcommands_are_not_the_server(cmd):
    assert m3_halt.writer_role(cmd.split()) is None, (
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


# ── what a process RUNS, not what its arguments mention ──────────────────────
# Substring matching made `m3 stop` kill any process that merely NAMED a writer.
# Measured 2026-10-07: it killed the shell driving an upgrade, whose next child
# then failed with 0xC0000142. These are argv lists, as psutil returns them.

MENTIONS_ONLY = [
    ["C:/Program Files/Git/bin/bash.exe", "-c",
     "echo $(powershell -c \"Get-Process | ? { $_.Name -eq 'm3-embed-server.exe' }\")"],
    ["grep", "-n", "m3_cognitive_loop.py", "bin/install_schedules.py"],
    ["tail", "-f", "/home/u/.m3/logs/m3_embed_server_inproc.log"],
    ["code", "bin/m3_cognitive_loop.py"],
    ["/usr/bin/python3", "-c", "import subprocess; subprocess.run(['m3_cognitive_loop.py'])"],
    ["bash", "-c", "which m3"],
    ["pgrep", "-fa", "m3-embed-server"],
]

REAL_WRITERS = [
    # Task Scheduler: pythonw + quoted script path + args
    ([r"C:\v\Scripts\pythonw.exe", r'"C:\v\Lib\site-packages\m3_memory\bin\m3_cognitive_loop.py"',
      "--interval", "60", "--background"], "cognitive-loop"),
    # the waiter is launched with -u before its script
    (["/home/u/.local/share/pipx/venvs/m3-memory/bin/python", "-u",
      "/home/u/.local/share/pipx/venvs/m3-memory/lib/python3.13/site-packages/m3_memory/bin/m3_notification_waiter.py"],
     "waiter"),
    (["/v/bin/python3.14", "-X", "utf8", "/v/bin/embed_server_inproc.py"], "embed-server"),
    # systemd/launchd run the Rust server binary directly
    (["/home/u/.local/share/pipx/venvs/m3-memory/lib/python3.13/site-packages/m3_core_rs/m3-embed-server"],
     "embed-server"),
    # a Linux console script runs as interpreter + script
    (["/v/bin/python", "/home/u/.local/bin/mcp-memory"], "mcp"),
    ([r"C:\py\python.exe", r"C:\v\bin\mcp_proxy.py"], "mcp"),
]


@pytest.mark.parametrize("argv", MENTIONS_ONLY)
def test_naming_a_writer_does_not_make_a_process_one(argv):
    assert m3_halt.writer_role(argv) is None, argv


@pytest.mark.parametrize("argv, role", REAL_WRITERS)
def test_every_real_launch_shape_is_still_found(argv, role):
    assert m3_halt.writer_role(argv) == role, argv
