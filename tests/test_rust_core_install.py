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


def test_install_rust_core_github_release_success_skips_fallbacks(monkeypatch):
    """GitHub Release succeeds — neither pip nor source attempted.

    The Release is tier 1 (changed 2026-07-31). It is the only channel that is
    both COMPLETE (all 7 backends x 4 interpreters every tag; PyPI can never
    carry the size-capped CUDA wheels) and CURRENT (PyPI's publish jobs have
    failed trusted-publishing exchange since 3.7.4, so it serves a 2026-07-04
    build). Trying pip first meant installing that stale core and STOPPING,
    because pip exits 0 — a stale success the cascade cannot recover from.
    """
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
    assert calls == ["github"], "pip must not be consulted when the Release has it"


def test_install_rust_core_falls_back_github_to_pip(monkeypatch):
    """GitHub Release fails -> pip succeeds -> source NOT attempted.

    Locks in the 3-tier order: GitHub Release -> pip -> source.
    """
    calls = []
    monkeypatch.setattr(rci, "install_prebuilt",
                        lambda c, **k: calls.append("prebuilt") or 0)
    monkeypatch.setattr(rci, "install_from_github_release",
                        lambda c, **k: calls.append("github") or 1)
    monkeypatch.setattr(rci, "install_from_source",
                        lambda c, **k: calls.append("source") or 0)
    monkeypatch.setattr(rci, "detect_backend",
                        lambda os_tok=None: rci.BackendChoice("macos", "metal", "t"))
    rc = rci.install_rust_core()
    assert rc == 0
    assert calls == ["github", "prebuilt"]


def test_install_rust_core_falls_back_all_three_tiers(monkeypatch):
    """GitHub Release + pip both fail -> source build attempted."""
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
    assert calls == ["github", "prebuilt", "source"]


