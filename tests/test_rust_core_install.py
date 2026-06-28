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

# Real function captured before the autouse fixture can patch it, so the unit
# tests that exercise is_rust_core_current itself call the genuine impl.
_REAL_IS_CURRENT = rci.is_rust_core_current


@pytest.fixture(autouse=True)
def _not_current(request, monkeypatch):
    """Default the skip-if-current guard OFF so install-flow tests exercise the
    cascade regardless of whether THIS host already has a current wheel. Tests
    marked `@pytest.mark.real_is_current` opt out to test the real function."""
    if "real_is_current" in request.keywords:
        return
    monkeypatch.setattr(rci, "is_rust_core_current", lambda: False)


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
        # Every backend ships an in-process embedder: cpu uses the plain
        # `embedded` feature (CPU llama.cpp); gpu backends use `embedded-<gpu>`.
        feats = choice.features
        if backend == "cpu":
            assert feats == ["embedded"]
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

    def fake_run(argv, env=None, **kwargs):
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
                        lambda argv, env=None, **kwargs: captured.update(argv=argv) or _FakeProc(0))
    choice = rci.BackendChoice("linux", "vulkan", "test")
    rci.install_from_source(choice, git_tag="v2026.05.30")
    argv = captured["argv"]
    joined = " ".join(argv)
    assert "git+https://github.com/skynetcmd/m3-core-rs.git@v2026.05.30" in joined
    assert "--config-settings" in argv
    assert "build-args=--features embedded-vulkan" in argv


def test_install_from_source_cpu_passes_embedded_feature(monkeypatch):
    # CPU now builds --features embedded (in-process BGE-M3), so the source
    # fallback passes it to maturin via pip config-settings.
    captured = {}
    monkeypatch.setattr(rci.subprocess, "run",
                        lambda argv, env=None, **kwargs: captured.update(argv=argv) or _FakeProc(0))
    rci.install_from_source(rci.BackendChoice("linux", "cpu", "test"))
    assert "--config-settings" in captured["argv"]
    idx = captured["argv"].index("--config-settings")
    assert captured["argv"][idx + 1] == "build-args=--features embedded"


def test_install_rust_core_prebuilt_success_skips_fallbacks(monkeypatch):
    """PyPI prebuilt succeeds — neither GitHub Release nor source attempted."""
    calls = []
    monkeypatch.setattr(rci, "install_prebuilt",
                        lambda c, **k: calls.append("prebuilt") or 0)
    monkeypatch.setattr(rci, "install_from_github_release",
                        lambda c, **k: calls.append("github") or 0)
    monkeypatch.setattr(rci, "install_from_source",
                        lambda c, **k: calls.append("source") or 0)
    monkeypatch.setattr(rci, "detect_backend",
                        lambda os_tok=None: rci.BackendChoice("linux", "cuda", "t"))
    rc = rci.install_rust_core()
    assert rc == 0
    assert calls == ["prebuilt"]


def test_install_rust_core_falls_back_pypi_to_github(monkeypatch):
    """PyPI fails -> GitHub Release succeeds -> source NOT attempted.

    Locks in the 3-tier order: PyPI -> GitHub Release -> source.
    """
    calls = []
    monkeypatch.setattr(rci, "install_prebuilt",
                        lambda c, **k: calls.append("prebuilt") or 1)
    monkeypatch.setattr(rci, "install_from_github_release",
                        lambda c, **k: calls.append("github") or 0)
    monkeypatch.setattr(rci, "install_from_source",
                        lambda c, **k: calls.append("source") or 0)
    monkeypatch.setattr(rci, "detect_backend",
                        lambda os_tok=None: rci.BackendChoice("macos", "metal", "t"))
    rc = rci.install_rust_core()
    assert rc == 0
    assert calls == ["prebuilt", "github"]


