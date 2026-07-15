# Sovereign & Air-Gapped Deployment Guide

This guide explains how to deploy M3-Memory as a completely self-contained
"memory appliance" in secure, offline, or air-gapped environments.

m3 is sovereign **by default**. The baseline install needs **zero external
services** — no LM Studio, no Ollama, no HuggingFace, no model server. Our
own BGE-M3 CPU embedder ships with the repo and runs on port 8082.

---

## The architecture

A sovereign deployment is:

1. **M3-Memory core** — the MCP server, CLI, and tools (`pip install m3-memory`).
2. **Bundled BGE-M3 GGUF** — ships with the repo via Git LFS at
   `_assets/models/bge-m3-Q4_K_M.gguf` (~438 MB).
3. **m3-embed-server** — Rust binary from the `oxidation` extra; serves an
   OpenAI-compatible `/embedding` endpoint on `127.0.0.1:8082`. Runs as a
   systemd / launchd / Windows Service with `concurrency=2`.
4. **OS service registration** — auto-managed by `m3 embedder install`;
   self-heals on next run if the binary is moved.

No internet, no third-party model server, no LM Studio.

---

## Phase 1 — preparation (on a connected machine)

Before moving to the air-gapped target, hydrate the repository with the
LFS-tracked model file and any extra wheels you'll need offline.

1. **Clone with LFS:**

   ```bash
   git lfs install      # one-time per machine
   git clone https://github.com/skynetcmd/m3-memory.git
   cd m3-memory
   git lfs pull         # materialize _assets/models/bge-m3-Q4_K_M.gguf
   ```

   After `git lfs pull`, `_assets/models/bge-m3-Q4_K_M.gguf` is a real ~438MB
   file (not a 130-byte pointer). Verify with `ls -lh _assets/models/`.

2. **Pre-download pip dependencies:**

   ```bash
   mkdir -p _assets/python_wheels
   pip download m3-memory -d _assets/python_wheels
   ```

   Until `m3-core-rs` publishes wheels to PyPI, the optional Rust core is
   not bundled with m3-memory. If you want it on the air-gapped target,
   also build `m3-core-rs` (Rust ≥1.94 + maturin) from a git checkout of
   `github.com/skynetcmd/m3-core-rs@v0.9.0` and stage the resulting wheel
   under `_assets/python_wheels/`. The base m3-memory install works
   without it.

3. **Bundle for transfer:**

   Copy the entire `m3-memory` folder (including hidden `_assets/`) to your
   offline media — USB, optical, or sneakernet.

---

## Phase 2 — deployment (on the air-gapped target)

1. **Copy the folder** to its permanent home on the secure machine.

2. **Install m3-memory from local wheels:**

   ```bash
   pip install --no-index --find-links=_assets/python_wheels m3-memory
   ```

   If you also staged a locally-built `m3-core-rs` wheel, the same
   `pip install --no-index --find-links=…` command picks it up — the
   Rust core is auto-detected at runtime when importable.

3. **Run the sovereign installer:**

   `m3 setup` orchestrates the full install. In an air-gapped context, you
   want non-interactive mode with no GPU embedder build (no toolchain
   present) and a known capture mode:

   ```bash
   m3 setup --non-interactive --capture-mode both
   ```

   What happens:
   - `m3 install-m3` fetches the system payload from the local cache (or git
     mirror) — see "Air-gapped install-m3" below if you need to skip GitHub.
   - `m3 embedder install` locates the bundled GGUF at
     `_assets/models/bge-m3-Q4_K_M.gguf`, registers `m3-embed-server` as an
     OS service with `concurrency=2`, and starts it on port 8082.
   - Per-agent MCP wiring runs for any of Claude Code / Gemini CLI /
     OpenCode detected on PATH.
   - Chatlog hooks are installed.
   - `m3 doctor` verifies everything.

> **Tool catalog stays small in your context.** m3 ships 100+ MCP tools but
> groups them into 9 domains (memory, chatlog, files, entity, agent, tasks,
> conversations, diagnostics, admin). Only the ~18 essentials load at MCP startup
> (~3,540 tokens, ~1.8% of a 200K window; the full catalog loads on demand). The
> agent pulls in a domain on demand — just say "load the files tools" and it does.
> Set `M3_TOOLS_LAZY=0` to disable. Especially relevant in air-gapped settings
> where every token of context margin counts.

### Air-gapped install-m3

`m3 install-m3` normally clones the system payload from GitHub. For
fully-offline installs, set `M3_BRIDGE_PATH` to point at a pre-staged
payload directory (the contents of the m3-memory repo on disk):