def test_install_rust_core_no_source_fallback_when_disabled(monkeypatch):
    """allow_source_fallback=False -> Release + pip attempted, then
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
    assert calls == ["github", "prebuilt"]  # source NOT attempted


def test_cuda_never_wastes_a_pypi_roundtrip(monkeypatch):
    """A CUDA box must reach its wheel without consulting PyPI at all.

    m3-core-rs-{windows,linux}-cuda are 404 on PyPI BY DESIGN (both exceed the
    100 MB per-file limit by an order of magnitude), so the old PyPI-first
    cascade opened every CUDA install with a guaranteed-miss network hop.
    """
    for os_tok in ("windows", "linux"):
        calls = []
        monkeypatch.setattr(rci, "install_prebuilt",
                            lambda c, **k: calls.append("prebuilt") or 0)
        monkeypatch.setattr(rci, "install_from_github_release",
                            lambda c, **k: calls.append("github") or 0)
        monkeypatch.setattr(rci, "install_from_source",
                            lambda c, **k: calls.append("source") or 0)
        monkeypatch.setattr(
            rci, "detect_backend",
            lambda os_tok=None, _o=os_tok: rci.BackendChoice(_o, "cuda", "t"))
        assert rci.install_rust_core() == 0
        assert "prebuilt" not in calls, f"{os_tok}-cuda consulted PyPI"


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


def _gh_release_payload(asset_names: list[str], *, size: int = 1024,
                        sizes: "dict[str, int] | None" = None) -> bytes:
    """Build a minimal GitHub release JSON with the named assets.

    ``size`` must equal the byte length the fake download will actually
    produce: the installer compares the API's declared size against what it
    received and treats a difference as a truncated transfer. Pass ``sizes``
    to override per asset (e.g. to simulate exactly that truncation).
    """
    import json as _json
    sizes = sizes or {}
    return _json.dumps({
        "tag_name": rci.M3_CORE_RS_GIT_TAG,
        "assets": [
            {
                "name": name,
                "size": sizes.get(name, size),
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
    fake_wheel = b"PK\x03\x04" + b"fake wheel" * 100  # ZIP-ish header so it looks plausible
    payload = _gh_release_payload([
        f"m3_core_rs_macos_metal-{ver}-cp310-cp310-macosx_11_0_arm64.whl",  # wrong py
        matching_name,                                                       # match
        f"m3_core_rs_linux_cuda-{ver}-{py_tag}-{py_tag}-linux_x86_64.whl",   # wrong os
    ], size=len(fake_wheel))

    urlopen_calls = []

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
    """--force overrides the skip-if-current guard.

    Instruments the FIRST tier (the GitHub Release) — this test is about the
    skip guard, not the cascade order, so it only needs to observe that an
    install was attempted at all.
    """
    calls = []
    monkeypatch.setattr(rci, "is_rust_core_current", lambda: True)
    monkeypatch.setattr(rci, "install_from_github_release",
                        lambda c, **k: calls.append("github") or 0)
    monkeypatch.setattr(rci, "install_prebuilt", lambda c, **k: 0)
    monkeypatch.setattr(rci, "install_from_source", lambda c, **k: 0)
    monkeypatch.setattr(rci, "detect_backend",
                        lambda os_tok=None: rci.BackendChoice("linux", "cpu", "t"))
    rc = rci.install_rust_core(force=True)
    assert rc == 0
    assert calls == ["github"], "force must reinstall"


def test_explicit_backend_bypasses_skip(monkeypatch):
    """An explicit --backend always proceeds (user may be switching cpu<->cuda),
    even if the current version matches."""
    calls = []
    monkeypatch.setattr(rci, "is_rust_core_current", lambda: True)
    monkeypatch.setattr(rci, "install_from_github_release",
                        lambda c, **k: calls.append("github") or 0)
    monkeypatch.setattr(rci, "install_prebuilt", lambda c, **k: 0)
    monkeypatch.setattr(rci, "install_from_source", lambda c, **k: 0)
    rc = rci.install_rust_core(backend="cpu", os_tok="linux")
    assert rc == 0
    assert calls == ["github"]


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


def test_pip_success_is_not_labelled_a_pypi_install(monkeypatch, capsys):
    """A successful pip install must NOT be reported as coming from PyPI.

    pip exits 0 just as happily from its local wheel cache as from a real PyPI
    download, and install_prebuilt returns only that exit code — the source is
    not observable. The old message asserted "(PyPI prebuilt)" anyway, which was
    provably wrong for CUDA: `m3-core-rs-windows-cuda` is a 404 on PyPI BY
    DESIGN (the wheels are ~10x the per-file size limit and ship via GitHub
    Releases), yet a cache hit still printed "installed ... (PyPI prebuilt)" —
    a false distribution claim in exactly the output someone reads when
    debugging distribution.
    """
    monkeypatch.setattr(rci, "install_prebuilt", lambda choice, **kw: 0)
    # install_from_github_release is tried FIRST and is NOT mocked by default —
    # it makes a real urllib call to api.github.com. Unmocked, this test reached
    # the network: green wherever the Release resolves, red in a sandboxed CI
    # runner, and never actually exercising the pip branch it asserts on. Force
    # the Release path to miss so the pip-success message is what gets printed
    # (§3 — a test that depends on a reachable external service is not hermetic).
    monkeypatch.setattr(rci, "install_from_github_release", lambda choice, **kw: 1)
    monkeypatch.setattr(
        rci, "detect_backend",
        lambda *a, **k: rci.BackendChoice(
            os_tok="windows", backend="cuda",
            reason="NVIDIA CUDA toolchain detected"),
    )
    assert rci.install_rust_core() == 0
    out = capsys.readouterr().out
    assert "PyPI prebuilt" not in out, f"unverifiable PyPI claim returned: {out!r}"
    assert "prebuilt wheel via pip" in out, out


# ---------------------------------------------------------------------------
# SHA256SUMS verification of a downloaded Release wheel
#
# The wheels are fetched by URL from a GitHub Release and handed to
# `pip install --no-deps <path>.whl`. pip rejects a grossly truncated wheel
# (the zip central directory lives at EOF) but does NOT verify member CRCs on a
# direct install -- a bit-flip inside the compiled .pyd installs cleanly and
# fails later as an import crash. Measured 2026-09-16: a 64-byte corruption
# mid-payload left the file the same length, zipfile opened it, testzip() named
# the bad member, and pip still reported "Would install".
# ---------------------------------------------------------------------------

def _sha256_hex(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _release_with_sums(wheel_bytes_by_name, *, sums_text=None,
                       include_sums=True, declared_sizes=None):
    """(api_payload, sha256sums_body) for a release carrying these wheels.

    Sizes default to the true body length: the installer compares the API's
    declared size against what it received, so a fixture that lies about it
    would trip the truncation check instead of the one under test.
    """
    import json as _json
    declared = dict(declared_sizes or {})
    names = list(wheel_bytes_by_name)
    if sums_text is None:
        sums_text = "".join(
            "{} *{}\n".format(_sha256_hex(b), n)
            for n, b in sorted(wheel_bytes_by_name.items())
        )
    assets = [
        {
            "name": n,
            "size": declared.get(n, len(wheel_bytes_by_name[n])),
            "browser_download_url": "https://example.com/{}".format(n),
        }
        for n in names
    ]
    if include_sums:
        assets.append({
            "name": "SHA256SUMS",
            "size": len(sums_text.encode("utf-8")),
            "browser_download_url": "https://example.com/SHA256SUMS",
        })
    payload = _json.dumps({
        "tag_name": rci.M3_CORE_RS_GIT_TAG,
        "assets": assets,
    }).encode("utf-8")
    return payload, sums_text


def _wire_release(monkeypatch, payload, sums_text, wheel_bodies):
    """Serve the API payload, SHA256SUMS, and one body per download attempt."""
    import urllib.request as _ur
    bodies = list(wheel_bodies)
    calls = {"api": 0, "sums": 0, "wheel": 0}

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        if "api.github.com" in url:
            calls["api"] += 1
            return _FakeRespCtx(payload)
        if url.endswith("SHA256SUMS"):
            calls["sums"] += 1
            return _FakeRespCtx(sums_text.encode("utf-8"))
        calls["wheel"] += 1
        return _FakeRespCtx(bodies[min(calls["wheel"] - 1, len(bodies) - 1)])

    monkeypatch.setattr(_ur, "urlopen", fake_urlopen)
    return calls


def _metal_target():
    py_tag = "cp{}{}".format(sys.version_info.major, sys.version_info.minor)
    name = "m3_core_rs_macos_metal-{}-{}-{}-macosx_11_0_arm64.whl".format(
        rci.M3_CORE_RS_VERSION, py_tag, py_tag)
    return rci.BackendChoice("macos", "metal", "test"), name


GOOD_WHEEL = b"PK\x03\x04" + b"good wheel" * 200
BAD_WHEEL = b"PK\x03\x04" + b"BAD! wheel" * 200      # same length, different bytes


def test_wheel_sha256_match_installs_and_reports_success(monkeypatch, capsys):
    """A matching digest installs AND says so on stdout.

    A silent pass is indistinguishable from a check that never ran, so the
    success line is part of the contract rather than decoration.
    """
    choice, name = _metal_target()
    payload, sums = _release_with_sums({name: GOOD_WHEEL})
    calls = _wire_release(monkeypatch, payload, sums, [GOOD_WHEEL])

    pip_argv = []
    monkeypatch.setattr(rci, "_pip_install_with_pep668_fallback",
                        lambda *a: pip_argv.append(a) or 0)

    assert rci.install_from_github_release(choice) == 0
    assert len(pip_argv) == 1, "a verified wheel must reach pip"
    assert calls["wheel"] == 1, "no retry when the first download verifies"

    out = capsys.readouterr().out
    assert "sha256 OK" in out
    assert name in out


def test_wheel_sha256_mismatch_retries_once_then_refuses(monkeypatch, capsys):
    """A persistent mismatch downloads twice and never installs.

    Two attempts: corruption in flight is transient and usually clears on a
    refetch. A repeat failure means the published asset is bad, and further
    retries cannot help.
    """
    choice, name = _metal_target()
    assert len(BAD_WHEEL) == len(GOOD_WHEEL), "must defeat the size check"
    payload, sums = _release_with_sums({name: GOOD_WHEEL})
    calls = _wire_release(monkeypatch, payload, sums, [BAD_WHEEL, BAD_WHEEL])

    pip_argv = []
    monkeypatch.setattr(rci, "_pip_install_with_pep668_fallback",
                        lambda *a: pip_argv.append(a) or 0)

    assert rci.install_from_github_release(choice) == 1
    assert pip_argv == [], "a wheel failing its digest must never reach pip"
    assert calls["wheel"] == 2, "exactly two attempts"

    err = capsys.readouterr().err
    assert "code=wheel_sha256_mismatch" in err
    assert "corruption in flight" in err, "first failure names the likely cause"


def test_wheel_sha256_mismatch_recovers_on_retry(monkeypatch, capsys):
    """First download corrupt, second clean -> installs.

    This is the case the retry exists for: without it a single flipped bit
    would push the user down the source-build path.
    """
    choice, name = _metal_target()
    payload, sums = _release_with_sums({name: GOOD_WHEEL})
    calls = _wire_release(monkeypatch, payload, sums, [BAD_WHEEL, GOOD_WHEEL])

    pip_argv = []
    monkeypatch.setattr(rci, "_pip_install_with_pep668_fallback",
                        lambda *a: pip_argv.append(a) or 0)

    assert rci.install_from_github_release(choice) == 0
    assert calls["wheel"] == 2
    assert len(pip_argv) == 1
    assert calls["sums"] == 1, "the manifest is fetched once, not per attempt"

    cap = capsys.readouterr()
    assert "code=wheel_sha256_mismatch" in cap.err
    assert "sha256 OK" in cap.out


def test_truncated_download_is_caught_by_size_before_pip(monkeypatch, capsys):
    """A short body fails on the declared size, with a message that names it.

    pip would also reject this, but with an opaque "Wheel ... is invalid" that
    sends the operator looking at the wrong thing.
    """
    choice, name = _metal_target()
    short = GOOD_WHEEL[: len(GOOD_WHEEL) // 2]
    payload, sums = _release_with_sums({name: GOOD_WHEEL})
    calls = _wire_release(monkeypatch, payload, sums, [short, short])

    pip_argv = []
    monkeypatch.setattr(rci, "_pip_install_with_pep668_fallback",
                        lambda *a: pip_argv.append(a) or 0)

    assert rci.install_from_github_release(choice) == 1
    assert pip_argv == []
    assert calls["wheel"] == 2
    err = capsys.readouterr().err
    assert "code=wheel_size_mismatch" in err
    assert "expected_bytes={}".format(len(GOOD_WHEEL)) in err


def test_release_without_sha256sums_still_installs(monkeypatch, capsys):
    """Releases before v2026.9.16 publish no manifest -- warn, do not block.

    Hard-failing here would break every rollback to an older tag.
    """
    choice, name = _metal_target()
    payload, sums = _release_with_sums({name: GOOD_WHEEL}, include_sums=False)
    _wire_release(monkeypatch, payload, sums, [GOOD_WHEEL])

    pip_argv = []
    monkeypatch.setattr(rci, "_pip_install_with_pep668_fallback",
                        lambda *a: pip_argv.append(a) or 0)

    assert rci.install_from_github_release(choice) == 0
    assert len(pip_argv) == 1
    assert "no SHA256SUMS asset" in capsys.readouterr().err


def test_sha256sums_without_an_entry_for_this_wheel_installs(monkeypatch, capsys):
    """Manifest present but silent about our wheel -> warn, install.

    Treating a missing line as a mismatch would block a legitimate install on
    an incomplete manifest.
    """
    choice, name = _metal_target()
    other = "0" * 64 + " *some_other_wheel.whl\n"
    payload, sums = _release_with_sums({name: GOOD_WHEEL}, sums_text=other)
    _wire_release(monkeypatch, payload, sums, [GOOD_WHEEL])

    pip_argv = []
    monkeypatch.setattr(rci, "_pip_install_with_pep668_fallback",
                        lambda *a: pip_argv.append(a) or 0)

    assert rci.install_from_github_release(choice) == 0
    assert len(pip_argv) == 1
    assert "has no entry for" in capsys.readouterr().err


def test_sha256sums_parser_accepts_both_coreutils_forms(monkeypatch):
    """`<hex>  name` (text) and `<hex> *name` (binary) are both valid.

    Our generator runs under Git Bash and emits the binary marker; a Linux
    generator would not. Keying on the basename also stops a path prefix from
    hiding an entry.
    """
    body = (
        "aa" * 32 + "  plain_text_form.whl\n"
        + "bb" * 32 + " *binary_form.whl\n"
        + "cc" * 32 + " *dist/with_a_path.whl\n"
        + "not-a-digest  ignored.whl\n"
        + "\n"
    )
    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen",
                        lambda url, timeout=None: _FakeRespCtx(body.encode()))

    got = rci._fetch_release_sha256sums(
        [{"name": "SHA256SUMS",
          "browser_download_url": "https://example.com/SHA256SUMS"}],
        git_tag="vtest",
    )
    assert got == {
        "plain_text_form.whl": "aa" * 32,
        "binary_form.whl": "bb" * 32,
        "with_a_path.whl": "cc" * 32,
    }


def test_sha256sums_non_https_url_is_refused():
    """The manifest URL is external data; pin the scheme as the wheel URL is."""
    assert rci._fetch_release_sha256sums(
        [{"name": "SHA256SUMS",
          "browser_download_url": "http://example.com/SHA256SUMS"}],
        git_tag="vtest",
    ) is None


# ---------------------------------------------------------------------------
# Install/upgrade log
#
# The console output of an upgrade is gone by the time anyone asks "when did
# this host move to 3.9.16, and did its wheel verify?". A digest mismatch in
# particular is an after-the-fact investigation.
# ---------------------------------------------------------------------------

def test_install_log_records_digest_outcome(monkeypatch, tmp_path, capsys):
    """A verified download leaves a durable record, not just a console line."""
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path / "config"))
    choice, name = _metal_target()
    payload, sums = _release_with_sums({name: GOOD_WHEEL})
    _wire_release(monkeypatch, payload, sums, [GOOD_WHEEL])
    monkeypatch.setattr(rci, "_pip_install_with_pep668_fallback", lambda *a: 0)

    assert rci.install_from_github_release(choice) == 0

    log = tmp_path / "logs" / "m3_rust_core_install.log"
    assert log.exists(), "a verified install must be recorded"
    text = log.read_text(encoding="utf-8")
    assert "sha256 OK" in text
    assert _sha256_hex(GOOD_WHEEL) in text, "the full digest is the evidence"


def test_install_log_records_a_mismatch_for_later_analysis(monkeypatch, tmp_path):
    """The failure a user reports days later must still be on disk."""
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path / "config"))
    choice, name = _metal_target()
    payload, sums = _release_with_sums({name: GOOD_WHEEL})
    _wire_release(monkeypatch, payload, sums, [BAD_WHEEL, BAD_WHEEL])
    monkeypatch.setattr(rci, "_pip_install_with_pep668_fallback", lambda *a: 0)

    assert rci.install_from_github_release(choice) == 1

    text = (tmp_path / "logs" / "m3_rust_core_install.log").read_text(encoding="utf-8")
    assert text.count("code=wheel_sha256_mismatch") == 2, "both attempts recorded"
    assert _sha256_hex(GOOD_WHEEL) in text, "expected digest"
    assert _sha256_hex(BAD_WHEEL) in text, "what we actually received"


def test_install_log_records_the_version_transition(monkeypatch, tmp_path):
    """The log answers "when did this host move to X" -- as a transition."""
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path / "config"))
    monkeypatch.setattr(rci, "installed_rust_core_version", lambda: "3.9.7")
    monkeypatch.setattr(rci, "is_rust_core_current", lambda: False)
    monkeypatch.setattr(rci, "detect_backend",
                        lambda os_tok=None: rci.BackendChoice("macos", "metal", "t"))
    monkeypatch.setattr(rci, "install_from_github_release", lambda c, **k: 0)

    assert rci.install_rust_core() == 0

    text = (tmp_path / "logs" / "m3_rust_core_install.log").read_text(encoding="utf-8")
    assert "install start:" in text
    assert "3.9.7 -> {}".format(rci.M3_CORE_RS_VERSION) in text
    assert "channel=github-release" in text


def test_install_log_records_a_total_failure(monkeypatch, tmp_path):
    """All three tiers failing is precisely what gets investigated later."""
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path / "config"))
    monkeypatch.setattr(rci, "installed_rust_core_version", lambda: None)
    monkeypatch.setattr(rci, "is_rust_core_current", lambda: False)
    monkeypatch.setattr(rci, "detect_backend",
                        lambda os_tok=None: rci.BackendChoice("linux", "cuda", "t"))
    monkeypatch.setattr(rci, "install_from_github_release", lambda c, **k: 1)
    monkeypatch.setattr(rci, "install_prebuilt", lambda c, **k: 1)
    monkeypatch.setattr(rci, "install_from_source", lambda c, **k: 1)

    assert rci.install_rust_core() != 0

    text = (tmp_path / "logs" / "m3_rust_core_install.log").read_text(encoding="utf-8")
    assert "install FAILED" in text
    assert "still_at=(none)" in text, "records that nothing was installed"


def test_install_log_failure_never_breaks_the_install(monkeypatch, tmp_path):
    """A read-only or unwritable log directory must not fail a good install.

    The same facts already went to stdout/stderr, so only the durable copy is
    lost -- that is not worth failing an otherwise successful upgrade over.
    """
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path / "config"))

    # _install_log_path imports Path locally, so patch pathlib itself -- the
    # object the helper will actually resolve at call time.
    import pathlib as _pathlib

    def boom(*a, **k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(_pathlib.Path, "mkdir", boom)

    choice, name = _metal_target()
    payload, sums = _release_with_sums({name: GOOD_WHEEL})
    _wire_release(monkeypatch, payload, sums, [GOOD_WHEEL])
    monkeypatch.setattr(rci, "_pip_install_with_pep668_fallback", lambda *a: 0)

    assert rci.install_from_github_release(choice) == 0, \
        "logging is best-effort; it must never fail the install"
