"""Pre-flight must SEE every m3 DB writer, or it quiesces nothing.

The HALT protocol's whole value is refusing to run an exclusive op while a
writer holds a WAL-mode DB open. A writer the scan cannot match is worse than no
scan at all: the pre-flight reports "no writers" and the migration proceeds
against an open DB, which is precisely the torn-WAL case the protocol exists to
prevent.

Two writers were invisible on a normal Windows desktop (2026-07-25):
  - the MCP server, which the Claude Code plugin launches as bare `m3`
    (mcp_config.json -> command: "m3"), not as `mcp-memory`
  - m3-embed-server.exe, the Rust embedder, which runs with an EMPTY cmdline
    so only the process-NAME fallback could ever match it -- and it was absent
    from that table entirely.
"""

import os
import sys

import pytest

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)


@pytest.fixture
def halt():
    import m3_halt
    return m3_halt


def _cmd_role(halt, cmdline):
    for role, sigs in halt._WRITER_CMDLINE_SIGNATURES.items():
        if any(s in cmdline for s in sigs):
            return role
    return None


def _name_role(halt, name):
    low = name.lower()
    for role, sigs in halt._WRITER_NAME_SIGNATURES.items():
        if any(s in low for s in sigs):
            return role
    return None


@pytest.mark.parametrize("cmdline", [
    r"C:\Users\u\.local\bin\m3.exe",
    "/home/u/.local/bin/m3.exe",
    r"C:\Users\u\pipx\venvs\m3-memory\Scripts\m3.exe serve",
])
def test_plugin_launched_mcp_server_is_matched(halt, cmdline):
    """The plugin runs `command: "m3"`. This matched NOTHING before."""
    assert _cmd_role(halt, cmdline) == "mcp", cmdline


def test_rust_embed_server_is_matched_by_name(halt):
    """Empty cmdline on Windows -> only the name fallback can catch it."""
    assert _name_role(halt, "m3-embed-server.exe") == "embed-server"


def test_m3_exe_matched_by_name_when_cmdline_unreadable(halt):
    """An ELEVATED m3.exe has an unreadable cmdline; the name must still match."""
    assert _name_role(halt, "m3.exe") == "mcp"


def test_legacy_entrypoints_still_matched(halt):
    """The rename must not drop the older signatures."""
    assert _cmd_role(halt, "/usr/bin/mcp-memory") == "mcp"
    assert _cmd_role(halt, "python bin/m3_cognitive_loop.py") == "cognitive-loop"
    assert _cmd_role(halt, "python bin/embed_server_inproc.py") == "embed-server"


@pytest.mark.parametrize("cmdline", [
    "python train_m3_model.py",       # 'm3' as a substring of a real word
    "/usr/bin/helm3 upgrade",         # ends in m3 but is not m3
    "python m3_analysis_notebook.py",
])
def test_unrelated_processes_are_not_claimed(halt, cmdline):
    """A false positive stops a legitimate upgrade, so anchor on separators."""
    assert _cmd_role(halt, cmdline) is None, cmdline
