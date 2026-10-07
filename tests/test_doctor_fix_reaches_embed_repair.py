"""`m3 doctor --fix` must reach the embed-server repairs.

The --fix branch returns before doctor's report section. The exec-bit repair
and the stale-core restart both sat in that report section under `if
args.fix:`, so neither ever ran — the upgrade's `doctor --fix` step could not
restart a server left on a replaced core, which is what it was added for.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
_SKIP = ["--skip-shared-embedder", "--skip-dashboard", "--skip-environment",
         "--skip-agent-paths", "--skip-claude-mcp"]


def _load():
    if str(_BIN) not in sys.path:
        sys.path.insert(0, str(_BIN))
    spec = importlib.util.spec_from_file_location("memory_doctor_fix_test", _BIN / "memory_doctor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("extra, expect_called", [([], True), (["--skip-embed-server"], False)])
def test_fix_reaches_the_embed_server_repairs(monkeypatch, extra, expect_called):
    md = _load()
    import importlib
    doctor_pkg = importlib.import_module("memory.doctor")

    async def _fix_impl(dry_run=False):
        return {"summary": "nothing_to_do", "actions": []}

    monkeypatch.setattr(doctor_pkg, "memory_doctor_fix_impl", _fix_impl)
    called: list = []
    monkeypatch.setattr(md, "_repair_embed_server", lambda args: called.append(args.fix))
    monkeypatch.setattr(sys, "argv", ["memory_doctor.py", "--fix", *_SKIP, *extra])
    assert md.main() == 0
    assert called == ([True] if expect_called else [])
