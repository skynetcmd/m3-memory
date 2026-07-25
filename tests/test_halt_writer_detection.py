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


# ── Blocking vs. merely-running ──────────────────────────────────────────────
# Detection and blocking are different questions. The embed server holds a CUDA
# context, not a store, so it can never "release the DB": waiting on it burns
# the whole quiesce timeout and then asks the operator to kill a process that
# was never a hazard -- which is how people learn to reflex-dismiss the prompt.

def test_embed_server_does_not_block(halt):
    assert halt._role_blocks("embed-server") is False


def test_scan_suffixed_embed_server_also_does_not_block(halt):
    """scan_db_writer_processes appends "(elevated?)" when the cmdline is
    unreadable. That variant must classify the same as the clean role."""
    assert halt._role_blocks("embed-server(elevated?)") is False


def test_real_writers_still_block(halt):
    for role in ("cognitive-loop", "mcp", "m3?(elevated)"):
        assert halt._role_blocks(role) is True, role


def test_unknown_roles_block_by_default(halt):
    """Fail SAFE: an unrecognized writer must be treated as a hazard, never
    waved through."""
    assert halt._role_blocks("something-new") is True
    assert halt._role_blocks("") is True


def test_blocking_list_is_a_subset_of_all(halt, monkeypatch):
    from m3_core.registry_payload import ProcInfo

    def fake_all(engine_root=None):
        return [
            ProcInfo(pid=1, role="cognitive-loop", started_at="", engine_root="", path=None),
            ProcInfo(pid=2, role="embed-server(elevated?)", started_at="", engine_root="", path=None),
            ProcInfo(pid=3, role="mcp", started_at="", engine_root="", path=None),
        ]

    monkeypatch.setattr(halt, "list_all_db_writers", fake_all)
    blocking = halt.list_blocking_db_writers()
    assert [p.pid for p in blocking] == [1, 3], [p.role for p in blocking]


def test_wait_for_quiesce_ignores_non_blocking_roles(halt, monkeypatch):
    """The regression that motivated the split: an embed server running during
    an upgrade made wait_for_quiesce burn its full timeout and report stuck."""
    from m3_core.registry_payload import ProcInfo

    monkeypatch.setattr(halt, "list_all_db_writers", lambda engine_root=None: [
        ProcInfo(pid=2, role="embed-server", started_at="", engine_root="", path=None),
    ])
    res = halt.wait_for_quiesce(timeout=0.5, poll=0.05)
    assert res.ok is True, f"embed server wrongly blocked quiesce: {res.stuck}"
