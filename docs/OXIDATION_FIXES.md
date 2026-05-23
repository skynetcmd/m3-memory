# Project Oxidation: Applied Fixes & macOS Wheel Status

This document records the "surgical" fixes and architectural workarounds implemented to stabilize the Rust core (`m3-core-rs`) and integrate it as the core memory for `m3-memory`. These should be reviewed for permanent, cleaner implementation in future waves.

## 🛠️ Stabilizing the Rust Core (m3-core-rs)

### 1. Llama.cpp Non-Causal Encoding (BERT-style)
- **Problem:** BERT-style models like `bge-m3` were triggering "decode: cannot decode batches" errors or hanging because the implementation used `ctx.decode()` (causal) instead of `ctx.encode()`.
- **Fix:** Updated `crates/m3-embed-llamacpp/src/lib.rs` to pass `true` to `add_sequence` and use `ctx.encode()`.
- **Status:** Verified post-wave-8c.

### 2. Blackwell (sm_120+) Performance Auto-Fix
- **Problem:** New Nvidia Blackwell GPUs (RTX 50-series) experienced ~2s latencies per call due to CUDA graph incompatibilities.
- **Fix:** Added auto-detection via `nvidia-smi` in the `m3-embed-llamacpp` init path. It sets `GGML_CUDA_DISABLE_GRAPHS=1` automatically when sm_120+ is detected.
- **Status:** Integrated in wave 9.1.

### 3. MMR Python Fallback Cap
- **Problem:** When the Rust core is absent, the pure-Python MMR implementation could hang or become extremely slow on very large candidate pools (O(k·n)).
- **Fix:** Added an iteration cap to the Python MMR fallback to prevent runaway execution.
- **Status:** Documented in `CHANGELOG.md`.

## 🔒 Security & Robustness

### 4. Gemini Endpoint Hardening (CodeQL #27, #28)
- **Problem:** Substring checks for `generativelanguage.googleapis.com` were susceptible to SSRF or bypass (e.g., `http://evil.com/?x=generativelanguage...`).
- **Fix:** Replaced substring tests with proper hostname parsing via `urlparse` in `bin/unified_ai.py` and `bin/batch_runner.py`.
- **Status:** Verified with 8 test cases.

### 5. Content-Safety Regex (CodeQL #29)
- **Problem:** The `<script.*?>` filter missed bypasses like `<script\n>`.
- **Fix:** Hardened to `<script\b` in `bin/memory/util.py` and collapsed duplicate definitions.
- **Status:** Verified with CodeQL bypass cases.

### 6. Migration Version Coercion
- **Problem:** Legacy installations sometimes wrote string markers in `schema_versions.version`, causing `migrate_memory.py` to fail.
- **Fix:** Added explicit `int` coercion and non-numeric skipping in the migration runner.
- **Status:** Recovered existing deployments.

## 🏗️ Architectural Workarounds

### 7. Circular Dependency "Shim"
- **Problem:** Modularizing `memory_core.py` created a cycle between it and `bin/memory/search.py`.
- **Fix:** Implemented `_resolve_mc_callbacks()` in `search.py`. It lazily binds `memory_core` symbols into the search globals at first use.
- **Goal:** This is a **technical debt** item. Future work should move graph traversal/linkage out of `memory_core` to allow direct imports.

### 8. Chatlog Curator Routing
- **Problem:** The curator was incorrectly applying deduplication logic to the main database when it should have been targeting the chatlog.
- **Fix:** Corrected the routing in the curation pass logic.
- **Status:** Regression caught and fixed 2026-05-17.

## 🍎 macOS Wheel Status (The "Metal" Path)

### Current State
- **Build Path:** `embedded-metal` is fully wired in `m3-core-rs` and gated to `target_os = "macos"`.
- **User UX:** Users must currently build from source (`pip install git+...`). This requires a full Rust toolchain and Xcode Command Line Tools, which is a high barrier to entry.
- **Binary Support:** Supports both Apple Silicon (Metal) and CPU-only builds.

### Desired Future State (TODO)
- **Automated CI:** Create a GitHub Actions workflow in `m3-core-rs` that uses `maturin-action` to build macOS wheels for both `x86_64` and `arm64`.
- **PyPI Distribution:** Publish these wheels to PyPI so `pip install m3-core-rs` works instantly without compilation.
- **Self-Containedness:** Verify if any system libraries (beyond standard macOS frameworks) need bundling.