```bash
export M3_BRIDGE_PATH=/srv/m3-memory/bin/memory_bridge.py
m3 setup --non-interactive --capture-mode both
```

`find_bridge()` resolves `M3_BRIDGE_PATH` first, so this skips the GitHub
fetch entirely.

---

## Phase 3 — portability & maintenance

### Embedder lifecycle

```bash
m3 embedder status     # is the service up?
m3 embedder stop
m3 embedder start
m3 embedder uninstall  # remove the OS service registration
```

### Optional: GPU acceleration

The CPU embedder is the sovereign baseline. If the target machine has a
supported GPU **and** the necessary toolchain (CUDA Toolkit / Vulkan SDK /
macOS Xcode CLT), you can add ~10-50× faster in-process embedding:

```bash
m3 embedder install-gpu
```

This auto-detects CUDA / Vulkan / Metal and rebuilds the `m3-core-rs`
component with the matching `embedded-<gpu>` feature. CPU embedder on :8082
continues to serve as the fallback if the GPU path fails (e.g. GPU OOM).

For fully air-gapped GPU builds you need the GPU toolchain pre-staged too —
that's a heavier dance documented in [EMBED_DEPLOYMENT.md](EMBED_DEPLOYMENT.md).

### Integrity verification

The GGUF, m3-embed-server binary, and all m3 Python code are in the repo
under version control. To verify nothing has been tampered with:

```bash
git status              # any modified files?
git lfs fsck             # GGUF SHA256s match LFS manifest?
m3 doctor                # all subsystems healthy?
```

---

## FIPS 140-3 deployment-ready (hardened)

For environments requiring FIPS-approved cryptography, M3 routes all crypto
through **wolfCrypt** when configured. M3 is *deployment-ready*, **not** itself
a validated module — see [`FIPS_MODULE_BOUNDARY.md`](FIPS_MODULE_BOUNDARY.md) for
the authoritative boundary, the two tiers, and limitations.

> **Order matters:** FIPS mode **fails closed** — if you set the env vars before
> wolfSSL is present, M3 will refuse to start. Install wolfSSL FIRST.

1. **Install wolfSSL** (M3 ships no binary — it builds from official source):

   ```bash
   m3 fips install-wolfssl        # clones + builds + installs to ~/.m3/lib
   ```

   (Or `m3 setup` and choose a FIPS tier — it offers to build wolfSSL for you.)

2. **Choose a tier and enable it:**

   ```bash
   # Tier 1 — hardened wolfCrypt, FREE open-source build (homelab/dev):
   export M3_FIPS_MODE=1

   # Tier 2 — also REQUIRE the CMVP-validated wolfCrypt FIPS module
   #          (commercial wolfSSL FIPS license):
   export M3_FIPS_STRICT=1        # implies M3_FIPS_MODE
   ```

3. **Verify + self-pin:**

   ```bash
   m3 doctor                       # crypto (FIPS) section: backend, tier, lib path
   # doctor prints the loaded library's SHA-256 — pin YOUR trusted build:
   export M3_WOLFSSL_SHA256=<that hash>
   ```

   M3 loads wolfSSL only from trusted absolute paths it controls (`M3_WOLFSSL_LIB`
   > `~/.m3/lib` > system dirs) — never the CWD/`%PATH%` — to resist DLL-hijack.

In FIPS mode, internal communication (e.g. to the embedder on port 8082) is
restricted to **TLS 1.3** with FIPS-approved ciphersuites, and the secrets vault
uses **AES-256-GCM**.

See the [FIPS Module Boundary](FIPS_MODULE_BOUNDARY.md) and
[FIPS Compliance Guide](FIPS_COMPLIANCE.md) for deep technical details and
control mappings.

---

## Hardware notes

- **Apple Silicon (M1-M4):** Sovereign baseline (CPU :8082) works out of the
  box. For Metal acceleration, opt in with `m3 embedder install-gpu` —
  builds with `embedded-metal`.
- **Intel Macs:** CPU baseline runs fine. No GPU acceleration path
  (`embedded-metal` is macOS-on-ARM only).
- **Linux ARM64:** Fully supported for low-power sovereign nodes
  (Raspberry Pi 5, Jetson, etc.). CPU baseline only.
- **Linux / Windows with NVIDIA:** Opt-in CUDA via `m3 embedder install-gpu`
  (needs CUDA Toolkit ≥12.0 + nvcc on PATH).
- **Linux / Windows with non-NVIDIA GPU:** Opt-in Vulkan via
  `m3 embedder install-gpu` (needs Vulkan SDK ≥1.3 + `VULKAN_SDK` env var).
