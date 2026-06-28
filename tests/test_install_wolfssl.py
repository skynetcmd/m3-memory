"""Tests for bin/install_wolfssl.py — the build-from-source wolfSSL helper.

We do NOT run the actual multi-minute build here; we test the pure logic:
install-path resolution (must match crypto_provider's discovery), the SHA-256
helper, and prereq detection shape.
"""
from __future__ import annotations

import importlib.util
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


def _load():
    path = os.path.join(os.path.dirname(__file__), "..", "bin", "install_wolfssl.py")
    spec = importlib.util.spec_from_file_location("_m3_install_wolfssl", os.path.abspath(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_install_dir_matches_crypto_provider(monkeypatch, tmp_path):
    """The helper must install where crypto_provider's loader looks (~/.m3/lib),
    honoring the decoupled roots — otherwise the built lib wouldn't be found."""
    monkeypatch.setenv("M3_MEMORY_ROOT", str(tmp_path))
    iw = _load()
    import crypto_provider as cp
    importlib.reload(cp)
    assert os.path.abspath(iw._m3_lib_dir()) == os.path.abspath(cp._m3_lib_dir())


def test_install_dir_default_is_dot_m3_lib(monkeypatch):
    for v in ("M3_MEMORY_ROOT", "M3_CONFIG_ROOT"):
        monkeypatch.delenv(v, raising=False)
    iw = _load()
    expected = os.path.join(os.path.expanduser("~"), ".m3", "lib")
    assert os.path.abspath(iw._m3_lib_dir()) == os.path.abspath(expected)


def test_config_root_parent_is_used(monkeypatch, tmp_path):
    """M3_CONFIG_ROOT's PARENT/lib is used (config root is <base>/config)."""
    monkeypatch.delenv("M3_MEMORY_ROOT", raising=False)
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path / "config"))
    iw = _load()
    assert os.path.abspath(iw._m3_lib_dir()) == os.path.abspath(str(tmp_path / "lib"))


def test_sha256_matches_hashlib(tmp_path):
    import hashlib
    iw = _load()
    f = tmp_path / "x.bin"
    data = b"wolf-bytes" * 1000
    f.write_bytes(data)
    assert iw._sha256(str(f)) == hashlib.sha256(data).hexdigest()


def test_default_ref_is_stable_tag():
    """wolfSSL release tags are '-stable' suffixed; a bare 'v5.9.2' fails to
    clone (verified). Lock the default to a real tag shape."""
    iw = _load()
    assert iw.DEFAULT_REF.endswith("-stable")


def test_prereq_check_returns_tuple():
    iw = _load()
    build_system, missing = iw._check_prereqs()
    assert build_system in ("autotools", "cmake", "")
    assert isinstance(missing, list)


def test_which_falls_back_to_known_dirs(monkeypatch, tmp_path):
    """_which finds a tool in a known dir even when it's not on PATH (the
    Homebrew-off-PATH case on macOS)."""
    iw = _load()
    # A 'cmake' that PATH-based which can't see, but lives in an extra dir.
    fakebin = tmp_path / "brewbin"
    fakebin.mkdir()
    tool = fakebin / "cmake"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    monkeypatch.setattr(iw.shutil, "which", lambda n: None)  # not on PATH
    monkeypatch.setattr(iw, "_EXTRA_TOOL_DIRS", (str(fakebin),))
    assert iw._which("cmake") == str(tool)


def test_macos_help_tiers(capsys, monkeypatch):
    """The macOS prereq help adapts: brew present -> `brew install cmake`;
    no brew -> offers Homebrew one-liner + standalone CMake.app."""
    iw = _load()
    present = {"clang", "make", "git", "brew"}
    monkeypatch.setattr(iw, "_which", lambda n: ("/x/" + n) if n in present else None)
    iw._print_macos_prereq_help()
    out = capsys.readouterr().out
    assert "brew install cmake" in out

    present = {"clang", "make", "git"}  # no brew
    iw._print_macos_prereq_help()
    out = capsys.readouterr().out
    assert "No Homebrew" in out
    assert "cmake.org/download" in out  # standalone fallback offered
