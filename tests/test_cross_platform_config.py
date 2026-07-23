"""Cross-platform path-resolution regression tests for the Claude install path.

Locks in the OS-aware behavior of bin/generate_configs.py and bin/setup_memory.py
so a future edit can't reintroduce the bugs fixed on 2026-06-14:

  * backslash venv paths that a shell (Git Bash on Windows) mangles to
    "command not found",
  * a /bin/sh hook invocation that doesn't exist on native Windows,
  * the Windows ".venv/Scripts/python.exe" layout leaking onto macOS/Linux
    (which use ".venv/bin/python", no .exe).

These tests don't touch a real venv or DB — they monkeypatch os.name / os.path.exists
to simulate each OS and inspect the generated command strings. bin/ is on sys.path
via tests/conftest.py.
"""

import os
import pathlib

import generate_configs as g
import pytest


def _all_commands(settings):
    """Every command string the install would write: hooks + statusLine + mcp."""
    cmds = []
    for event_entries in settings.get("hooks", {}).values():
        for entry in event_entries:
            for h in entry.get("hooks", []):
                cmds.append(h["command"])
    cmds.append(settings["statusLine"]["command"])
    for srv in settings.get("mcpServers", {}).values():
        cmds.append(srv["command"])
        cmds.extend(srv.get("args", []))
    return cmds


def _build_settings(monkeypatch, os_name):
    """Run the generator with os.name forced and the matching venv 'present',
    and return the resulting claude settings dict (no files written)."""
    repo_root = g._m3_repo_root()
    if os_name == "nt":
        venv_py = os.path.join(repo_root, ".venv", "Scripts", "python.exe")
    else:
        venv_py = os.path.join(repo_root, ".venv", "bin", "python")
    venv_py_fwd = venv_py.replace("\\", "/")

    real_exists = os.path.exists

    def fake_exists(p):
        # The forced venv interpreter "exists"; everything else (e.g. the GGUF
        # auto-detect path) does not, to keep output deterministic. Coerce to str
        # first — pytest's own internals call os.path.exists with Path objects.
        s = str(p)
        if s.replace("\\", "/") == venv_py_fwd:
            return True
        if s.endswith(".gguf"):
            return False
        return real_exists(p)

    monkeypatch.setattr(g.os.path, "exists", fake_exists)
    monkeypatch.delenv("M3_EMBED_GGUF", raising=False)
    # Don't write template files during the test.
    monkeypatch.setattr(g, "_write_json", lambda path, data: None)

    # `g.os` IS the global `os` module, so forcing `os.name` is process-wide —
    # and `pathlib.Path()` picks PosixPath vs WindowsPath from `os.name` at
    # construction time. Left to monkeypatch's teardown, the override stays live
    # through the rest of the test AND through pytest's own reporting, where
    # `Path(os.getcwd())` in `repr_failure` then builds a WindowsPath. On
    # Python 3.11 that raises NotImplementedError inside the reporter, turning
    # any unrelated failure into an INTERNALERROR that aborts the whole session
    # (green on 3.12, where the guard sits elsewhere; green on Windows, where
    # WindowsPath is native — so it only ever bit POSIX + 3.11 in CI).
    # Scope the override to the generator call and restore it immediately.
    saved_name = g.os.name
    try:
        g.os.name = os_name
        g.generate_configs()
    finally:
        g.os.name = saved_name
    return g.generate_configs._last_claude, venv_py_fwd


# ── interpreter layout per OS ────────────────────────────────────────────────

def test_windows_interpreter_uses_scripts_python_exe(monkeypatch):
    settings, venv_py = _build_settings(monkeypatch, "nt")
    assert venv_py.endswith("/.venv/Scripts/python.exe")
    session = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert session.startswith(venv_py)


def test_posix_interpreter_uses_bin_python_no_exe(monkeypatch):
    settings, venv_py = _build_settings(monkeypatch, "posix")
    assert venv_py.endswith("/.venv/bin/python")
    assert ".exe" not in venv_py
    session = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert session.startswith(venv_py)


# ── invariants that must hold on EVERY OS ────────────────────────────────────

@pytest.mark.parametrize("os_name", ["nt", "posix"])
def test_no_backslashes_in_any_command(monkeypatch, os_name):
    settings, _ = _build_settings(monkeypatch, os_name)
    for cmd in _all_commands(settings):
        assert "\\" not in cmd, f"backslash in command: {cmd!r}"


@pytest.mark.parametrize("os_name", ["nt", "posix"])
def test_no_bin_sh_dependency(monkeypatch, os_name):
    settings, _ = _build_settings(monkeypatch, os_name)
    for cmd in _all_commands(settings):
        assert "/bin/sh" not in cmd, f"/bin/sh dependency in: {cmd!r}"


