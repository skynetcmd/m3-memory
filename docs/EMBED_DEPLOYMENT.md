# Embed Deployment Guide

> Status: 2026-05-16. Architecture landed in waves 9.0-9.6 of the Project
> Oxidation pass. CUDA bench numbers (wave 9.7) land in
> `reports/bench_optimized.md` once the post-wave-9 run completes.

This document is operator-facing: build, install, configure, and troubleshoot
the m3 embedder stack on a single host. For environment-variable details,
cross-reference `docs/ENVIRONMENT_VARIABLES.md` — entries are not duplicated
here. For the input-side recipe (what text actually gets embedded — cascade,
anchor augmentation, variants, model-tag namespacing, pooling/BOS triplet)
see `docs/EMBED_INPUT_RECIPE.md`.

---

## Table of contents

1. [The dual-path architecture](#the-dual-path-architecture)
2. [Build matrix](#build-matrix)
3. [Build steps per backend](#build-steps-per-backend)
4. [CPU HTTP fallback (port 8082)](#cpu-http-fallback-port-8082)
5. [m3-memory integration](#m3-memory-integration)
6. [Environment variables](#environment-variables)
7. [Troubleshooting](#troubleshooting)
8. [Cross-references](#cross-references)

---

## The dual-path architecture

```
   +------------------------------------------------+
   |  m3-memory (Python)  bin/memory_core.py        |
   |     _embed() / _embed_many()                   |
   +------------------------------------------------+
              |
              | 1. PRIMARY: in-process via pyo3
              v
   +------------------------------------------------+
   |  m3_core_rs.EmbeddedEmbedder                   |
   |  (llama.cpp linked in-process; zero IPC)       |
   |  backend label: cuda-inprocess /               |
   |  vulkan-inprocess / metal-inprocess /          |
   |  cpu-inprocess                                 |
   +------------------------------------------------+
              |
              | 2. FALLBACK on construction or call failure
              v
   +------------------------------------------------+
   |  m3-embed-server (CPU HTTP, port 8082)         |
   |  Windows Service; always-on CPU embedder       |
   |  backend label: cpu-http-fallback              |
   +------------------------------------------------+
              |
              | 3. LEGACY when both above unavailable
              v
   +------------------------------------------------+
   |  M3_EMBED_URL (LM Studio, llama-server bench)  |
   |  backend label: http-primary                   |
   +------------------------------------------------+
```

Decision points (implemented in `bin/memory_core.py` around line 2018):

- **In-process** is attempted whenever `M3_EMBED_GGUF` is set AND the
  `m3_core_rs` wheel was built with one of the `embedded[-cuda|-vulkan|-metal]`
  features. The dimension is validated against `EMBED_DIM` at construction
  time; a mismatch demotes the backend to HTTP before any real call runs.
- **CPU HTTP fallback** kicks in when the in-process embedder cannot be
  constructed (GGUF missing, CUDA OOM during init, wheel built without an
  `embedded` feature) or raises mid-call. The fallback target is
  `M3_EMBED_FALLBACK_URL` (default `http://127.0.0.1:8082`), POSTed at the
  singular `/embedding` path.
- **Primary HTTP** (`M3_EMBED_URL`) is the original LM-Studio-class path; it
  serves when both the in-process AND the fallback fail.

Runtime observability is exposed via `get_embed_backend_stats()` and
`reset_embed_backend_stats()` in `bin/memory_core.py`. Each served call
increments a thread-safe counter keyed by backend label; bulk calls
attribute one bump per text along the served path.

---

## Build matrix

| feature             | backend  | toolchain                                                                | tested              |
|---------------------|----------|--------------------------------------------------------------------------|---------------------|
| (default)           | HTTP     | none — pure HTTP client                                                  | Win / Linux         |
| `embedded`          | CPU      | C++ compiler (run `install_oxidation_buildtools.ps1` on Windows)         | Win / Linux         |
| `embedded-cuda`     | NVIDIA   | CUDA Toolkit 12.0+ + nvcc + MSVC (CUDA 13.2 confirmed on RTX 5080)       | Win (RTX 5080)      |
| `embedded-vulkan`   | cross-GPU| Vulkan SDK 1.3+ (sets `VULKAN_SDK` env var)                              | wired, untested     |
| `embedded-metal`    | Apple    | macOS + Xcode CLT (compile-gated to `target_os = "macos"`)               | wired, untested     |

GPU backends are **mutually exclusive** — pick exactly one per build. The
`embedded-cuda`/`-vulkan`/`-metal` features each additively enable
`embedded`, so `--features embedded-cuda` is sufficient on its own.
Conflicting GPU features fail at compile time via `compile_error!` in
`crates/m3-embed-llamacpp/src/lib.rs`.

`embedded-metal` is hard-gated on `target_os = "macos"`. Building it on
Windows/Linux produces a compile error rather than a silent CPU artifact.

The active backend at runtime is reported by
`m3_core_rs.embed_backend_label()` — one of `"cpu"`, `"cuda"`, `"vulkan"`,
`"metal"`, or `"none"` if the wheel was built without `embedded`.

---

## Build steps per backend

All commands assume PowerShell on Windows. The `m3-core-rs` repo lives at
`C:\Users\<USER>\m3-core-rs`; the Python bindings crate is `m3-core-py`.

### CPU build (universal)

1. Run the bootstrap script in an **elevated** PowerShell (installs cmake,
   MSVC Build Tools + VCTools workload, and LLVM/libclang — sets
   `LIBCLANG_PATH` machine-wide). CUDA is intentionally NOT installed by
   this script.

   ```powershell
   C:\Users\<USER>\m3-memory\install_oxidation_buildtools.ps1
   ```

2. Open a fresh PowerShell so the updated PATH and `LIBCLANG_PATH` are
   visible, then build and install the wheel:

   ```powershell
   cd C:\Users\<USER>\m3-core-rs\crates\m3-core-py
   python -m maturin build --release --features embedded
   python -m pip install --force-reinstall --no-deps `
       C:\Users\<USER>\m3-core-rs\target\wheels\m3_core_rs-0.0.0-cp314-cp314-win_amd64.whl
   ```

3. Verify:

   ```powershell
   python -c "import m3_core_rs; print(m3_core_rs.embed_backend_label())"
   # -> cpu
   ```

### CUDA build (Nvidia / Blackwell+)

Prerequisites:

- All CPU build prereqs above.
- CUDA Toolkit 12.0+ on PATH (13.2 confirmed working on RTX 5080 /
  Blackwell, sm_120).
- `nvcc --version` succeeds in a fresh PowerShell.
- `$env:CUDA_PATH` points at the toolkit root.

```powershell
# Refresh shell env to pick up the CUDA install:
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
            [Environment]::GetEnvironmentVariable("Path","User")
$env:CUDA_PATH = [Environment]::GetEnvironmentVariable("CUDA_PATH","Machine")

# Populate MSVC env (skip if you are already in a Developer PowerShell):
$vsPath = & "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe" `
    -latest -products * -property installationPath
cmd /c "`"$vsPath\VC\Auxiliary\Build\vcvarsall.bat`" x64 && exit"

cd C:\Users\<USER>\m3-core-rs\crates\m3-core-py
python -m maturin build --release --features embedded-cuda
# 5-20 min compile (llama.cpp + ggml-cuda from source)
python -m pip install --force-reinstall --no-deps `
    C:\Users\<USER>\m3-core-rs\target\wheels\m3_core_rs-0.0.0-cp314-cp314-win_amd64.whl

# Verify:
python -c "import m3_core_rs; print(m3_core_rs.embed_backend_label())"
# -> cuda
```

#### CUDA wheel portability

The CUDA wheel produced by `maturin build --release --features embedded-cuda`
is **not** self-contained. The compiled `m3_core_rs*.pyd` dynamically links
against:

- `cublas64_13.dll`
- `cublasLt64_13.dll`
- `cudart64_13.dll`

These DLLs are loaded at import time from `$env:CUDA_PATH\bin\x64`. The
Python shim in `python/m3_core_rs/__init__.py` auto-registers that path via
`os.add_dll_directory()` before the extension is imported, so as long as the
target machine has a matching CUDA Toolkit installed and `CUDA_PATH` is set
(or the toolkit lives at the standard
`C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v*` layout), the wheel
imports cleanly — no extra operator setup.

What this means in practice:

- **The CUDA wheel is NOT redistributable.** Copying it to a machine without
  the CUDA Toolkit installed will fail at import with
  `ImportError: DLL load failed while importing m3_core_rs: The specified
  module could not be found.`
- The CUDA Toolkit version on the target machine should match the major
  version the wheel was built against (CUDA 13.x for this repo's wheels —
  `cublas64_13.dll` is the giveaway). A CUDA 12 host will not satisfy a
  CUDA 13 wheel.
- The **CPU wheel** (`--features embedded` with no `-cuda`) IS self-contained
  and portable — no external runtime DLLs needed.
- For a truly redistributable CUDA wheel, the CUDA runtime DLLs would need
  to be bundled into the wheel. On Linux this is `auditwheel repair`; on
  Windows the equivalent is [`delvewheel`](https://github.com/adang1345/delvewheel),
  which we have NOT exercised in this repo. Filed as future work — the
  current operator story assumes CUDA Toolkit is installed locally.

**Blackwell auto-fix (wave 9.1)**: the `m3-embed-llamacpp` init path detects
CUDA compute capability via `nvidia-smi` and sets
`GGML_CUDA_DISABLE_GRAPHS=1` automatically when sm_120+ is detected, before
`LlamaBackend::init()` runs. Operators on Blackwell do not need to set this
manually. Explicit user-set values win — if you export the variable
yourself (to any value), the auto-fix does not overwrite it.

### Vulkan build (cross-GPU, Linux / Windows)

Wired and compiles, but not exercised in this repo.

1. Install the LunarG Vulkan SDK from <https://www.lunarg.com/vulkan-sdk/> (or
   on Debian/Ubuntu: `apt install vulkan-sdk libvulkan-dev`).
2. Confirm `$env:VULKAN_SDK` (Windows) or `$VULKAN_SDK` (Linux) is set after
   install — restart the shell so the installer-set var is visible. Without
   it, `llama-cpp-sys-2`'s `build.rs` panics with
   `"Please install Vulkan SDK and ensure that VULKAN_SDK env variable is
   set: NotPresent"`.
3. Build the wheel:

   ```powershell
   cd C:\Users\<USER>\m3-core-rs\crates\m3-core-py
   python -m maturin build --release --features embedded-vulkan
   ```

   `embed_backend_label()` should return `vulkan`.

### Metal build (macOS only)

1. Install Xcode Command Line Tools: `xcode-select --install`.
2. Install `cmake` via Homebrew: `brew install cmake`.
3. Build the wheel:

   ```bash
   cd ~/m3-core-rs/crates/m3-core-py
   # Build for specific Python versions (e.g., 3.12)
   python -m maturin build --release --features embedded-metal --interpreter python3.12
   ```

   `embed_backend_label()` should return `metal`.

> **Pre-compiled macOS Wheels**: If you are using the `skynetcmd/m3-core-rs` repository, check the GitHub Actions "Build macOS Wheels" workflow for pre-compiled artifacts for Python 3.11, 3.12, and 3.14.

| Variant | Build Feature | Target Python |
| :--- | :--- | :--- |
| **Metal GPU** | `embedded-metal` | 3.11, 3.12, 3.14 |
| **CPU Sovereign** | `embedded` | 3.11, 3.12, 3.14 |

---
## Sovereign HTTP fallback (port 8082)

`m3-embed-server` is an HTTP embedder that wraps the same
`m3-embed-llamacpp` in-process backend behind an OpenAI-compatible HTTP API.
It is the deterministic fallback for the in-process path.

### Build

```bash
cd ~/m3-core-rs

# For CPU (Universal)
cargo build -p m3-embed-server --release --features embedded

# For Metal GPU (macOS Apple Silicon)
cargo build -p m3-embed-server --release --features embedded-metal
```

# Binary at: C:\Users\<USER>\m3-core-rs\target\release\m3-embed-server.exe
```

### Install as a Windows Service (elevated PowerShell)

```powershell
# Snapshot M3_EMBED_* into the service config — LocalSystem can't read your
# user env, so the installer captures the current shell's vars into TOML.
$env:M3_EMBED_GGUF        = "C:\Users\<USER>\.lmstudio\models\deepsweet\bge-m3-GGUF-Q4_K_M\bge-m3-GGUF-Q4_K_M.gguf"
$env:M3_EMBED_SERVER_PORT = "8082"   # optional; this is the default

C:\Users\<USER>\m3-core-rs\target\release\m3-embed-server.exe install
```

Edit the resulting config if needed, then start the service:

```powershell
notepad C:\ProgramData\m3-embed-server\config.toml
C:\Users\<USER>\m3-core-rs\target\release\m3-embed-server.exe start
```

Verify the service is up:

```powershell
curl http://127.0.0.1:8082/health    # -> "OK"
curl http://127.0.0.1:8082/metrics   # -> JSON dispatcher stats
Get-Service m3-embed-server          # status: Running
```

Stop / status / uninstall:

```powershell
C:\Users\<USER>\m3-core-rs\target\release\m3-embed-server.exe stop
C:\Users\<USER>\m3-core-rs\target\release\m3-embed-server.exe status
C:\Users\<USER>\m3-core-rs\target\release\m3-embed-server.exe uninstall
# `status` works without admin; `start`/`stop`/`install`/`uninstall` need elevation.
```

`uninstall` does NOT delete `%PROGRAMDATA%\m3-embed-server\config.toml` —
remove it manually if you want a fully clean uninstall.

### Foreground / dev mode

```powershell
$env:M3_EMBED_GGUF = "C:\Users\<USER>\.lmstudio\models\deepsweet\bge-m3-GGUF-Q4_K_M\bge-m3-GGUF-Q4_K_M.gguf"
C:\Users\<USER>\m3-core-rs\target\release\m3-embed-server.exe
# Ctrl-C to stop. Logs go to stderr.
```

### Config file (`%PROGRAMDATA%\m3-embed-server\config.toml`)

```toml
[embed]
gguf             = "C:/Users/<USER>/.lmstudio/models/deepsweet/bge-m3-GGUF-Q4_K_M/bge-m3-GGUF-Q4_K_M.gguf"
port             = 8082
host             = "127.0.0.1"
streams          = 2
ctx              = 8192
seq_max          = 32
n_batch          = 2048
n_ubatch         = 512
coalesce_ms      = 3
max_batch_tokens = 2048
```

All keys are optional except `gguf`. **Precedence**: process env var > TOML
value > built-in default. Env > TOML makes ad-hoc overrides easy in
foreground mode; in service mode LocalSystem cannot see your user env, so
the TOML written at `install` time is the source of truth (plus any
machine-level env vars you have set explicitly).

### Recovery actions

Configured automatically by `install`: the installer shells out to
`sc.exe failure` to set restart-on-failure (3 restarts, 5 s delay, 60 s
reset window). If the `sc.exe` call fails (typically because `install` was
run unelevated and only succeeded partially), the installer prints a WARN
with the exact one-liner to run by hand:

```powershell
sc.exe failure m3-embed-server reset= 60 actions= restart/5000/restart/5000/restart/5000
```

Equivalent GUI path: `services.msc` -> M3 Embed Server -> Recovery tab.

### Logs

Service-mode logs land in `%PROGRAMDATA%\m3-embed-server\service.log.YYYY-MM-DD`,
rotated daily (UTC) via `tracing-appender`. The active day's file is the
newest one in the directory. Foreground / dev mode keeps the original
`env_logger` stderr behaviour — only the Windows Service path uses the
rolling-file appender.

Old rolled files are pruned on service startup: anything older than 14 days
is deleted. No external rotator needed.

Tail the most recent day's log:

```powershell
Get-ChildItem $env:PROGRAMDATA\m3-embed-server\service.log.* |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1 |
    Get-Content -Wait
```

### Non-Windows

Foreground mode (CPU build) runs on Linux unchanged. There is no systemd
unit generator in this crate yet — wire it up manually if you need a
sovereign install on Linux.

---

## Shared GPU embedder (one CUDA context for all m3 processes)

**The problem.** The in-process Rust embedder (`m3_core_rs.EmbeddedEmbedder`,
tier-1) runs the GGUF model *inside* the calling process. CUDA contexts do not
cross process boundaries, so **every** m3 process that embeds in-process opens
its **own** CUDA context — each a multi-GB host-RAM reservation. On a box where
the MCP memory server *and* the cognitive loop both embed, you pay for the model
twice (~4 GB + up to ~12 GB observed). "In-process, shared between processes" is
a contradiction — the only way to load the GPU model **once** and serve many
processes is one owner process + thin clients over IPC.

**The fix.** `bin/embed_server_inproc.py` loads the embedder ONCE and serves it
over localhost HTTP; the other processes disable their own tier-1 and defer to
it. **One CUDA context total (~9-10 GB reclaimed) — the win is host RAM, not
latency.** Measured (RTX 5080): a single small embed is ~33 ms P50 via the server
vs ~28 ms in-process — the localhost round-trip adds a few ms (~10-15% on a
single small request). That fixed per-call cost amortises across a batch (one
round-trip for N vectors), so bulk paths (the cognitive loop, file ingestion)
see negligible overhead. You are trading a few ms on interactive single embeds
for ~9-10 GB of RAM back.

### Enable it

```bash
# 1. Route all m3 processes to the shared server (writes .embed_config.json):
m3 embedder shared                 # or: --port 8091 for a non-default port

# 2. Make the shared server run (it is the SOLE GPU embedder):
#    - it is installed as the AgentOS_EmbedServer scheduled task by
#      `python bin/install_schedules.py --repair` (Windows; elevated shell), OR
#    - start it directly:
python bin/embed_server_inproc.py --port 8082

# 3. Restart the MCP server + cognitive loop so they re-read the config.
```

`m3 embedder shared` writes `<config_root>/.embed_config.json`:

```json
{ "disable_inproc_embedder": true, "fallback_url": "http://127.0.0.1:8082" }
```

This is a **config file, not an env var** — because a headless daemon (the
scheduled-task loop, the MCP server) does not inherit your shell environment
(DESIGN_PHILOSOPHIES §3). `bin/memory/embed.py` reads it at import: precedence is
env var > config file > default, so a one-off `M3_EMBED_GGUF` still overrides.

### Revert

```bash
m3 embedder unshared               # removes .embed_config.json
# then restart the MCP server + loop → each loads its own in-process embedder
```

### When to use it

- **Use shared** when multiple m3 processes embed on the same GPU and host RAM is
  tight — the common desktop/homelab case (one GPU, MCP server + loop + maybe a
  local chat model competing for VRAM).
- **Keep per-process (unshared)** when only one process embeds, or when you want
  zero inter-process dependency (each process is self-contained but heavier).

The shared server binds `127.0.0.1` only (it is not a LAN service), serializes
GPU calls with a semaphore, caps batch size (413 on overflow, never silent
truncation), and exits non-zero if the embedder can't load — see the module
docstring in `bin/embed_server_inproc.py`.

---

## m3-memory integration

`bin/memory_core.py`'s `_embed()` (single) and `_embed_many()` (batch)
implement the dual-path chain:

1. **In-process via `m3_core_rs.EmbeddedEmbedder`.** Triggered when
   `M3_EMBED_GGUF` is set AND the wheel was built with `embedded[-cuda|...]`.
   Backend labels recorded: `cuda-inprocess` / `vulkan-inprocess` /
   `metal-inprocess` / `cpu-inprocess`.

2. **CPU HTTP fallback.** Triggered when the in-process embedder fails to
   construct OR raises during a call. POSTs to `{M3_EMBED_FALLBACK_URL}/embedding`
   (default `http://127.0.0.1:8082/embedding`, singular path). Vectors are
   tagged with `M3_EMBED_GGUF_MODEL_TAG`, sharing the cache namespace with
   in-process vectors. Backend label: `cpu-http-fallback`.

3. **Legacy primary HTTP via `M3_EMBED_URL`.** The original LM-Studio /
   llama-server route. Backend label: `http-primary`.

Vectors produced by paths (1) and (2) share a cache namespace
(`M3_EMBED_GGUF_MODEL_TAG`, default `bge-m3-GGUF-Q4_K_M.gguf`) that is
distinct from LM Studio's `text-embedding-bge-m3` namespace. This is a
deliberate cache-segregation choice so the in-process path can be enabled
without invalidating prior HTTP-tagged rows.

### Observability

```python
from memory_core import get_embed_backend_stats, reset_embed_backend_stats

reset_embed_backend_stats()
# ... do work that produces embeddings ...
print(get_embed_backend_stats())
# e.g. {'cuda-inprocess': 1234, 'cpu-http-fallback': 7}
```

Use this to diagnose distribution between paths:

- Lots of `cpu-http-fallback` where you expected `cuda-inprocess` -> the
  wheel may be the CPU variant, or the GGUF path is wrong, or the in-process
  embedder is raising mid-call (look in stderr).
- Lots of `http-primary` -> both in-process AND fallback failed; check
  service status and `m3_core_rs.embed_backend_label()`.
- All `http-primary` from the start -> `M3_EMBED_GGUF` is probably unset.

Both helpers are thread-safe. `_embed` bumps by 1; `_embed_many` attributes
one bump per text along the path that served it.

---

## Environment variables

The canonical list lives in
[`docs/ENVIRONMENT_VARIABLES.md`](ENVIRONMENT_VARIABLES.md). Relevant rows
for embed deployment, summarized — see that document for full semantics and
defaults:

| Variable                    | Role                                                               |
|-----------------------------|--------------------------------------------------------------------|
| `M3_EMBED_GGUF`             | Path to bge-m3 GGUF. Setting this enables the in-process path.     |
| `M3_EMBED_GGUF_MODEL_TAG`   | `embed_model` cache-namespace tag for in-process + fallback rows.  |
| `M3_EMBED_URL`              | Legacy primary HTTP endpoint (LM Studio, llama-server).            |
| `M3_EMBED_FALLBACK_URL`     | CPU HTTP fallback endpoint (default `http://127.0.0.1:8082`).      |
| `M3_EMBED_STREAMS`          | In-process / fallback dispatcher concurrency (context count).      |
| `M3_EMBED_CTX`              | Per-context `n_ctx`; KV cache scales with this.                    |
| `M3_EMBED_SEQ_MAX`          | Max sequences per batch (parallelism within a context).            |
| `M3_EMBED_N_BATCH`          | llama.cpp batch tokens per pass.                                   |
| `M3_EMBED_N_UBATCH`         | llama.cpp micro-batch tokens.                                      |
| `M3_EMBED_COALESCE_MS`      | Dispatcher coalescing window in ms.                                |
| `M3_EMBED_MAX_BATCH_TOKENS` | Hard cap on tokens per dispatcher batch.                           |
| `M3_EMBED_SERVER_PORT`      | Port `m3-embed-server` binds (default 8082).                       |
| `M3_EMBED_SERVER_HOST`      | Bind host (default `127.0.0.1`).                                   |
| `GGML_CUDA_DISABLE_GRAPHS`  | Blackwell auto-set by wave 9.1; user value wins if explicit.       |

---

## Troubleshooting

### Blackwell embed slow (~2 s per call)

The wave 9.1 auto-fix detects sm_120+ via `nvidia-smi` and sets
`GGML_CUDA_DISABLE_GRAPHS=1` before `LlamaBackend::init()`. If detection
fails silently (no `nvidia-smi`, unexpected output format), force it:

```powershell
$env:GGML_CUDA_DISABLE_GRAPHS = "1"
```

Confirm the auto-fix fired by checking stderr at startup for a log line of
the form `detected CUDA compute cap X.Y - setting GGML_CUDA_DISABLE_GRAPHS=1`.

### CUDA OOM during bulk ingest

Reduce `M3_EMBED_STREAMS` (default 8 in the in-process path; 2 in the
shipped server TOML). Each context costs roughly `n_ctx * 96 KB` of KV
cache on the GPU for bge-m3 fp16. At `M3_EMBED_CTX=8192` x 8 streams that is
~6 GB on a 16 GB RTX 5080; drop to 4 streams for headroom, or shrink `ctx`.

### "decode: cannot decode batches" or "embeddings required but some input tokens were not marked"

Pre-wave-8c bug. Cause: `LlamaBatch::add_sequence(..., false)` plus
`ctx.decode()` for a CLS-pooled non-causal model (bge-m3 is BERT-style).
Two-line fix in `crates/m3-embed-llamacpp/src/lib.rs` (around lines 756 +
765): pass `true` to `add_sequence` and call `ctx.encode()` instead of
`ctx.decode()`. See `C:\Users\<USER>\m3-core-rs\reports\hang_diagnosis.md`
and `fix_verification.md` for the full diagnosis. If you are hitting this,
update to a post-wave-8c `m3-core-rs` build and rebuild the wheel.

### Port 8082 already in use

`m3-embed-server` bind fails with "Only one usage of each socket address".
Find the offender:

```powershell
Get-NetTCPConnection -LocalPort 8082 | Select-Object OwningProcess
Get-Process -Id <pid>
```

Either kill the offender, or change `M3_EMBED_SERVER_PORT` in
`%PROGRAMDATA%\m3-embed-server\config.toml` and restart the service. Also
update `M3_EMBED_FALLBACK_URL` in your m3-memory shell env to match.

### Fallback chain not behaving as expected

Run a short Python session and inspect the counters:

```python
from memory_core import get_embed_backend_stats, reset_embed_backend_stats
reset_embed_backend_stats()
# ... a few writes / searches ...
get_embed_backend_stats()
```

Cross-check:

- `M3_EMBED_GGUF` is set and the file exists?
- `python -c "import m3_core_rs; print(m3_core_rs.embed_backend_label())"`
  returns the backend you expect?
- `curl http://127.0.0.1:8082/health` returns `OK`?
- `Get-Service m3-embed-server` shows `Running`?

### Wheel labeled "cuda" but actually serving CPU

`m3_core_rs.embed_backend_label()` returns `cuda` if and only if the wheel
was built with `--features embedded-cuda`. If it returns `cpu`, the wheel
is the CPU variant — rebuild with `--features embedded-cuda` and reinstall
with `pip install --force-reinstall --no-deps <wheel>`.

### `VULKAN_SDK` not present at build time

`llama-cpp-sys-2`'s `build.rs` panics with
`"Please install Vulkan SDK and ensure that VULKAN_SDK env variable is set:
NotPresent"`. Install the LunarG SDK, restart the shell so the installer's
env-var changes take effect, and re-run the build. Verify with
`$env:VULKAN_SDK` (Windows) or `echo $VULKAN_SDK` (Linux).

### `embedded-metal` on Linux/Windows

Compile error by design (wave 9.6). The feature is gated to
`target_os = "macos"`; building it elsewhere is an explicit error rather
than a silent CPU artifact. Use `embedded` (CPU) or `embedded-vulkan` /
`embedded-cuda` on non-Apple hosts.

### Bench results

For end-to-end numbers (latency, throughput, GPU utilisation), see
`C:\Users\<USER>\m3-core-rs\reports\bench_optimized.md` (post-wave-9
combined report). Pre-oxidation baselines live in
`reports/bench_baseline.md`. Wave 9.7 will land the CUDA-build numbers in
that combined report — do not infer numbers from this document.

---

## Cross-references

- Input-side recipe (what text gets embedded): `C:\Users\<USER>\m3-memory\docs\EMBED_INPUT_RECIPE.md`
- m3-embed-llamacpp crate docs: `C:\Users\<USER>\m3-core-rs\crates\m3-embed-llamacpp\README.md`
- m3-embed-server crate docs: `C:\Users\<USER>\m3-core-rs\crates\m3-embed-server\README.md`
- CPU build-tools bootstrap: `C:\Users\<USER>\m3-memory\install_oxidation_buildtools.ps1`
- Env-var canonical list: `C:\Users\<USER>\m3-memory\docs\ENVIRONMENT_VARIABLES.md`
- Bench numbers: `C:\Users\<USER>\m3-core-rs\reports\bench_baseline.md` (wave 0),
  `C:\Users\<USER>\m3-core-rs\reports\bench_optimized.md` (post-wave-9)
- Hang / fix history: `C:\Users\<USER>\m3-core-rs\reports\hang_diagnosis.md`,
  `C:\Users\<USER>\m3-core-rs\reports\fix_verification.md`
