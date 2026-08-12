"""find_python() must reject an interpreter that cannot import the ingest deps.

Regression for the 2026-08-09..11 outage: a bare `python -m venv` (created with
include-system-site-packages=false and no packages installed) appeared beside the
installed payload. Every hook's find_python() picked it purely because the path
`.exists()`, then chatlog_ingest died at `import httpx` before printing any JSON.
run_ingest() saw no parseable stdout and reported the generic "m3 ingest failed
or unreachable", so chatlog capture was silently dead for ~2 days while the hook
configuration still looked correct — the only visible symptom was a stale
last_write_at.

The guard is `usable_interpreter()`: probe the import, never trust the path.
These tests must hold for EVERY agent hook, since the selection logic is
duplicated across them by design (the hooks run standalone, without package
imports, so they cannot share a helper module).
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import venv

import pytest

HOOK_DIR = os.path.join(os.path.dirname(__file__), "..", "bin", "hooks", "chatlog")
HOOK_MODULES = [
    "claude_code_precompact",
    "gemini_cli_onexit",
    "opencode_session_end",
]


def _load(name):
    path = os.path.join(HOOK_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"_hook_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _venv_python(root):
    for rel in (("Scripts", "python.exe"), ("bin", "python")):
        p = root.joinpath(*rel)
        if p.exists():
            return p
    return None


@pytest.fixture(scope="module")
def stub_venv(tmp_path_factory):
    """A real but dependency-less venv — the exact shape that caused the outage."""
    root = tmp_path_factory.mktemp("stub_repo") / ".venv"
    venv.create(root, with_pip=False, system_site_packages=False)
    py = _venv_python(root)
    if py is None:
        pytest.skip("could not create a stub venv on this platform")
    # Sanity: the stub must genuinely lack httpx, or the test proves nothing.
    rc = subprocess.run([str(py), "-c", "import httpx"],
                        capture_output=True).returncode
    if rc == 0:
        pytest.skip("stub venv unexpectedly has httpx; cannot test the guard")
    return root


@pytest.mark.parametrize("name", HOOK_MODULES)
def test_usable_interpreter_rejects_dependency_less_venv(name, stub_venv):
    mod = _load(name)
    assert mod.usable_interpreter(_venv_python(stub_venv)) is False


@pytest.mark.parametrize("name", HOOK_MODULES)
def test_usable_interpreter_accepts_the_running_interpreter(name):
    """sys.executable got here, so it necessarily has the deps."""
    mod = _load(name)
    assert mod.usable_interpreter(sys.executable) is True


@pytest.mark.parametrize("name", HOOK_MODULES)
def test_find_python_falls_through_when_repo_venv_is_broken(name, stub_venv):
    """THE regression: a stub .venv must not be selected over a working python."""
    mod = _load(name)
    repo = stub_venv.parent
    chosen = mod.find_python(repo)
    assert chosen == sys.executable
    assert str(stub_venv) not in chosen


@pytest.mark.parametrize("name", HOOK_MODULES)
def test_usable_interpreter_is_false_for_missing_path(name, tmp_path):
    mod = _load(name)
    assert mod.usable_interpreter(tmp_path / "nope" / "python.exe") is False


@pytest.mark.parametrize("name", HOOK_MODULES)
def test_find_python_prefers_a_working_repo_venv(name, monkeypatch, tmp_path):
    """The guard must not break the feature: a GOOD repo venv is still chosen.

    Dev checkouts rely on find_python() picking the repo-local venv. Simulate one
    by pointing the candidate at the current (working) interpreter.
    """
    mod = _load(name)
    good = tmp_path / ".venv" / "Scripts" / "python.exe"
    good.parent.mkdir(parents=True)
    good.write_text("")  # exists(); usability is stubbed below

    monkeypatch.setattr(mod, "usable_interpreter", lambda c: str(c) == str(good))
    assert mod.find_python(tmp_path) == str(good)


# ── scheduled-task interpreter: existence is not usability ───────────────────

def _sched():
    import install_schedules
    return install_schedules


def test_scheduled_task_interpreter_rejects_a_dependency_less_venv(tmp_path):
    """A registered task BAKES IN an absolute interpreter path and runs under
    pythonw.exe, which has no console. Binding one that cannot import m3's deps
    means every fire dies silently, forever, with nothing but a non-zero exit in
    the task history.

    A stub .venv beside the installed payload did exactly this to all seven
    AgentOS_* jobs (2026-08-11) — the same stub that had already killed chatlog
    capture through the hook-side resolver.
    """
    import venv as _venv

    isch = _sched()
    root = tmp_path / "payload"
    (root).mkdir()
    _venv.create(root / ".venv", with_pip=False, system_site_packages=False)

    scripts = "Scripts" if os.name == "nt" else "bin"
    exe = "python.exe" if os.name == "nt" else "python"
    stub = root / ".venv" / scripts / exe
    if not stub.exists():
        pytest.skip("could not create a stub venv on this platform")
    if isch._interpreter_has_deps(str(stub)):
        pytest.skip("stub venv unexpectedly has httpx; cannot test the guard")

    assert isch._interpreter_has_deps(str(stub)) is False
    # ...and the resolver must NOT hand that path to a task registration.
    resolved = isch._venv_python(str(root))
    assert os.path.abspath(resolved) != os.path.abspath(str(stub))


def test_scheduled_task_interpreter_accepts_the_running_one():
    """sys.executable got here, so it necessarily has the deps."""
    assert _sched()._interpreter_has_deps(sys.executable) is True


def test_scheduled_task_interpreter_rejects_a_missing_path(tmp_path):
    assert _sched()._interpreter_has_deps(str(tmp_path / "nope" / "python.exe")) is False
