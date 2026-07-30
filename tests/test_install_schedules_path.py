"""install_schedules.py must be found on a WHEEL install, not just a repo checkout.

`m3 install-m3 --cognitive-loop` (and the dashboard task) locate
bin/install_schedules.py to register a launchd/systemd/schtasks service. The old
`Path(__file__).parent.parent / "bin"` only resolved in a repo checkout: on a
pip/pipx install bin/ lives INSIDE the package (m3_memory/bin/), so the path
pointed at a non-existent site-packages/bin and the step silently no-op'd — the
cognitive loop NEVER registered on a real install while `[OK]` still printed.
The fix routes through the canonical bin_dir() resolver and makes the register
step return a bool so the `[OK]` is only printed when it actually registered.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from m3_memory import installer  # noqa: E402


def test_no_repo_only_bin_path_for_install_schedules():
    """The known-broken `parent.parent / "bin" / "install_schedules.py"` literal
    must not reappear — it is invisible in a repo checkout (where it works) and
    only breaks on real installs."""
    src = Path(installer.__file__).read_text(encoding="utf-8")
    assert 'parent.parent / "bin" / "install_schedules.py"' not in src


def test_install_schedules_script_uses_bin_dir(monkeypatch, tmp_path):
    # bin_dir() points at a payload bin/ that HAS the script -> returned.
    bd = tmp_path / "bin"
    bd.mkdir()
    (bd / "install_schedules.py").write_text("# stub\n")
    monkeypatch.setattr(installer, "bin_dir", lambda: bd)
    got = installer._install_schedules_script()
    assert got == bd / "install_schedules.py"

    # Script absent -> None (caller must not try to run it).
    (bd / "install_schedules.py").unlink()
    assert installer._install_schedules_script() is None

    # No payload bin/ at all -> None, never a crash.
    monkeypatch.setattr(installer, "bin_dir", lambda: None)
    assert installer._install_schedules_script() is None


def test_register_cognitive_loop_returns_true_only_on_success(monkeypatch, tmp_path):
    script = tmp_path / "install_schedules.py"
    script.write_text("# stub\n")
    monkeypatch.setattr(installer, "_install_schedules_script", lambda: script)
    monkeypatch.setattr(installer, "_python_exe", lambda: "python3")

    class _P:
        def __init__(self, rc): self.returncode = rc

    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: _P(0))
    assert installer._register_cognitive_loop_task() is True

    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: _P(1))
    assert installer._register_cognitive_loop_task() is False

    # No script on the payload -> False (the wheel-install failure mode).
    monkeypatch.setattr(installer, "_install_schedules_script", lambda: None)
    assert installer._register_cognitive_loop_task() is False


def test_prompt_prints_honest_failure_when_not_registered(monkeypatch, capsys):
    """When registration fails, the loud [OK] must NOT print — a [!] does."""
    monkeypatch.setattr(installer, "_register_cognitive_loop_task", lambda: False)
    installer._prompt_and_install_cognitive_loop(interactive=False, forced=True)
    out = capsys.readouterr().out
    assert "[OK] cognitive loop installed" not in out
    assert "NOT registered" in out


def test_prompt_prints_ok_when_registered(monkeypatch, capsys):
    monkeypatch.setattr(installer, "_register_cognitive_loop_task", lambda: True)
    did = installer._prompt_and_install_cognitive_loop(interactive=False, forced=True)
    out = capsys.readouterr().out
    assert "[OK] cognitive loop installed" in out
    assert did is True


def test_prompt_returns_false_headless_without_flag(monkeypatch):
    # Headless, no --cognitive-loop -> did nothing -> False (the refuse path).
    did = installer._prompt_and_install_cognitive_loop(interactive=False, forced=False)
    assert did is False


# ── install_m3 exit semantics: --cognitive-loop on an existing payload ───────

def _stub_install_m3_env(monkeypatch, tmp_path):
    """Existing-payload sandbox: repo_path present, config helpers redirected."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    monkeypatch.setattr(installer, "config_dir", lambda: tmp_path / ".m3")
    monkeypatch.setattr(installer, "config_file", lambda: tmp_path / ".m3" / "config.json")
    # Neutralize the pre-guard side-effect prompts except cognitive-loop, which
    # each test drives explicitly.
    monkeypatch.setattr(installer, "_prompt_and_install_dashboard", lambda interactive: None)
    return repo_path


def test_install_m3_cognitive_loop_flag_is_success_on_existing_payload(monkeypatch, tmp_path, capsys):
    """`m3 install-m3 --cognitive-loop` on an already-present payload must NOT
    raise 'already installed' — the loop WAS the requested work. Returns the
    existing bridge (exit 0), not a RuntimeError (exit 1)."""
    repo_path = _stub_install_m3_env(monkeypatch, tmp_path)
    monkeypatch.setattr(installer, "_register_cognitive_loop_task", lambda: True)
    # find_bridge resolves the existing payload bridge.
    fake_bridge = repo_path / "bin" / "memory_bridge.py"
    monkeypatch.setattr(installer, "find_bridge", lambda: fake_bridge)

    out = installer.install_m3(repo_path=repo_path, tag="v1.2.3", cognitive_loop=True)
    assert out == fake_bridge                      # exit-0 success, bridge returned
    assert "left in place" in capsys.readouterr().out


def test_install_m3_bare_still_refuses_without_force(monkeypatch, tmp_path):
    """The base case is unchanged: no sub-action + existing payload + no force
    still raises the actionable 'already installed' refusal."""
    import pytest
    repo_path = _stub_install_m3_env(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="already installed"):
        installer.install_m3(repo_path=repo_path, tag="v1.2.3")  # no cognitive_loop
