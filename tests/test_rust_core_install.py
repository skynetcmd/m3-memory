"""Tests for m3_memory.rust_core_install — backend resolution + install flow.

No network, no real pip: subprocess.run is monkeypatched so we assert on the
exact argv the resolver would invoke.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from m3_memory import rust_core_install as rci  # noqa: E402


# ── name mapping (must mirror build_wheel.py) ──────────────────────────────────

@pytest.mark.parametrize("os_tok,backend,expected", [
    ("windows", "cpu", "m3-core-rs-windows-cpu"),
    ("windows", "cuda", "m3-core-rs-windows-cuda"),
    ("windows", "vulkan", "m3-core-rs-windows-vulkan"),
    ("linux", "cpu", "m3-core-rs-linux-cpu"),
    ("linux", "cuda", "m3-core-rs-linux-cuda"),
    ("linux", "vulkan", "m3-core-rs-linux-vulkan"),
    ("macos", "metal", "m3-core-rs-macos-metal"),
])
def test_package_name(os_tok, backend, expected):
    assert rci.package_name(os_tok, backend) == expected


def test_all_valid_combos_have_features():
    for os_tok, backend in rci._VALID:
        choice = rci.BackendChoice(os_tok, backend, "test")
        assert choice.backend in rci._BACKEND_FEATURES
        # cpu has no features; gpu backends have exactly one embedded-* feature
        feats = choice.features
        if backend == "cpu":
            assert feats == []
        else:
            assert feats == [f"embedded-{backend}"]


def test_macos_is_metal_only():
    macos = {(o, b) for (o, b) in rci._VALID if o == "macos"}
    assert macos == {("macos", "metal")}


# ── detection ──────────────────────────────────────────────────────────────────

def test_detect_macos_is_metal(monkeypatch):
    c = rci.detect_backend("macos")
    assert c.backend == "metal"


def test_detect_cuda_when_nvcc(monkeypatch):
    monkeypatch.setattr(rci.shutil, "which", lambda x: "/usr/bin/nvcc" if x == "nvcc" else None)
    monkeypatch.delenv("CUDA_PATH", raising=False)
    monkeypatch.delenv("VULKAN_SDK", raising=False)
    c = rci.detect_backend("linux")
    assert c.backend == "cuda"


def test_detect_cuda_when_cuda_path(monkeypatch):
    monkeypatch.setattr(rci.shutil, "which", lambda x: None)
    monkeypatch.setenv("CUDA_PATH", r"C:\CUDA\v13.2")
    c = rci.detect_backend("windows")
    assert c.backend == "cuda"


def test_detect_vulkan_when_sdk(monkeypatch):
    monkeypatch.setattr(rci.shutil, "which", lambda x: None)
    monkeypatch.delenv("CUDA_PATH", raising=False)
    monkeypatch.setenv("VULKAN_SDK", r"C:\VulkanSDK\1.4.341.0")
    c = rci.detect_backend("windows")
    assert c.backend == "vulkan"


def test_detect_cpu_when_nothing(monkeypatch):
    monkeypatch.setattr(rci.shutil, "which", lambda x: None)
    monkeypatch.delenv("CUDA_PATH", raising=False)
    monkeypatch.delenv("VULKAN_SDK", raising=False)
    c = rci.detect_backend("linux")
    assert c.backend == "cpu"


def test_cuda_preferred_over_vulkan(monkeypatch):
    # both present -> CUDA wins (matches legacy precedence)
    monkeypatch.setattr(rci.shutil, "which",
                        lambda x: "/usr/bin/nvcc" if x == "nvcc" else "/usr/bin/vulkaninfo")
    monkeypatch.setenv("VULKAN_SDK", "/opt/vulkan")
    c = rci.detect_backend("linux")
    assert c.backend == "cuda"


# ── install flow (mocked pip) ──────────────────────────────────────────────────

class _FakeProc:
    def __init__(self, rc): self.returncode = rc


def test_install_prebuilt_argv(monkeypatch):
    captured = {}

    def fake_run(argv, env=None):
        captured["argv"] = argv
        return _FakeProc(0)

    monkeypatch.setattr(rci.subprocess, "run", fake_run)
    choice = rci.BackendChoice("windows", "cuda", "test")
    rc = rci.install_prebuilt(choice, version="3.5.30")
    assert rc == 0
    argv = captured["argv"]
    assert argv[:3] == [sys.executable, "-m", "pip"]
    assert "install" in argv and "--only-binary=:all:" in argv
    assert "m3-core-rs-windows-cuda==3.5.30" in argv


def test_install_from_source_passes_features(monkeypatch):
    captured = {}
    monkeypatch.setattr(rci.subprocess, "run",
                        lambda argv, env=None: captured.update(argv=argv) or _FakeProc(0))
    choice = rci.BackendChoice("linux", "vulkan", "test")
    rci.install_from_source(choice, git_tag="v2026.05.30")
    argv = captured["argv"]
    joined = " ".join(argv)
    assert "git+https://github.com/skynetcmd/m3-core-rs.git@v2026.05.30" in joined
    assert "--config-settings" in argv
    assert "build-args=--features embedded-vulkan" in argv


def test_install_from_source_cpu_no_features(monkeypatch):
    captured = {}
    monkeypatch.setattr(rci.subprocess, "run",
                        lambda argv, env=None: captured.update(argv=argv) or _FakeProc(0))
    rci.install_from_source(rci.BackendChoice("linux", "cpu", "test"))
    assert "--config-settings" not in captured["argv"]


def test_install_rust_core_prebuilt_success_no_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(rci, "install_prebuilt", lambda c, **k: calls.append("prebuilt") or 0)
    monkeypatch.setattr(rci, "install_from_source", lambda c, **k: calls.append("source") or 0)
    monkeypatch.setattr(rci, "detect_backend",
                        lambda os_tok=None: rci.BackendChoice("linux", "cuda", "t"))
    rc = rci.install_rust_core()
    assert rc == 0
    assert calls == ["prebuilt"]  # source NOT attempted


def test_install_rust_core_falls_back_to_source(monkeypatch):
    calls = []
    monkeypatch.setattr(rci, "install_prebuilt", lambda c, **k: calls.append("prebuilt") or 1)
    monkeypatch.setattr(rci, "install_from_source", lambda c, **k: calls.append("source") or 0)
    monkeypatch.setattr(rci, "detect_backend",
                        lambda os_tok=None: rci.BackendChoice("linux", "cuda", "t"))
    rc = rci.install_rust_core()
    assert rc == 0
    assert calls == ["prebuilt", "source"]


def test_install_rust_core_no_fallback_when_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(rci, "install_prebuilt", lambda c, **k: calls.append("prebuilt") or 1)
    monkeypatch.setattr(rci, "install_from_source", lambda c, **k: calls.append("source") or 0)
    monkeypatch.setattr(rci, "detect_backend",
                        lambda os_tok=None: rci.BackendChoice("linux", "cuda", "t"))
    rc = rci.install_rust_core(allow_source_fallback=False)
    assert rc == 1
    assert calls == ["prebuilt"]  # no source build