def test_install_rust_core_falls_back_all_three_tiers(monkeypatch):
    """PyPI + GitHub Release both fail -> source build attempted."""
    calls = []
    monkeypatch.setattr(rci, "install_prebuilt",
                        lambda c, **k: calls.append("prebuilt") or 1)
    monkeypatch.setattr(rci, "install_from_github_release",
                        lambda c, **k: calls.append("github") or 1)
    monkeypatch.setattr(rci, "install_from_source",
                        lambda c, **k: calls.append("source") or 0)
    monkeypatch.setattr(rci, "detect_backend",
                        lambda os_tok=None: rci.BackendChoice("linux", "cuda", "t"))
    rc = rci.install_rust_core()
    assert rc == 0
    assert calls == ["prebuilt", "github", "source"]


def test_install_rust_core_no_source_fallback_when_disabled(monkeypatch):
    """allow_source_fallback=False -> PyPI + GitHub Release attempted, then
    recommendation printed and source build NOT triggered. The curl install.sh
    flow passes False so users aren't surprised by a multi-minute Rust build.
    """
    calls = []
    monkeypatch.setattr(rci, "install_prebuilt",
                        lambda c, **k: calls.append("prebuilt") or 1)
    monkeypatch.setattr(rci, "install_from_github_release",
                        lambda c, **k: calls.append("github") or 1)
    monkeypatch.setattr(rci, "install_from_source",
                        lambda c, **k: calls.append("source") or 0)
    monkeypatch.setattr(rci, "detect_backend",
                        lambda os_tok=None: rci.BackendChoice("macos", "metal", "t"))
    rc = rci.install_rust_core(allow_source_fallback=False)
    assert rc != 0
    assert calls == ["prebuilt", "github"]  # source NOT attempted


# ── GitHub Release fallback ────────────────────────────────────────────────────


class _FakeRespCtx:
    """Mimics urlopen()'s context-manager + .read() interface."""
    def __init__(self, payload: bytes, chunks: int = 0):
        self._payload = payload
        self._chunks = chunks
        self._offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            data = self._payload[self._offset:]
            self._offset = len(self._payload)
            return data
        data = self._payload[self._offset:self._offset + size]
        self._offset += len(data)
        return data


def _gh_release_payload(asset_names: list[str]) -> bytes:
    """Build a minimal GitHub release JSON with the named assets."""
    import json as _json
    return _json.dumps({
        "tag_name": rci.M3_CORE_RS_GIT_TAG,
        "assets": [
            {
                "name": name,
                "size": 1024,
                "browser_download_url": f"https://example.com/{name}",
            }
            for name in asset_names
        ],
    }).encode("utf-8")


def test_github_release_finds_and_installs_matching_asset(monkeypatch):
    """Picks the m3_core_rs_<os>_<backend>-<ver>-<py>-*.whl that matches
    the current Python and pip-installs the downloaded file.
    """
    py_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    ver = rci.M3_CORE_RS_VERSION
    matching_name = f"m3_core_rs_macos_metal-{ver}-{py_tag}-{py_tag}-macosx_11_0_arm64.whl"
    payload = _gh_release_payload([
        f"m3_core_rs_macos_metal-{ver}-cp310-cp310-macosx_11_0_arm64.whl",  # wrong py
        matching_name,                                                       # match
        f"m3_core_rs_linux_cuda-{ver}-{py_tag}-{py_tag}-linux_x86_64.whl",   # wrong os
    ])

    urlopen_calls = []
    fake_wheel = b"PK\x03\x04" + b"fake wheel" * 100  # ZIP-ish header so it looks plausible

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        urlopen_calls.append(url)
        if "api.github.com" in url:
            return _FakeRespCtx(payload)
        return _FakeRespCtx(fake_wheel)

    # install_from_github_release imports urllib lazily — patch the global
    # module so the lazy import in the function picks up our fake.
    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen", fake_urlopen)

    pip_argv = []
    monkeypatch.setattr(rci, "_pip_install_with_pep668_fallback",
                        lambda *a: pip_argv.append(a) or 0)

    choice = rci.BackendChoice("macos", "metal", "test")
    rc = rci.install_from_github_release(choice)
    assert rc == 0

    # We made two HTTPS calls: GH API for the release, then wheel download
    assert any("api.github.com" in u for u in urlopen_calls)
    assert any(matching_name in u for u in urlopen_calls)

    # pip got our temp wheel path
    assert len(pip_argv) == 1
    args = pip_argv[0]
    assert args[0] == "install"
    assert "--force-reinstall" in args
    assert "--no-deps" in args
    assert args[-1].endswith(".whl")


