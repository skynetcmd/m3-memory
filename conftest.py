"""Repo-root conftest: ensure no test ever writes to the production DBs.

The bin/ test scripts (e.g., bin/test_bulk_parity.py) historically did not
patch DB_PATH and leaked fixture rows into memory/agent_memory.db. This
fixture forces M3_DATABASE / CHATLOG_DB_PATH / memory_core.DB_PATH to a
per-test tmp dir for every test that runs.
"""
from __future__ import annotations
import sys
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent
_BIN = REPO_ROOT / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

# Files at the repo root that pytest must NOT try to collect.
# CLAUDE.md and GEMINI.md were once tracked as git symlinks (mode
# 120000) pointing at docs/AGENT_INSTRUCTIONS.md so coding agents pick
# up the instructions. They've been converted to regular files in
# 2026-05-01 to fix Windows pytest collection (broken symlinks tripped
# scandir+is_file with OSError WinError 123). Belt-and-braces: keep
# them out of pytest collection regardless of how they're stored.
collect_ignore = ["CLAUDE.md", "GEMINI.md"]


@pytest.fixture(autouse=True)
def _isolate_db_paths(tmp_path, monkeypatch):
    """Force every test to use a tmp DB. Belt-and-braces against tests
    that import memory_core (which captures DB_PATH at import time)
    AND tests that read the env vars on each call."""
    tmp_main = tmp_path / "test_agent_memory.db"
    tmp_chatlog = tmp_path / "test_agent_chatlog.db"
    monkeypatch.setenv("M3_DATABASE", str(tmp_main))
    monkeypatch.setenv("CHATLOG_DB_PATH", str(tmp_chatlog))
    # Also patch memory_core.DB_PATH if already imported, so tests
    # that captured the constant at import time still get the tmp path.
    if "memory_core" in sys.modules:
        monkeypatch.setattr("memory_core.DB_PATH", str(tmp_main), raising=False)
    yield
