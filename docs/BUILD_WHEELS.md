# Building your own Project Oxidation native wheel

m3-memory ships **thin**: the optional native acceleration (the in-process
embedder, the oxidized governor and ingest paths — collectively **Project
Oxidation**) lives in the companion crate **`m3-core-rs`** and is delivered as a
prebuilt Python wheel named `m3-core-rs-<os>-<backend>`.

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

### The three states

| State | In-process embedder? | Backend | Speed |
|---|---|---|---|
| GPU wheel installed | ✅ `EmbeddedEmbedder` | CUDA / Metal / Vulkan | maximized |
| **CPU wheel installed (no GPU)** | ✅ `EmbeddedEmbedder` | **CPU llama.cpp, in-process** | still the oxidized hot path |
| No wheel (pure-Python) | ❌ | HTTP fallback (`:8082` / primary) | usable, not maximized |

Note the middle row: **a machine with no GPU still gets the in-process
embedder** from the CPU wheel — "no GPU" does **not** mean "no Project
Oxidation". You only lose the in-process path entirely when **no wheel at all**
is installed.

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
