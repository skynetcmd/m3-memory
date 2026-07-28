"""`sys.executable` is not always an interpreter — regression guard.

Under a pip/pipx console script (`m3.exe`, `mcp-memory.exe`) `sys.executable` is
the SHIM, not python. Any `subprocess.run([sys.executable, "script.py", ...])`
therefore re-enters the m3 CLI with a .py path as its subcommand, argparse
rejects it, and the child exits 2.

That silently truncated `m3 setup` (2026-07-27): the dashboard boot-task step
failed, the wizard aborted before Step 5, and it exited 1 — while
`python -m m3_memory.cli setup` completed and exited 0. Same code, same venv,
different launcher. Reproduced directly: passing a .py path to `m3.exe` prints
"invalid choice: '...py'" and returns 2.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from m3_memory._platform import python_exe  # noqa: E402


def test_returns_sys_executable_when_it_is_a_real_interpreter():
    """The common case (`python -m ...`, any bin/*.py) must be unchanged."""
    assert python_exe() == sys.executable
    assert Path(python_exe()).stem.lower().startswith("python")


def test_resolves_interpreter_beside_a_console_script_shim(monkeypatch, tmp_path):
    """Given `.../Scripts/m3.exe`, resolve `.../Scripts/python.exe` next to it."""
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    shim = scripts / ("m3.exe" if os.name == "nt" else "m3")
    shim.write_bytes(b"\x00")
    real = scripts / ("python.exe" if os.name == "nt" else "python3")
    real.write_bytes(b"\x00")

    monkeypatch.setattr(sys, "executable", str(shim))
    assert python_exe() == str(real), "did not resolve the interpreter beside the shim"


def test_resolves_python3_on_posix(monkeypatch, tmp_path):
    """POSIX layout: `.../bin/m3` shim next to `python3` (no plain `python`).

    m3 supports 3 OSes and console scripts exist on all of them, so the sibling
    search must try `python3` first on POSIX — a venv often has no bare `python`.
    """
    import m3_memory._platform as P

    bind = tmp_path / "bin"
    bind.mkdir()
    (bind / "m3").write_bytes(b" ")
    (bind / "python3").write_bytes(b" ")

    monkeypatch.setattr(P, "is_windows", lambda: False)
    monkeypatch.setattr(sys, "executable", str(bind / "m3"))
    assert P.python_exe() == str(bind / "python3")


@pytest.mark.parametrize("name", ["python", "python3", "python3.14", "pythonw", "python3.11"])
def test_versioned_interpreter_names_pass_through(monkeypatch, tmp_path, name):
    """Any real interpreter name is returned unchanged — including the
    versioned forms macOS/Linux use (`python3.11`) and Windows' `pythonw`."""
    exe = tmp_path / name
    exe.write_bytes(b" ")
    monkeypatch.setattr(sys, "executable", str(exe))
    assert python_exe() == str(exe)


def test_falls_back_rather_than_raising(monkeypatch, tmp_path):
    """No interpreter beside the shim -> return sys.executable unchanged.

    Degrading to the previous behaviour is correct; raising here would turn a
    spawn-helper into a new failure mode (§3 fail safe).
    """
    lonely = tmp_path / "m3.exe"
    lonely.write_bytes(b"\x00")
    monkeypatch.setattr(sys, "executable", str(lonely))
    assert python_exe() == str(lonely)


def test_no_package_module_spawns_a_script_via_sys_executable():
    """The actual guard: no module reachable from the console script may pass a
    .py path to `sys.executable`. Use `_python_exe()` instead.

    Scoped to `m3_memory/` because those modules run UNDER the shim. `bin/*.py`
    are themselves run by a real interpreter, where sys.executable is correct.
    """
    pkg = Path(__file__).resolve().parent.parent / "m3_memory"
    # cli.py re-execs the ORIGINAL launch (sys.orig_argv) to set -X utf8. There
    # sys.executable is exactly right — it re-runs the same shim rather than
    # handing it a script path. _platform.py only mentions the pattern in the
    # docstring that explains why not to use it.
    exempt = {"cli.py", "_platform.py"}
    offenders: list[str] = []
    for py in pkg.rglob("*.py"):
        if py.name in exempt:
            continue
        for n, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if "sys.executable" not in line or line.lstrip().startswith("#"):
                continue
            # `[sys.executable, "-m", ...]` / `"-c"` are safe: the shim never
            # sees a bare path. A script path (or a str()/Path variable) is not.
            if '"-m"' in line or "'-m'" in line or '"-c"' in line or "'-c'" in line:
                continue
            if "sys.executable," in line.replace(" ", ""):
                offenders.append(f"{py.relative_to(pkg.parent)}:{n}: {line.strip()}")
    assert not offenders, (
        "sys.executable passed a script path in code that runs under the console "
        "script shim; use m3_memory._platform.python_exe():\n  "
        + "\n  ".join(offenders)
    )


# ── `m3 setup` must exit 0 when it succeeds ─────────────────────────────────

@pytest.mark.parametrize("rc,blob,expect", [
    (0,  "Added MCP server memory",                    True),
    (1,  "MCP server memory already exists in user config", True),
    (1,  "Error: connection refused",                  False),
    (2,  "unknown option '--global'",                  False),
])
def test_claude_mcp_add_already_exists_is_success(monkeypatch, capsys, rc, blob, expect):
    """`claude mcp add` is idempotent in EFFECT but not in EXIT CODE.

    With the server already registered it prints "already exists" and exits 1.
    The old code ran it with inherited stdio and `check=False`, so that status
    leaked out as `m3 setup`'s own exit code — a completely successful re-run of
    setup exited 1, which would false-fail any CI or script gating on it.
    Already-registered must classify as success; a real error must not.
    """
    from types import SimpleNamespace

    from m3_memory import setup_wizard as sw

    monkeypatch.setattr(sw.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(
        sw.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=rc, stdout=blob, stderr=""),
    )
    assert sw._wire_claude("both") is expect
    if rc != 0 and expect:
        assert "already registered" in capsys.readouterr().out.lower()
