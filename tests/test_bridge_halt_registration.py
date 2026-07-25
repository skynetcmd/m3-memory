"""The MCP server must register with the HALT protocol.

It is typically the LONGEST-LIVED DB writer on a desktop install -- it lives as
long as the agent session -- yet it was the one writer that never registered.
HALT_PROTOCOL.md's rollout section deferred embed-server and MCP-server
HALT-honoring as a "fast follow" that never landed, so until now an exclusive op
could find this process only via the psutil cmdline scan.

That scan is a best-effort net, not a substitute:
  - it cannot read an ELEVATED process's cmdline (AccessDenied)
  - it depends on signature tables matching however the client launched us, and
    the Claude Code plugin runs bare `m3`, which those tables did not know

Registering makes the server discoverable by the reliable path, so a migration
waits for it to quiesce instead of proceeding against an open WAL-mode DB.
"""

import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(REPO, "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)


@pytest.fixture
def halt(tmp_path, monkeypatch):
    import m3_halt
    monkeypatch.setenv("M3_ENGINE_ROOT", str(tmp_path))
    return m3_halt


def test_bridge_registers_and_deregisters(halt):
    """Round-trip the exact calls memory_bridge makes at startup."""
    halt.register_process("mcp", engine_root=None, extra={"transport": "stdio"})
    roles = [w.role for w in halt.list_live_processes()]
    assert "mcp" in roles, roles

    halt.deregister("mcp")
    assert "mcp" not in [w.role for w in halt.list_live_processes()]


def test_registered_mcp_appears_in_the_union(halt):
    """list_all_db_writers is what an exclusive op consults."""
    halt.register_process("mcp", extra={"transport": "stdio"})
    try:
        assert "mcp" in [w.role for w in halt.list_all_db_writers()]
    finally:
        halt.deregister("mcp")


def test_transport_is_recorded_for_the_operator(halt):
    """The prompt in HALT_PROTOCOL.md step 5 names the stuck holder; recording
    the transport tells the human WHICH server they are being asked about."""
    import json
    entry = halt.register_process("mcp", extra={"transport": "streamable-http"})
    try:
        payload = json.loads(entry.read_text(encoding="utf-8"))
        assert payload.get("extra", {}).get("transport") == "streamable-http"
    finally:
        halt.deregister("mcp")


def test_bridge_source_registers_before_serving():
    """Registration must wrap the server's real lifetime -- registering after
    mcp.run() would never execute, since run() blocks until shutdown."""
    with open(os.path.join(BIN, "memory_bridge.py"), encoding="utf-8") as fh:
        src = fh.read()
    reg = src.find('register_process("mcp"')
    assert reg != -1, "memory_bridge does not register with the HALT protocol"

    run_calls = [m.start() for m in re.finditer(r"^\s*mcp\.run\(", src, re.M)]
    assert run_calls, "no mcp.run() found"
    assert reg < min(run_calls), "registration must precede mcp.run()"


def test_bridge_registration_is_never_fatal():
    """Coordination is best-effort by contract: a registry failure must not stop
    the server from serving."""
    with open(os.path.join(BIN, "memory_bridge.py"), encoding="utf-8") as fh:
        src = fh.read()
    idx = src.find('register_process("mcp"')
    window = src[max(0, idx - 400):idx + 400]
    assert "try:" in window and "except" in window, (
        "HALT registration must be wrapped so a failure degrades coordination "
        "rather than breaking the MCP server"
    )
