# Building your own Project Oxidation native wheel

m3-memory ships **thin**: the optional native acceleration (the in-process
embedder, the oxidized governor and ingest paths — collectively **Project
Oxidation**) lives in the companion crate **`m3-core-rs`** and is delivered as a
prebuilt Python wheel named `m3-core-rs-<os>-<backend>`.

> **The lightweight wheels are published on PyPI** — under the platform-suffixed
> names (`m3-core-rs-linux-cpu`, `m3-core-rs-windows-cpu`, `m3-core-rs-linux-vulkan`,
> `m3-core-rs-macos-metal`, …), not the bare `m3-core-rs`. So
> `pip install m3-core-rs` returning "not found" is expected — that is not a
> package name. (If a tool told you "the Rust core isn't on PyPI," it was looking
> for the umbrella name; the per-platform wheels are there.)
>
> **The CUDA wheels are currently too large for PyPI** (they bundle the CUDA
> runtime — `linux-cuda` ~970 MB, `windows-cuda` ~256 MB, vs PyPI's 100 MB
> limit), so they're distributed via the
> [GitHub Release](https://github.com/skynetcmd/m3-core-rs/releases). This is a
> size-limit consequence, **not** a downgrade for GPU users — CUDA is the
> fastest, fully-supported backend and ships complete; it just needs a home that
> allows a ~1 GB file. The release pipeline attempts the PyPI upload anyway
> (limits may change) but doesn't fail if it's rejected. **Every** wheel — CUDA
> included — is always attached to the GitHub Release, so `m3 setup` resolves
> **PyPI first, then GitHub Release** and always finds a match. CUDA-specific
> install paths (incl. PyPI-only mirrors): see [CUDA_INSTALL.md](CUDA_INSTALL.md).

**You almost never need this document.** `m3 setup` and `m3 embedder install-gpu`
install the matching prebuilt wheel automatically (PyPI first, then the GitHub
Release). You only build your own wheel when **no prebuilt wheel matches your
platform + Python version** — e.g. a CPU architecture or a Python release we
don't publish for.

## First: you are not broken without it

> **m3 is fully functional as a pure-Python solution** — your memories, search,
> chatlog, and sync all work. Only Project Oxidation's hot-path optimizations
> are not employed, so speed is not maximized.
>
> With Project Oxidation (the native in-process embedder) a typical embed
> completes in **~10–50 ms**. Without it, the same embed runs on the HTTP
> fallback path **~10–85× longer — roughly ~0.3–2.5 s each** *(illustrative;
> varies by host)*. Slower, but still very usable.

So building a wheel is a **speed optimization**, never a correctness
requirement. If the build is inconvenient, skip it — m3 keeps working.

### What ships in every wheel

Each `m3-core-rs-<os>-<backend>` wheel bundles **two** native artifacts, both
built for that wheel's backend:

1. the **in-process embedder** — the `m3_core_rs` Python extension
   (`EmbeddedEmbedder`), used when a process embeds directly; and
2. the **shared-embedder server binary** — `m3-embed-server[.exe]`, the
   OpenAI-compatible HTTP server on port 8082 that is m3's **shared-embedder
   baseline**: all m3 processes defer to ONE server (one model in host RAM
   instead of one copy per process), kept alive as an OS service.

Both are the oxidized (native) path; the shared server is the default topology,
the in-process embedder the per-process fallback.

### The three states

| State | In-process embedder? | Shared `:8082` server | Speed |
|---|---|---|---|
| GPU wheel installed | ✅ `EmbeddedEmbedder` | ✅ `m3-embed-server` (CUDA/Metal/Vulkan) | maximized |
| **CPU wheel installed (no GPU)** | ✅ `EmbeddedEmbedder` | ✅ `m3-embed-server` (**CPU llama.cpp**) | still the oxidized hot path |
| No wheel (pure-Python) | ❌ | ❌ (Python `embed_server_inproc.py` only if started) | usable, not maximized |

Note the middle row: **a machine with no GPU still gets both native artifacts**
from the CPU wheel — "no GPU" does **not** mean "no Project Oxidation". You only
lose the native path entirely when **no wheel at all** is installed.

## Prerequisites

A from-source build needs a Rust toolchain plus the native build tools:

| OS | Install |
|---|---|
| **macOS** | `xcode-select --install && brew install cmake` |
| **Debian/Ubuntu** | `sudo apt install cmake build-essential` |
| **Fedora/RHEL** | `sudo dnf install cmake gcc-c++` |
| **Arch** | `sudo pacman -S cmake base-devel` |
| **Windows** | Install **Visual Studio Build Tools** (C++ workload) |

Rust toolchain (all platforms):

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"
```

GPU backends additionally need their SDK on `PATH`: the **CUDA toolkit**
(`nvcc`) for CUDA, the **Vulkan SDK** for Vulkan. macOS Metal needs only Xcode.

## Option A — let m3 build it for you (easiest)

Once the prerequisites above are installed, just run the GPU-install path with
the source fallback enabled (this is what `m3 setup` does only if you opt into
source builds):

```bash
m3 embedder install-gpu
```

It detects your `(os, backend)`, tries the prebuilt wheel once more, and — when
none matches — builds `m3-core-rs` from source with the right Cargo features
(`embedded` for CPU, `embedded-cuda` / `embedded-vulkan` / `embedded-metal`
for GPU) and `pip install`s the result.

To force the source build to be skipped (CI / unattended), pass
`--no-source-fallback`; m3 then stays on the pure-Python path instead of
compiling.

## Option B — build the wheel by hand from the m3-core-rs repo

The canonical wheel builder lives in the **m3-core-rs** repository, not here:

```
github.com/skynetcmd/m3-core-rs  →  crates/m3-core-py/build_wheel.py
```

```bash
git clone https://github.com/skynetcmd/m3-core-rs.git
cd m3-core-rs
# Build the wheel for your host's backend (cpu|cuda|vulkan|metal):
python crates/m3-core-py/build_wheel.py --backend cpu
# The wheel lands under crates/m3-core-py/dist/ ; install it:
pip install --force-reinstall --no-deps crates/m3-core-py/dist/m3_core_rs_*.whl
```

`build_wheel.py` is also the script that names and produces the wheels we
publish, so a hand-built wheel is byte-for-byte the same kind m3 would have
installed for you.

**The wheel bundles the shared-embedder binary.** `build_wheel.py` builds
`m3-embed-server` with the same backend feature as the wheel and stages it into
the package so it ships *inside* the wheel (at `m3_core_rs/m3-embed-server[.exe]`,
where `embedder_admin._server_binary()` finds it). This is a baseline artifact,
not optional — the build **fails loud** rather than publishing a binary-less
wheel. (Prior wheels silently omitted it because `maturin build` packages only
the Python extension, not Cargo `[[bin]]` targets; the server crate lives in a
separate workspace member.) So a from-source build needs the same native
toolchain (cmake + C/C++ compiler, plus the GPU SDK for a GPU backend) that the
`embedded*` features already require.

The version m3-memory expects is pinned in
[`m3_memory/rust_core_install.py`](../m3_memory/rust_core_install.py)
(`M3_CORE_RS_VERSION` / `M3_CORE_RS_GIT_TAG`) — check out that tag in the
`m3-core-rs` clone to stay in lockstep with your installed m3-memory.

## Verify it landed

```bash
m3 doctor          # the "embedder (Project Oxidation)" line reports the live tier
```

A healthy native install reports `tier-1 in-process — Project Oxidation active`.
If it still reports the pure-Python fallback, the wheel either didn't install or
was built without the `embedded` feature — rebuild with the feature enabled
(Option A does this automatically).

## See also

- [`docs/EMBEDDER_ARCHITECTURE.md`](EMBEDDER_ARCHITECTURE.md) — the full embed cascade and tiers.
- [`docs/MCP_CLIENT_INSTALL.md`](MCP_CLIENT_INSTALL.md) — configuring the tier-1 GGUF.
- [`docs/DESIGN_PHILOSOPHIES.md`](DESIGN_PHILOSOPHIES.md) — why the cascade is structured this way.