def test_github_release_404_returns_nonzero(monkeypatch):
    """Draft / missing release -> 1 (caller falls through). Don't raise."""
    import urllib.error as _ue
    import urllib.request as _ur

    def fake_urlopen(req, timeout=None):
        raise _ue.HTTPError(
            url="https://api.github.com/...", code=404, msg="Not Found",
            hdrs=None, fp=None,
        )

    monkeypatch.setattr(_ur, "urlopen", fake_urlopen)
    choice = rci.BackendChoice("macos", "metal", "test")
    rc = rci.install_from_github_release(choice)
    assert rc == 1


def test_github_release_no_matching_asset_returns_nonzero(monkeypatch):
    """API succeeds but no asset starts with the expected prefix."""
    import urllib.request as _ur
    py_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    payload = _gh_release_payload([
        f"m3_core_rs_linux_cpu-{rci.M3_CORE_RS_VERSION}-{py_tag}-{py_tag}-manylinux2014.whl",
        # No macos_metal asset
    ])

    monkeypatch.setattr(_ur, "urlopen",
                        lambda req, timeout=None: _FakeRespCtx(payload))

    choice = rci.BackendChoice("macos", "metal", "test")
    rc = rci.install_from_github_release(choice)
    assert rc == 1


def test_github_release_network_error_returns_nonzero(monkeypatch):
    """OSError / URLError on API request -> 1 (don't raise)."""
    import urllib.request as _ur

    def boom(req, timeout=None):
        raise OSError("network unreachable")

    monkeypatch.setattr(_ur, "urlopen", boom)
    choice = rci.BackendChoice("macos", "metal", "test")
    rc = rci.install_from_github_release(choice)
    assert rc == 1


# ── build-tool preflight (cargo via rustup toolchains) ─────────────────────────


def test_check_build_tools_includes_rust(monkeypatch):
    """Source build needs Rust — _check_build_tools must catch missing cargo."""
    # No cmake, no C++, no cargo, no rustup dirs.
    monkeypatch.setattr(rci.shutil, "which", lambda x: None)
    monkeypatch.setattr(rci.os.path, "isfile", lambda p: False)
    monkeypatch.setattr(rci.os.path, "isdir", lambda p: False)
    missing = rci._check_build_tools()
    assert "Rust (cargo)" in missing


def test_find_cargo_via_rustup_toolchain(monkeypatch, tmp_path):
    """cargo not on PATH but rustup toolchain dir holds it -> found."""
    # No cargo on PATH and no ~/.cargo/bin/cargo
    monkeypatch.setattr(rci.shutil, "which", lambda x: None)

    rustup_home = tmp_path / "rustup"
    toolchain_bin = rustup_home / "toolchains" / "stable-aarch64-apple-darwin" / "bin"
    toolchain_bin.mkdir(parents=True)
    cargo_path = toolchain_bin / "cargo"
    cargo_path.write_text("#!/bin/sh\necho cargo\n", encoding="utf-8")
    cargo_path.chmod(0o755)

    monkeypatch.setenv("RUSTUP_HOME", str(rustup_home))
    monkeypatch.setenv("HOME", str(tmp_path / "home-without-cargo"))  # no ~/.cargo

    found = rci._find_cargo()
    assert found is not None
    assert "cargo" in found
    assert "toolchains" in found


# ── skip-if-current guard ──────────────────────────────────────────────────────

