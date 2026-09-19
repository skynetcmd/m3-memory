"""A structured-argument payload too large for the shell needs a documented path.

The ceiling is CMD.EXE, not the OS. Measured 2026-09-14: the same 20,061-char
command line SUCCEEDS via CreateProcess directly (subprocess shell=False) and
FAILS with "The command line is too long." through cmd.exe (shell=True).
cmd.exe caps at 8,191 chars; Windows CreateProcess allows ~32 KB. An earlier
note in this repo blamed "the Windows argv limit" -- that was measuring the
shell wrapper, not the platform, and the distinction matters: the same call is
fine from a script and broken from a terminal.

The shell refuses before Python starts, so no in-process error handling can
catch it. The only place a caller can learn this is the help text, before they
size a payload -- which is what these tests pin.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))


def _cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "m3_memory.cli", *args],
        capture_output=True, text=True, cwd=_ROOT,
    )


def test_json_help_points_at_json_file_for_large_payloads():
    """The signpost. Without it a caller hits a raw OS error with no hint that
    a working alternative is one flag away."""
    out = _cli("admin", "notify", "--help").stdout
    assert "--json-file" in out
    body = out[out.index("--json "):]
    assert "json-\n" in body or "json-file" in body, (
        "--json help must name --json-file as the large-payload path"
    )
    assert "command line" in body.lower(), (
        "--json help must say WHY large payloads fail -- the OS argv cap, not "
        "anything this program controls"
    )


def test_json_file_accepts_a_payload_too_large_for_argv():
    """The capability itself: bigger than the measured argv ceiling."""
    blob = "x" * 20000          # ~2.5x the measured ~8.1 KB argv ceiling
    payload = {
        "agent_id": "pytest-large@s1",
        "kind": "probe",
        "payload": {"blob": blob},
    }
    path = os.path.join(tempfile.mkdtemp(), "big.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)

    r = _cli("admin", "notify", "--yes", "--dry-run", "--json-file", path)
    assert r.returncode == 0, f"--json-file rejected a large payload: {r.stderr}"
    assert '"ok": true' in r.stdout, r.stdout


def test_the_ceiling_is_the_shell_not_the_platform():
    """THE correction. A payload that cmd.exe refuses is fine without it.

    Written as a failed first attempt: asserting `--json` must fail for 20 KB
    passed only because that run went through a shell. Invoked directly it
    succeeds, which is why the help text names cmd.exe rather than "Windows".
    """
    import pytest

    if sys.platform != "win32":
        pytest.skip("cmd.exe ceiling is Windows-specific")

    # Resolve the console script instead of invoking a bare name. With
    # shell=False there is no PATH search, so a bare "m3" raises
    # FileNotFoundError [WinError 2] wherever the entry point is not installed
    # -- which is every CI lane running the hermetic suite from a source
    # checkout. That error is NOT the ceiling this test measures, but it failed
    # the lane as though it were. (Same trap as the npm .CMD shim in
    # setup_wizard._wire_openclaw: on Windows, resolve, never assume PATH.)
    exe = shutil.which("m3")
    if not exe:
        pytest.skip("`m3` console script not installed; nothing to measure")

    payload = json.dumps({
        "agent_id": "pytest-large@s1", "kind": "probe",
        "payload": {"blob": "x" * 20000},
    })
    argv = [exe, "admin", "notify", "--yes", "--dry-run", "--json", payload]

    direct = subprocess.run(argv, capture_output=True, text=True, shell=False)
    viashell = subprocess.run(argv, capture_output=True, text=True, shell=True)

    assert direct.returncode == 0, (
        f"CreateProcess refused {len(payload)} chars -- the ~32KB platform "
        f"ceiling moved and the help text needs re-measuring: {direct.stderr}"
    )
    assert viashell.returncode != 0, (
        "cmd.exe accepted a payload over its 8,191-char cap -- if this ever "
        "holds, --json is safe at this size and the help text overstates it"
    )
