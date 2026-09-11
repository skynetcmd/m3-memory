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