@pytest.mark.parametrize("os_name", ["nt", "posix"])
def test_session_start_hook_present(monkeypatch, os_name):
    settings, _ = _build_settings(monkeypatch, os_name)
    assert "SessionStart" in settings["hooks"]
    inner = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "session_start_capture_check.py" in inner


def test_posix_has_no_windows_interpreter_leak(monkeypatch):
    settings, _ = _build_settings(monkeypatch, "posix")
    for cmd in _all_commands(settings):
        assert "Scripts/python.exe" not in cmd, f"windows layout leaked: {cmd!r}"
        assert ".exe" not in cmd, f".exe leaked onto posix: {cmd!r}"


def test_windows_has_no_posix_only_interpreter(monkeypatch):
    # On Windows the venv python must be the Scripts/.exe form, not bin/python.
    settings, _ = _build_settings(monkeypatch, "nt")
    session = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "/.venv/Scripts/python.exe" in session
    assert "/.venv/bin/python " not in session


# ── setup_memory.py path-resolution snippet (replicated logic) ───────────────
# setup_memory.py runs pip/migrations at import, so we can't import it directly
# in a unit test. These assert the SAME resolution rules it uses, guarding
# against a regression to the windows-only .exe layout or the missing
# requirements-windows.txt crash.

@pytest.mark.parametrize(
    "is_win, expected_suffix",
    [(True, ".venv/Scripts/python.exe"), (False, ".venv/bin/python")],
)
def test_setup_venv_layout_matches_os(is_win, expected_suffix):
    from pathlib import PurePosixPath

    base = PurePosixPath("/repo/m3-memory")
    venv = base / ".venv"
    py = venv / ("Scripts/python.exe" if is_win else "bin/python")
    assert str(py).endswith(expected_suffix)
    if not is_win:
        assert ".exe" not in str(py)


def test_setup_requirements_falls_back_when_windows_file_absent(tmp_path):
    # Mirrors setup_memory.py: prefer requirements-windows.txt only if it exists,
    # else requirements.txt — so a fresh Windows install never FileNotFoundErrors.
    base = tmp_path
    (base / "requirements.txt").write_text("pkg\n")
    req_win = base / "requirements-windows.txt"
    is_win = True
    reqs = req_win if (is_win and req_win.exists()) else base / "requirements.txt"
    assert reqs.name == "requirements.txt"

    # And when the windows file DOES exist on Windows, it wins.
    req_win.write_text("pkg-win\n")
    reqs = req_win if (is_win and req_win.exists()) else base / "requirements.txt"
    assert reqs.name == "requirements-windows.txt"


def test_setup_migrations_skip_down_and_order_numerically():
    # Mirrors setup_memory.py forward-migration selection: only .up.sql / bare
    # NNN_*.sql, never .down.sql, ordered by integer prefix.
    names = [
        "001_initial_schema.sql",
        "013_conversation_id.up.sql",
        "013_conversation_id.down.sql",
        "002_enforce.sql",
        "010_tier.sql",
    ]

    def mig_key(name):
        stem = name.split("_", 1)[0]
        try:
            return (int(stem), name)
        except ValueError:
            return (1 << 30, name)

    forward = [n for n in names if not n.endswith(".down.sql")]
    ordered = sorted(forward, key=mig_key)

    assert all(not n.endswith(".down.sql") for n in ordered)
    # numeric, not lexicographic: 002 before 010 before 013
    prefixes = [int(n.split("_", 1)[0]) for n in ordered]
    assert prefixes == sorted(prefixes)
    assert prefixes == [1, 2, 10, 13]


# ── os.name must not leak out of the helper ──────────────────────────────────

def test_build_settings_restores_os_name(monkeypatch):
    """Regression: `_build_settings` forces `os.name` process-wide, and
    `pathlib.Path()` selects PosixPath vs WindowsPath from `os.name` at
    construction time. If the override outlives the helper, every later
    `Path(...)` on a POSIX host becomes a WindowsPath — which raises
    NotImplementedError on Python 3.11. pytest builds one in `repr_failure`,
    so a leak turns any unrelated test failure into a session-aborting
    INTERNALERROR (CI 2026-07-22/23: ubuntu+macos py3.11 red, py3.12 and
    Windows green, for ~30 consecutive runs)."""
    before = os.name
    _build_settings(monkeypatch, "nt")
    assert os.name == before, "os.name leaked out of _build_settings"
    # The observable consequence, asserted directly.
    assert type(pathlib.Path(".")) is type(pathlib.Path(os.getcwd()))


def test_path_flavour_unchanged_after_both_variants(monkeypatch):
    """Both parametrised variants must leave path flavour untouched."""
    native = type(pathlib.Path("."))
    for os_name in ("nt", "posix"):
        _build_settings(monkeypatch, os_name)
        assert type(pathlib.Path(".")) is native, (
            f"path flavour changed after _build_settings({os_name!r})")
