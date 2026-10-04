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
