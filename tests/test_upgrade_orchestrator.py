"""Pins install-method detection in bin/m3_upgrade.py.

The orchestrator's whole value is picking the RIGHT upgrade command. Guessing
wrong is worse than not running: ``pipx upgrade`` against a pip install exits 0
having upgraded nothing, which reads as success. So detection is what these
tests cover, across all three supported OS path shapes.

A real machine produced a false ``pip`` verdict for a ``pipx`` install, because
``~/.local/bin`` held unrelated ``python3.11.exe``/``python3.12.exe`` beside the
pipx shim and one of them imported ``m3_memory`` from a DEV CHECKOUT on the
ambient path. Both halves of that trap are pinned below.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
_SPEC = importlib.util.spec_from_file_location("m3_upgrade", _BIN / "m3_upgrade.py")
assert _SPEC and _SPEC.loader
m3u = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(m3u)


def _mk(base: pathlib.Path, rel: str) -> pathlib.Path:
    p = base / rel
    p.mkdir(parents=True, exist_ok=True)
    return p


def test_pipx_detected_by_metadata_file(tmp_path):
    """A pipx venv is identified by pipx_metadata.json at the venv root."""
    venv = _mk(tmp_path, "pipx/venvs/m3-memory")
    (venv / "pipx_metadata.json").write_text("{}", encoding="utf-8")
    pkg = _mk(venv, "Lib/site-packages/m3_memory")

    method, evidence = m3u.detect_install_method(pkg)
    assert method == m3u.PIPX
    assert "pipx_metadata.json" in evidence


def test_pipx_detected_on_posix_layout(tmp_path):
    """POSIX venvs nest site-packages deeper; the marker must still be found."""
    venv = _mk(tmp_path, ".local/pipx/venvs/m3-memory")
    (venv / "pipx_metadata.json").write_text("{}", encoding="utf-8")
    pkg = _mk(venv, "lib/python3.12/site-packages/m3_memory")

    assert m3u.detect_install_method(pkg)[0] == m3u.PIPX


def test_plain_pip_site_packages(tmp_path):
    """No pipx marker, not user-site, not a plugin cache -> a plain pip install."""
    pkg = _mk(tmp_path, "usr/lib/python3.12/site-packages/m3_memory")
    method, evidence = m3u.detect_install_method(pkg)
    assert method == m3u.PIP
    assert "site-packages" in evidence


@pytest.mark.parametrize(
    "rel",
    [
        ".claude/plugins/cache/skynetcmd/m3/m3_memory",
        ".antigravity/plugins/marketplace/m3/m3_memory",
        "somewhere/plugins/cache/m3_memory",
    ],
)
def test_plugin_cache_is_refused(tmp_path, rel):
    """Host-managed plugin installs must NOT be upgraded by pip/pipx."""
    pkg = _mk(tmp_path, rel)
    method, _ = m3u.detect_install_method(pkg)
    assert method == m3u.PLUGIN
    assert m3u.upgrade_command(method) is None, "a plugin install must have no upgrade command"


def test_unknown_when_package_not_found():
    method, _ = m3u.detect_install_method(None)
    assert method == m3u.UNKNOWN
    assert m3u.upgrade_command(method) is None


@pytest.mark.parametrize(
    "method,expect",
    [
        (m3u.PIPX, ["upgrade", "m3-memory"]),
        (m3u.PIP, ["-m", "pip", "install", "--upgrade", "m3-memory"]),
        (m3u.PIP_USER, ["-m", "pip", "install", "--upgrade", "--user", "m3-memory"]),
    ],
)
def test_upgrade_command_shape(method, expect):
    cmd = m3u.upgrade_command(method, python="/fake/python")
    assert cmd is not None
    for token in expect:
        assert token in cmd, f"{token!r} missing from {cmd!r}"


def test_pip_commands_use_the_owning_interpreter():
    """`pip install -U` must run under the interpreter that owns the package,
    never blindly under whichever one is executing this script."""
    for method in (m3u.PIP, m3u.PIP_USER):
        cmd = m3u.upgrade_command(method, python="/owner/bin/python")
        assert cmd[0] == "/owner/bin/python"
        assert cmd[0] != sys.executable or sys.executable == "/owner/bin/python"


def test_detection_does_not_import_m3_memory():
    """The script must never import the package it is about to replace.

    On Windows that is a file-locking failure, not a theoretical one.
    """
    src = (_BIN / "m3_upgrade.py").read_text(encoding="utf-8")
    assert "import m3_memory" not in src.replace(
        '"import m3_memory,pathlib;"', ""
    ), "m3_upgrade.py must not import m3_memory at module scope"


def test_sibling_interpreter_probe_is_isolated():
    """The fallback probe must pass -E so an ambient PYTHONPATH cannot make a dev
    checkout masquerade as the installed package (observed on a real machine)."""
    src = (_BIN / "m3_upgrade.py").read_text(encoding="utf-8")
    assert '"-E",' in src, "the interpreter probe must run isolated (-E)"


def test_parses_at_the_declared_floor():
    """Supported Pythons start at 3.11 (pyproject requires-python)."""
    import ast

    src = (_BIN / "m3_upgrade.py").read_text(encoding="utf-8")
    for ver in ((3, 11), (3, 12), (3, 13)):
        ast.parse(src, feature_version=ver)


# --- findings from antigravity-agent's macOS review of PR #143 --------------


def test_user_site_detection_survives_a_symlinked_home(tmp_path, monkeypatch):
    """A --user install must still be detected when the user-site path reaches
    the package through a SYMLINK.

    macOS routinely does this (/Users/... vs /System/Volumes/Data/Users/...).
    `pkg_dir` arrives resolved; if `site.getusersitepackages()` is compared
    unresolved, `startswith` fails and a --user install is misdetected as a
    plain pip install -- which would then run the wrong upgrade command.
    """
    real = tmp_path / "real" / "site-packages"
    real.mkdir(parents=True)
    pkg = real / "m3_memory"
    pkg.mkdir()

    link = tmp_path / "linked-site-packages"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted in this environment")

    import site as _site

    monkeypatch.setattr(_site, "getusersitepackages", lambda: str(link))
    method, _ = m3u.detect_install_method(pkg)
    assert method == m3u.PIP_USER, "symlinked user-site must still detect as --user"


def test_setup_step_forces_quiesce():
    """Step 1's `m3 stop` is best-effort, so step 3 must not block forever on a
    writer that did not exit."""
    src = (_BIN / "m3_upgrade.py").read_text(encoding="utf-8")
    assert '"--force-quiesce"' in src, (
        "`m3 setup --non-interactive` needs --force-quiesce or an unattended "
        "upgrade can hang waiting on a stuck DB writer"
    )


def test_the_verify_step_repairs_and_rewires_hooks():
    """An upgrade must REPAIR, not just report.

    The final step was a bare `m3 doctor`, which left every self-repairable
    thing broken behind a warning the user had no reason to act on: the
    embed-server exec bit (a packaging defect in m3's OWN file), a dead agent
    MCP config, a wedged dashboard. An upgrade is the one moment repair is
    unambiguously wanted — the user asked for a new version and is waiting on
    this command.

    `--fix-hooks` is the load-bearing half: hook entries point at the payload
    step 2 just REPLACED, and NOTHING else in the upgrade rewires them, so
    omitting it is how an upgrade leaves chatlog capture pointing at the old
    install. It is normally gated because it writes ~/.claude/settings.json (the
    user's own file) — it backs that up with a timestamp first.
    """
    src = (_BIN / "m3_upgrade.py").read_text(encoding="utf-8")
    assert '"doctor", "--fix", "--fix-hooks"' in src, (
        "the verify step no longer runs `m3 doctor --fix --fix-hooks`; an upgrade "
        "would report repairable state instead of repairing it, and would leave "
        "hooks pointing at the replaced payload"
    )


def test_daemons_are_stopped_AFTER_the_package_is_replaced():
    """Whatever survives step 1 is old-code by definition.

    Step 1's `m3 stop` is best-effort AND --skip-stop-able, and the setup step
    only restarts services it finds STOPPED — so a daemon that survived step 1
    is reported "running" and keeps serving the OLD code against the NEW
    package, indefinitely. The cognitive loop is the one that matters: it is the
    engine that writes derived knowledge, so stale code there quietly produces
    stale derivations.

    Asserting the ORDER, not just the presence: a second stop before the upgrade
    would be useless.
    """
    src = (_BIN / "m3_upgrade.py").read_text(encoding="utf-8")
    assert src.count('run([m3, "stop"]') >= 2, (
        "only one `m3 stop` remains; a daemon that survived it now runs OLD code "
        "and the setup step will not restart it because it looks healthy"
    )
    upgrade_at = src.index("[2/5] upgrading the package")
    second_stop = src.index("survived on OLD code")
    setup_at = src.index("[4/5] finalizing")
    assert upgrade_at < second_stop < setup_at, (
        "the post-upgrade stop must come AFTER the package is replaced and "
        "BEFORE setup restarts the daemons, or it restarts them on old code"
    )


def test_the_step_count_in_the_plan_matches_the_steps_run():
    """The plan printed before the prompt is a promise. It said 4 steps while 5
    ran once this was extended — and a user who approves a 4-step plan should
    not be surprised by a fifth."""
    src = (_BIN / "m3_upgrade.py").read_text(encoding="utf-8")
    import re
    labels = set(re.findall(r"\[(\d)/(\d)\]", src))
    totals = {t for _, t in labels}
    assert totals == {"5"}, f"inconsistent step totals in progress labels: {totals}"
    assert len({n for n, _ in labels}) == 5, (
        f"expected 5 distinct step numbers, found {sorted(n for n, _ in labels)}"
    )
    # `print(f"  2. {' '.join(up)}")` is an f-string, so the prefix is optional —
    # omitting `f?` made this find only 1,3,4,5 and fail on the code being right.
    plan_lines = re.findall(r'print\(f?"  (\d)\. ', src)
    assert plan_lines == ["1", "2", "3", "4", "5"], (
        f"the printed plan does not list 5 steps in order: {plan_lines}"
    )


def test_package_dir_is_resolved_before_comparison():
    """Both sides of every path comparison must be resolved."""
    src = (_BIN / "m3_upgrade.py").read_text(encoding="utf-8")
    assert "pkg_dir = pkg_dir.resolve()" in src
    assert "pathlib.Path(user_site).resolve()" in src


@pytest.mark.parametrize("spec, pypi", [
    ("m3-memory", True),
    ("/home/alice/src/m3-memory", False),
    ("m3-memory==2026.10.1.1", False),
    ("git+https://github.com/skynetcmd/m3-memory", False),
])
def test_pipx_source_is_read_and_classified(tmp_path, spec, pypi):
    """`pipx upgrade` rebuilds from the recorded spec, which may not be PyPI."""
    import json

    venv = _mk(tmp_path, "pipx/venvs/m3-memory")
    (venv / "pipx_metadata.json").write_text(
        json.dumps({"main_package": {"package_or_url": spec}}), encoding="utf-8")
    pkg = _mk(venv, "Lib/site-packages/m3_memory")
    assert m3u.pipx_source(pkg) == spec
    assert m3u.source_is_pypi(spec) is pypi


@pytest.mark.parametrize("spec, missing", [
    ("m3-memory", False),
    ("git+https://github.com/skynetcmd/m3-memory", False),
    ("/tmp/definitely-gone/m3_memory-1-py3-none-any.whl", True),
    ("m3_memory-2026.10.4.2rc3-py3-none-any.whl", True),
])
def test_missing_local_source_is_detected(spec, missing):
    assert m3u.source_is_missing_path(spec) is missing


def test_upgrade_stops_before_touching_services_when_the_source_is_gone(tmp_path, monkeypatch, capsys):
    """pipx upgrade cannot run from a deleted wheel; stopping m3 first would
    leave it down."""
    import json

    venv = _mk(tmp_path, "pipx/venvs/m3-memory")
    (venv / "pipx_metadata.json").write_text(json.dumps(
        {"main_package": {"package_or_url": str(tmp_path / "gone.whl")}}), encoding="utf-8")
    pkg = _mk(venv, "Lib/site-packages/m3_memory")
    monkeypatch.setattr(m3u.shutil, "which", lambda n: "/bin/" + n)
    monkeypatch.setattr(m3u, "find_m3_package", lambda exe: pkg)
    monkeypatch.setattr(m3u, "run", lambda *a, **k: pytest.fail("nothing may run"))
    assert m3u.main(["--yes"]) == 2
    assert "no longer exists" in capsys.readouterr().out


def _pip_install(tmp_path, version="2026.10.4.2"):
    """A pip-layout venv: Scripts/python.exe + m3 launchers + dist-info."""
    venv = _mk(tmp_path, "venv")
    scripts = _mk(venv, "Scripts")
    for name in ("python.exe", "m3.exe", "mcp-memory.exe"):
        (scripts / name).write_bytes(b"MZ")
    site = _mk(venv, "Lib/site-packages")
    pkg = _mk(site, "m3_memory")
    dist = _mk(site, f"m3_memory-{version}.dist-info")
    (dist / "entry_points.txt").write_text(
        "[console_scripts]\nm3 = m3_memory.cli:main\nmcp-memory = m3_memory.cli:main\n",
        encoding="utf-8")
    return scripts, pkg


def test_entry_points_and_version_come_from_the_dist_info(tmp_path):
    _scripts, pkg = _pip_install(tmp_path)
    assert m3u._entry_point_names(pkg) == ["m3", "mcp-memory"]
    assert m3u.installed_version(pkg) == "2026.10.4.2"


def test_a_held_launcher_stops_the_upgrade_before_anything_runs(tmp_path, monkeypatch, capsys):
    """pip removes the package and then fails on a held .exe, leaving m3
    uninstalled; nothing may be stopped or replaced while one is held."""
    scripts, pkg = _pip_install(tmp_path)
    monkeypatch.setattr(m3u.shutil, "which", lambda n: str(scripts / "m3.exe"))
    monkeypatch.setattr(m3u, "find_m3_package", lambda exe: pkg)
    monkeypatch.setattr(m3u, "locked_launchers", lambda d, n: [scripts / "m3.exe"])
    monkeypatch.setattr(m3u, "describe_holders",
                        lambda locked: ["    pid 1  m3.exe  (this command's own launcher)"])
    monkeypatch.setattr(m3u, "run", lambda *a, **k: pytest.fail("nothing may run"))
    monkeypatch.setattr(m3u, "cognitive_loop_installed", lambda exe: True)
    assert m3u.main(["--yes"]) == 2
    out = capsys.readouterr().out
    assert "Nothing was changed" in out
    assert "m3_upgrade.py" in out and "python.exe" in out    # the remedy for a self-hold


def test_other_holders_get_the_close_them_remedy(tmp_path, monkeypatch, capsys):
    scripts, _pkg = _pip_install(tmp_path)
    monkeypatch.setattr(m3u, "describe_holders",
                        lambda locked: ["    pid 7  mcp-memory.exe  (started by claude.exe)"])
    m3u.explain_locked([scripts / "mcp-memory.exe"], str(scripts / "python.exe"))
    out = capsys.readouterr().out
    assert "close the agent sessions" in out
    assert "run the upgrade through Python" not in out


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file-locking semantics")
def test_an_open_launcher_is_reported_locked_and_left_intact(tmp_path):
    scripts, _pkg = _pip_install(tmp_path)
    with open(scripts / "m3.exe", "rb"):
        assert m3u.locked_launchers(scripts, ["m3", "mcp-memory"]) == [scripts / "m3.exe"]
    assert m3u.locked_launchers(scripts, ["m3", "mcp-memory"]) == []
    assert sorted(p.name for p in scripts.iterdir()) == ["m3.exe", "mcp-memory.exe", "python.exe"]


@pytest.mark.parametrize("importable, expect", [
    (True, "m3 2026.10.4.2 is still installed"),
    (False, "m3 is NOT installed now"),
])
def test_a_failed_upgrade_reports_what_is_actually_installed(tmp_path, monkeypatch, capsys,
                                                             importable, expect):
    scripts, pkg = _pip_install(tmp_path)
    monkeypatch.setattr(m3u.shutil, "which", lambda n: str(scripts / "m3.exe"))
    monkeypatch.setattr(m3u, "find_m3_package", lambda exe: pkg)
    monkeypatch.setattr(m3u, "locked_launchers", lambda d, n: [])
    monkeypatch.setattr(m3u, "cognitive_loop_installed", lambda exe: True)
    calls = []

    def _run(cmd, **k):
        calls.append(cmd)
        return 1 if "install" in cmd else 0

    monkeypatch.setattr(m3u, "run", _run)
    monkeypatch.setattr(m3u.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0 if importable else 1})())
    assert m3u.main(["--yes"]) == 1
    out = capsys.readouterr().out
    assert expect in out
    assert "still on its previous version" not in out
    if not importable:
        assert "--force-reinstall --no-deps m3-memory==2026.10.4.2" in out


def test_from_pypi_reinstalls_pipx_from_the_index():
    """--from-pypi moves a pipx install's recorded source to PyPI; pip installs
    already resolve against the index and keep their upgrade command."""
    cmd = m3u.upgrade_command(m3u.PIPX, python="/fake/python", from_pypi=True)
    assert cmd[1:] == ["install", "--force", "m3-memory"]
    assert m3u.upgrade_command(m3u.PIP, python="/fake/python", from_pypi=True) == \
        m3u.upgrade_command(m3u.PIP, python="/fake/python")


def test_from_pypi_proceeds_when_the_recorded_source_is_gone(tmp_path, monkeypatch, capsys):
    """A deleted wheel blocks `pipx upgrade`, but --from-pypi does not need it."""
    import json

    venv = _mk(tmp_path, "pipx/venvs/m3-memory")
    (venv / "pipx_metadata.json").write_text(json.dumps(
        {"main_package": {"package_or_url": str(tmp_path / "gone.whl")}}), encoding="utf-8")
    pkg = _mk(venv, "Lib/site-packages/m3_memory")
    monkeypatch.setattr(m3u.shutil, "which", lambda n: "/bin/" + n)
    monkeypatch.setattr(m3u, "find_m3_package", lambda exe: pkg)
    monkeypatch.setattr(m3u, "locked_launchers", lambda d, n: [])
    monkeypatch.setattr(m3u, "cognitive_loop_installed", lambda exe: True)
    calls = []
    monkeypatch.setattr(m3u, "run", lambda cmd, **k: calls.append(cmd) or 0)
    assert m3u.main(["--yes", "--from-pypi"]) == 0
    assert ["/bin/pipx", "install", "--force", "m3-memory"] in calls
    assert "no longer exists" not in capsys.readouterr().out


def test_an_upgrade_that_changes_nothing_says_so(tmp_path, monkeypatch, capsys):
    """A package manager exits 0 when its source holds the installed version; the
    run must not end in a bare "Done." with the old version still installed."""
    scripts, pkg = _pip_install(tmp_path, version="2026.10.5.0")
    monkeypatch.setattr(m3u.shutil, "which", lambda n: str(scripts / "m3.exe"))
    monkeypatch.setattr(m3u, "find_m3_package", lambda exe: pkg)
    monkeypatch.setattr(m3u, "locked_launchers", lambda d, n: [])
    monkeypatch.setattr(m3u, "cognitive_loop_installed", lambda exe: True)
    monkeypatch.setattr(m3u, "run", lambda cmd, **k: 0)
    assert m3u.main(["--yes"]) == 0
    out = capsys.readouterr().out
    assert "m3 is still 2026.10.5.0" in out
    assert "nothing was upgraded" in out


def test_an_upgrade_that_changes_the_version_reports_both(tmp_path, monkeypatch, capsys):
    scripts, pkg = _pip_install(tmp_path, version="2026.10.4.3")

    def _run(cmd, **k):
        if "install" in cmd:  # the package manager replaces the dist-info
            old = pkg.parent / "m3_memory-2026.10.4.3.dist-info"
            old.rename(pkg.parent / "m3_memory-2026.10.5.0.dist-info")
        return 0

    monkeypatch.setattr(m3u.shutil, "which", lambda n: str(scripts / "m3.exe"))
    monkeypatch.setattr(m3u, "find_m3_package", lambda exe: pkg)
    monkeypatch.setattr(m3u, "locked_launchers", lambda d, n: [])
    monkeypatch.setattr(m3u, "cognitive_loop_installed", lambda exe: True)
    monkeypatch.setattr(m3u, "run", _run)
    assert m3u.main(["--yes"]) == 0
    out = capsys.readouterr().out
    assert "m3 2026.10.4.3 -> 2026.10.5.0" in out
    assert "nothing was upgraded" not in out


def _held_pip_install(tmp_path, monkeypatch, own, others):
    scripts, pkg = _pip_install(tmp_path)
    monkeypatch.setattr(m3u.shutil, "which", lambda n: str(scripts / "m3.exe"))
    monkeypatch.setattr(m3u, "find_m3_package", lambda exe: pkg)
    monkeypatch.setattr(m3u, "locked_launchers", lambda d, n: [scripts / "m3.exe"])
    monkeypatch.setattr(m3u, "launcher_holders", lambda locked: (own, others))
    monkeypatch.setattr(m3u, "cognitive_loop_installed", lambda exe: True)
    monkeypatch.setattr(m3u, "interactive_console", lambda: True)
    return scripts


def test_own_launcher_only_hands_off_to_a_new_window(tmp_path, monkeypatch, capsys):
    """Windows: `m3 upgrade` runs from m3.exe, the file it must replace. When that
    is the only holder, continue from Python in a new window instead of refusing."""
    _held_pip_install(tmp_path, monkeypatch, own=[4242], others=[])
    handed = []
    monkeypatch.setattr(m3u, "hand_off_to_new_window",
                        lambda py, argv, pids: handed.append((argv, pids)))
    monkeypatch.setattr(m3u, "run", lambda *a, **k: pytest.fail("nothing may run here"))
    assert m3u.main(["--yes"]) == 0
    assert handed == [(["--yes"], [4242])]
    assert "continues in a new window" in capsys.readouterr().out


def test_a_dry_run_held_only_by_its_own_launcher_shows_the_plan(tmp_path, monkeypatch, capsys):
    _held_pip_install(tmp_path, monkeypatch, own=[4242], others=[])
    monkeypatch.setattr(m3u, "hand_off_to_new_window",
                        lambda *a: pytest.fail("a dry run must not hand off"))
    assert m3u.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Dry run complete" in out
    assert "Cannot upgrade now" not in out


def test_another_holder_still_refuses(tmp_path, monkeypatch, capsys):
    """An agent's MCP server holding m3.exe cannot be waited out by a hand-off."""
    _held_pip_install(tmp_path, monkeypatch, own=[4242], others=[5555])
    monkeypatch.setattr(m3u, "hand_off_to_new_window", lambda *a: pytest.fail("no hand-off"))
    monkeypatch.setattr(m3u, "run", lambda *a, **k: pytest.fail("nothing may run"))
    assert m3u.main(["--yes"]) == 2
    assert "Cannot upgrade now" in capsys.readouterr().out


def test_an_unattended_run_is_refused_not_handed_off(tmp_path, monkeypatch, capsys):
    """No person at the console: a script would read the hand-off's exit 0 as
    "upgraded" before the upgrade starts. Refuse with the command to run instead."""
    _held_pip_install(tmp_path, monkeypatch, own=[4242], others=[])
    monkeypatch.setattr(m3u, "interactive_console", lambda: False)
    monkeypatch.setattr(m3u, "hand_off_to_new_window", lambda *a: pytest.fail("no hand-off"))
    monkeypatch.setattr(m3u, "run", lambda *a, **k: pytest.fail("nothing may run"))
    assert m3u.main(["--yes"]) == 2
    out = capsys.readouterr().out
    assert "Cannot upgrade now" in out
    assert "m3_upgrade.py\" --yes" in out


def test_agent_sessions_are_listed_and_stopped_after_the_proceed_prompt(tmp_path, monkeypatch, capsys):
    """An agent's m3 server holds m3.exe. At a console, the plan lists it as step 0
    and the one Proceed answer is the consent to stop it."""
    _held_pip_install(tmp_path, monkeypatch, own=[], others=[5555])
    monkeypatch.setattr(m3u, "describe_holders", lambda locked: ["    pid   5555  m3.exe  (started by claude.exe)"])
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    stopped = []
    def _stop(pids):  # stopping the holders releases the launcher, as on Windows
        stopped.append(list(pids))
        monkeypatch.setattr(m3u, "locked_launchers", lambda d, n: [])
    monkeypatch.setattr(m3u, "stop_holders", _stop)
    monkeypatch.setattr(m3u, "run", lambda cmd, **k: 0)
    assert m3u.main([]) == 0
    out = capsys.readouterr().out
    assert "started by claude.exe" in out
    assert "first: stop m3 in the 1 agent session(s)" in out
    assert stopped == [[5555]]


def test_agent_sessions_are_not_stopped_when_the_prompt_is_declined(tmp_path, monkeypatch):
    _held_pip_install(tmp_path, monkeypatch, own=[], others=[5555])
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    monkeypatch.setattr(m3u, "stop_holders", lambda pids: pytest.fail("declined: nothing stopped"))
    monkeypatch.setattr(m3u, "run", lambda *a, **k: pytest.fail("nothing may run"))
    assert m3u.main([]) == 1


def test_stop_agents_is_the_unattended_consent(tmp_path, monkeypatch):
    _held_pip_install(tmp_path, monkeypatch, own=[], others=[5555])
    monkeypatch.setattr(m3u, "interactive_console", lambda: False)
    stopped = []
    def _stop(pids):  # stopping the holders releases the launcher, as on Windows
        stopped.append(list(pids))
        monkeypatch.setattr(m3u, "locked_launchers", lambda d, n: [])
    monkeypatch.setattr(m3u, "stop_holders", _stop)
    monkeypatch.setattr(m3u, "run", lambda cmd, **k: 0)
    assert m3u.main(["--yes", "--stop-agents"]) == 0
    assert stopped == [[5555]]


def test_the_hand_off_passes_the_agent_pids_it_was_allowed_to_stop(tmp_path, monkeypatch):
    _held_pip_install(tmp_path, monkeypatch, own=[4242], others=[5555])
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    handed = []
    monkeypatch.setattr(m3u, "hand_off_to_new_window",
                        lambda py, argv, pids: handed.append((argv, pids)) or "log")
    monkeypatch.setattr(m3u, "stop_holders", lambda pids: pytest.fail("the new window stops them"))
    assert m3u.main([]) == 0
    assert handed == [(["--stop-pids", "5555"], [4242])]


def test_the_hand_off_window_refuses_an_agent_it_was_not_told_about(tmp_path, monkeypatch, capsys):
    """Consent covers the pids passed on; a server that appeared since is left alone."""
    _held_pip_install(tmp_path, monkeypatch, own=[], others=[7777])
    monkeypatch.setattr(m3u, "wait_for_exit", lambda pids, timeout=60.0: True)
    monkeypatch.setattr(m3u, "stop_holders", lambda pids: None)
    monkeypatch.setattr(m3u, "run", lambda *a, **k: pytest.fail("nothing may run"))
    assert m3u.main(["--yes", "--wait-for-pid", "4242", "--stop-pids", "5555"]) == 2
    assert "Cannot upgrade now" in capsys.readouterr().out


def test_the_printed_command_keeps_the_users_flags(tmp_path, monkeypatch, capsys):
    """An unattended `--yes --stop-agents` refused at its own launcher must print a
    command that still stops the agents, or running it refuses again."""
    _held_pip_install(tmp_path, monkeypatch, own=[4242], others=[5555])
    monkeypatch.setattr(m3u, "interactive_console", lambda: False)
    monkeypatch.setattr(m3u, "run", lambda *a, **k: pytest.fail("nothing may run"))
    assert m3u.main(["--yes", "--stop-agents"]) == 2
    assert 'm3_upgrade.py" --yes --stop-agents' in capsys.readouterr().out