def test_skips_install_when_already_current(monkeypatch):
    """When the embedded wheel is already at the target version, install_rust_core
    short-circuits — no PyPI / GitHub / source attempt, no re-download."""
    calls = []
    monkeypatch.setattr(rci, "is_rust_core_current", lambda: True)
    monkeypatch.setattr(rci, "active_embedder_tier",
                        lambda: {"native": True, "version": rci.M3_CORE_RS_VERSION,
                                 "summary": "current"})
    monkeypatch.setattr(rci, "install_prebuilt", lambda c, **k: calls.append("prebuilt") or 0)
    monkeypatch.setattr(rci, "install_from_github_release", lambda c, **k: calls.append("github") or 0)
    monkeypatch.setattr(rci, "install_from_source", lambda c, **k: calls.append("source") or 0)
    monkeypatch.setattr(rci, "detect_backend",
                        lambda os_tok=None: rci.BackendChoice("linux", "cuda", "t"))
    rc = rci.install_rust_core()
    assert rc == 0
    assert calls == [], "current wheel must not be reinstalled"


def test_force_reinstalls_even_when_current(monkeypatch):
    """--force overrides the skip-if-current guard."""
    calls = []
    monkeypatch.setattr(rci, "is_rust_core_current", lambda: True)
    monkeypatch.setattr(rci, "install_prebuilt", lambda c, **k: calls.append("prebuilt") or 0)
    monkeypatch.setattr(rci, "install_from_github_release", lambda c, **k: 0)
    monkeypatch.setattr(rci, "install_from_source", lambda c, **k: 0)
    monkeypatch.setattr(rci, "detect_backend",
                        lambda os_tok=None: rci.BackendChoice("linux", "cpu", "t"))
    rc = rci.install_rust_core(force=True)
    assert rc == 0
    assert calls == ["prebuilt"], "force must reinstall"


def test_explicit_backend_bypasses_skip(monkeypatch):
    """An explicit --backend always proceeds (user may be switching cpu<->cuda),
    even if the current version matches."""
    calls = []
    monkeypatch.setattr(rci, "is_rust_core_current", lambda: True)
    monkeypatch.setattr(rci, "install_prebuilt", lambda c, **k: calls.append("prebuilt") or 0)
    monkeypatch.setattr(rci, "install_from_github_release", lambda c, **k: 0)
    monkeypatch.setattr(rci, "install_from_source", lambda c, **k: 0)
    rc = rci.install_rust_core(backend="cpu", os_tok="linux")
    assert rc == 0
    assert calls == ["prebuilt"]


@pytest.mark.real_is_current
def test_is_rust_core_current_false_without_version(monkeypatch):
    """CONSERVATIVE: a wheel with no __version__ is NOT treated as current — we
    must not skip an upgrade on a guess."""
    class _FakeRS:
        EmbeddedEmbedder = object  # has the feature
        # no __version__
    monkeypatch.setitem(sys.modules, "m3_core_rs", _FakeRS())
    assert rci.is_rust_core_current() is False


@pytest.mark.real_is_current
def test_is_rust_core_current_compares_version(monkeypatch):
    class _Old:
        EmbeddedEmbedder = object
        __version__ = "0.0.1"

    class _New:
        EmbeddedEmbedder = object
        __version__ = "999.0.0"

    monkeypatch.setitem(sys.modules, "m3_core_rs", _Old())
    assert rci.is_rust_core_current() is False  # older than target
    monkeypatch.setitem(sys.modules, "m3_core_rs", _New())
    assert rci.is_rust_core_current() is True   # newer than target


@pytest.mark.real_is_current
def test_is_rust_core_current_false_without_embedded_feature(monkeypatch):
    """A wheel built WITHOUT the embedded feature must reinstall."""
    class _NoFeature:
        __version__ = "999.0.0"
        # no EmbeddedEmbedder
    monkeypatch.setitem(sys.modules, "m3_core_rs", _NoFeature())
    assert rci.is_rust_core_current() is False
