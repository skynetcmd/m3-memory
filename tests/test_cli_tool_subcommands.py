"""Regression tests for the generated `m3 <domain> <tool>` human CLI surface.

PR 2 of the dual-surface tool-access work (see
docs/DUAL_SURFACE_TOOL_ACCESS_PLAN.md). `m3_memory/cli.py` grows a codegen
helper (`_add_tool_domain_subcommands`) that turns every catalog tool into a
`m3 <domain> <tool>` subcommand, dispatched through the SAME
`mcp_tool_catalog.execute_tool_structured` path that `m3_call` uses — so the
two surfaces cannot drift.

These tests drive the CLI via subprocess (`python -m m3_memory.cli ...`) so we
can assert on real process exit codes, and compare the structured `result`
against an in-process `execute_tool_structured` call for round-trip parity.

Conventions mirrored from test_lazy_tool_loading.py: bin/ is on sys.path via
conftest.py; we resolve the shipped DB at an absolute path relative to the repo
root.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# conftest.py already puts bin/ on sys.path; belt-and-suspenders for isolation.
_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import mcp_tool_catalog  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FILES_DB = _REPO_ROOT / "memory" / "files_database.db"


def _run_cli(*argv: str) -> subprocess.CompletedProcess:
    """Invoke `python -m m3_memory.cli <argv...>` and capture output.

    cwd is the repo root so relative DB defaults / bin discovery behave like a
    dev checkout.
    """
    return subprocess.run(
        [sys.executable, "-m", "m3_memory.cli", *argv],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )


# ── files_stats: happy path ──────────────────────────────────────────────────

@pytest.mark.skipif(not _FILES_DB.is_file(), reason="shipped files_database.db absent")
def test_files_stats_returns_json_with_file_nodes_total():
    """`m3 files files_stats --database <db>` exits 0 and emits parseable JSON
    carrying the corpus counter `file_nodes_total`."""
    proc = _run_cli("files", "files_stats", "--database", str(_FILES_DB))
    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"
    data = json.loads(proc.stdout)
    assert "file_nodes_total" in data, data
    assert isinstance(data["file_nodes_total"], int)


@pytest.mark.skipif(not _FILES_DB.is_file(), reason="shipped files_database.db absent")
def test_files_stats_roundtrip_parity_with_execute_tool_structured():
    """The `result` from the CLI surface must equal what the m3_call path
    (execute_tool_structured) returns for the same tool + args. Both surfaces
    share the impl, so any divergence is a dispatch bug.

    The catalog mutates the args dict it's handed (pops `database`), so the
    in-process comparison call gets its own copy.
    """
    proc = _run_cli("files", "files_stats", "--database", str(_FILES_DB))
    assert proc.returncode == 0, proc.stderr
    cli_result = json.loads(proc.stdout)

    spec = next(t for t in mcp_tool_catalog.TOOLS if t.name == "files_stats")
    direct = asyncio.run(
        mcp_tool_catalog.execute_tool_structured(
            spec, {"database": str(_FILES_DB)}, agent_id="", dry_run=False
        )
    )
    # Normalize through json so dict/tuple coercion matches the CLI's
    # json.dumps(default=str) boundary.
    direct_norm = json.loads(json.dumps(direct, default=str))
    assert cli_result == direct_norm, (
        f"CLI surface diverged from m3_call path:\n"
        f"cli={cli_result}\ndirect={direct_norm}"
    )


# ── --help lists generated tools ─────────────────────────────────────────────

def test_files_help_lists_generated_tools():
    """`m3 files --help` exits 0 and lists the generated file tools."""
    proc = _run_cli("files", "--help")
    assert proc.returncode == 0, proc.stderr
    assert "files_stats" in proc.stdout
    assert "files_search" in proc.stdout


# ── destructive gate ─────────────────────────────────────────────────────────

_ZERO_UUID = "00000000-0000-0000-0000-000000000000"


def test_destructive_tool_without_yes_is_gated():
    """`memory memory_delete` (a destructive tool) without --yes must refuse:
    exit 2, stderr mentions --yes."""
    proc = _run_cli("memory", "memory_delete", "--id", _ZERO_UUID)
    assert proc.returncode == 2, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "--yes" in proc.stderr, proc.stderr


def test_destructive_tool_dry_run_needs_no_yes():
    """`--dry-run` validates + gate-checks without executing and without --yes:
    exit 0, stdout JSON has dry_run True."""
    proc = _run_cli("memory", "memory_delete", "--id", _ZERO_UUID, "--dry-run")
    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"
    data = json.loads(proc.stdout)
    assert data.get("dry_run") is True, data
    assert data.get("ok") is True, data


# ── complex-arg tools take --json ────────────────────────────────────────────

def test_complex_arg_tool_rejects_invalid_json():
    """A complex-arg tool (task_create) with malformed --json exits 2."""
    proc = _run_cli("tasks", "task_create", "--json", "{invalid")
    assert proc.returncode == 2, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "json" in proc.stderr.lower(), proc.stderr


def test_complex_arg_tool_accepts_valid_json_dry_run():
    """A valid --json object on a complex-arg tool validates cleanly under
    --dry-run (no mutation): exit 0, dry_run True."""
    proc = _run_cli(
        "tasks", "task_create", "--json", json.dumps({"subject": "x"}), "--dry-run"
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"
    data = json.loads(proc.stdout)
    assert data.get("dry_run") is True, data


# ── chat namespace (chatlog domain → `chat` command + op subcommands) ─────────

def test_chat_status_is_operational_and_exits_zero():
    """`m3 chat status` runs the operational chatlog status command (not a
    generated data-tool) and exits 0."""
    proc = _run_cli("chat", "status")
    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"


def test_chat_help_lists_both_operational_and_data_tools():
    """`m3 chat --help` is the single chatlog namespace: it lists BOTH the
    operational ops (init/status) AND the generated chatlog data-tools
    (chatlog_search)."""
    proc = _run_cli("chat", "--help")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "init" in out, out
    assert "status" in out, out
    assert "chatlog_search" in out, out


def test_chatlog_status_alias_still_works():
    """`m3 chatlog status` (the pre-existing back-compat alias) still exits 0."""
    proc = _run_cli("chatlog", "status")
    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"


# ── exclusions: dispatcher/meta tools are NOT generated subcommands ───────────

def test_m3_call_is_not_a_generated_subcommand():
    """m3_call is excluded from the human CLI surface — `m3 admin m3_call`
    must fail (argparse rejects the unknown choice, exit 2)."""
    proc = _run_cli("admin", "m3_call")
    assert proc.returncode != 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"


def test_admin_help_does_not_list_m3_call():
    """The admin domain --help must not advertise the excluded m3_call."""
    proc = _run_cli("admin", "--help")
    assert proc.returncode == 0, proc.stderr
    assert "m3_call" not in proc.stdout, proc.stdout
