# Installing the CUDA-accelerated Rust core

M3's optional native core (`m3-core-rs`) ships as prebuilt wheels, one per
`(os, backend)` pair. The lightweight backends (CPU, Vulkan, Metal) are on
**PyPI**. The **CUDA** wheels are currently **too large for PyPI** — they bundle
the CUDA runtime and run ~256 MB (Windows) to ~970 MB (Linux), well over PyPI's
100 MB per-file limit — so they may not appear on PyPI. **Every** wheel,
including CUDA, is always attached to the **GitHub Release** for the tag, which
is the guaranteed source when PyPI can't serve a build.

> The release pipeline *attempts* to publish CUDA to PyPI too (in case the limit
> changes), so a future version's CUDA wheel may resolve straight from PyPI. But
> the GitHub Release is always complete — treat it as the source of truth for
> CUDA today.

For **CPU, Vulkan, and Metal** you don't need this page — `m3 setup` pulls those
straight from PyPI. Read on only if you have an **NVIDIA GPU and want the CUDA
build**, especially if your environment is restricted to a PyPI index and can't
reach GitHub automatically.

> **This is a PyPI file-size limit, not a deprioritization of GPU users.** The
> CUDA build is a first-class, fully-supported backend — in fact it's the
> *fastest* one (10–50× the CPU embedder). Nothing is stripped from it; it's
> the same wheel as every other backend plus the bundled CUDA runtime, which is
> exactly *why* it's too big for PyPI. We host it on the GitHub Release (no size
> limit) rather than shipping a smaller-but-fragile "fetch the runtime at import
> time" wheel that could break on your machine. GPU users get the most complete,
> most reliable build — it just installs from a different place.

> M3 also works **without** the native core at all — it falls back to a
> pure-Python path. CUDA only accelerates the in-process embedder and a few hot
> paths. If installing CUDA is inconvenient, you lose speed, not function.

---

## Option A — let the installer do it (recommended)

On a host with an NVIDIA GPU, the wizard detects it, downloads the matching
CUDA wheel from the GitHub Release, and installs it:

```bash
m3 embedder install-gpu
```

This is the normal path and needs no manual URL. If the host can reach both
PyPI and `github.com`, it just works. Use the options below only when automatic
resolution can't reach the Release (air-gapped mirrors, PyPI-only proxies,
locked-down CI).

---

## Option B — install the prebuilt CUDA wheel by hand from the Release

If your environment only lets `pip` see a PyPI index (so the installer can't
fetch from GitHub), download the wheel yourself and `pip install` the file.

1. Open the m3-core-rs releases page and find the tag matching your installed
   `m3-memory` version's expected core version:
   **https://github.com/skynetcmd/m3-core-rs/releases**

2. Download the wheel for **your OS + Python version**. Wheel names encode both:

   | Your platform | Wheel name pattern |
   |---|---|
   | Linux, Python 3.12 | `m3_core_rs_linux_cuda-<ver>-cp312-cp312-manylinux_*_x86_64.whl` |
   | Windows, Python 3.12 | `m3_core_rs_windows_cuda-<ver>-cp312-cp312-win_amd64.whl` |

   (Swap `cp312` for `cp311` / `cp313` / `cp314` to match your interpreter.)

3. Install the downloaded file directly:

   ```bash
   pip install ./m3_core_rs_linux_cuda-<ver>-cp312-cp312-manylinux_2_17_x86_64.whl
   ```

   `pip install <local-file>.whl` never touches any index, so it works behind a
   PyPI-only proxy. All CUDA wheels install the **same `m3_core_rs` import
   module** as the PyPI backends — nothing else in your setup changes.

4. Verify:

   ```bash
   python -c "import m3_core_rs as m; print(m.embed_backend_label())"
   ```

### One-liner (if the host *can* reach github.com)

`pip` can install straight from the Release asset URL:

```bash
pip install "https://github.com/skynetcmd/m3-core-rs/releases/download/v<ver>/m3_core_rs_linux_cuda-<ver>-cp312-cp312-manylinux_2_17_x86_64.whl"
```

Replace `<ver>` and the `cpXYZ` tag to match. Copy the exact asset URL from the
Release page to avoid typos in the filename.

---

## Option C — host the CUDA wheels on your own index

For a fleet behind a private mirror, download the CUDA wheels once from the
Release and upload them to your internal **PyPI-compatible index** (Artifactory,
Nexus, devpi, a plain `pip install --find-links` directory). Because the wheels
are named `m3-core-rs-linux-cuda` / `m3-core-rs-windows-cuda`, they resolve
normally once your index serves them:

```bash
pip install m3-core-rs-linux-cuda --index-url https://your-mirror/simple
```

This gives PyPI-only clients the CUDA build without every machine reaching
GitHub — you mirror it once.

---

## Option D — build the wheel yourself

If no prebuilt wheel matches your platform or Python version, build from source.
This needs a Rust toolchain and the CUDA toolkit (`nvcc`) on `PATH`. See
[BUILD_WHEELS.md](BUILD_WHEELS.md) for the full procedure — the short version:

```bash
git clone https://github.com/skynetcmd/m3-core-rs.git
cd m3-core-rs
python crates/m3-core-py/build_wheel.py --backend cuda
pip install dist/*.whl
```

---

## Why CUDA usually isn't on PyPI

PyPI enforces a **100 MB per-file limit** (a higher cap requires a manual
exception request and still tops out well below a ~1 GB wheel). The CUDA wheels
statically bundle the CUDA runtime libraries to stay self-contained, which puts
them at ~256 MB (Windows) to ~970 MB (Linux) — an order of magnitude over the
limit. Rather than ship a fragile "download the runtime separately at import
time" hack, M3 keeps the fully-bundled CUDA wheels on the GitHub Release, where
there's no size limit, and serves the smaller backends from PyPI. This is a
deliberate choice to give GPU users the **most complete and most reliable**
build — a self-contained wheel that just works — not a sign that CUDA is an
afterthought. It's the highest-performance backend M3 ships; it simply lives
where a ~1 GB artifact is allowed to.

The release pipeline still *attempts* the CUDA PyPI upload on every release —
PyPI's limits can change, and if a CUDA wheel is ever accepted it'll resolve
from PyPI automatically. The attempt is non-fatal, so a rejection just leaves
the wheel on the GitHub Release (always the complete set). The `m3 setup` /
`m3 embedder install-gpu` resolver hides this split from you.

See [BUILD_WHEELS.md](BUILD_WHEELS.md) for the full backend/PyPI matrix.
