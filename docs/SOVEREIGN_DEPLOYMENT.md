# 🛡️ Sovereign & Air-Gapped Deployment Guide

This guide explains how to deploy M3-Memory as a completely self-contained "memory appliance" in secure, offline, or air-gapped environments.

---

## 🛠️ The Architecture

A sovereign deployment consists of three parts:
1.  **M3-Memory Core:** The MCP server and tools.
2.  **Sovereign Payload:** OS-native LM Studio binaries and the BGE-M3 model.
3.  **Self-Healing Persistence:** OS-level autostart logic that repairs itself if the folder is moved.

---

## 🛰️ Phase 1: Preparation (Online)

Before moving to the air-gapped machine, you must "hydrate" the repository with the required binary assets.

1.  **Clone the Repository:**
    ```bash
    git clone https://github.com/skynetcmd/m3-memory.git
    cd m3-memory
    ```

2.  **Fetch Sovereign Assets:**
    Run the hydrator script. This will download ~1.5GB of binaries and models into the `_assets/embedder/` folder and generate an integrity manifest.
    ```bash
    python bin/fetch_sovereign_assets.py
    ```

3.  **Bundle for Transfer:**
    Copy the entire `m3-memory` folder to your offline media (USB drive, optical disc, etc.). Ensure the hidden `.env` and `_assets/` folders are included.

---

## 🔒 Phase 2: Deployment (Offline)

On the target air-gapped machine:

1.  **Copy the Folder:**
    Move the `m3-memory` folder from your USB drive to its permanent home on the secure machine.

2.  **Install the Core:**
    If you bundled dependencies in Phase 0, install them using the local wheels:
    ```bash
    pip install --no-index --find-links=_assets/python_wheels m3-memory
    ```
    Otherwise, ensure the target machine has internet access and run:
    ```bash
    pip install m3-memory
    ```
    Finally, initialize the project:
    ```bash
    mcp-memory install-m3 --capture-mode both
    ```

3.  **Run the Sovereign Installer:**
    This command operates entirely offline. It verifies file integrity, migrates the correct assets for the target hardware, and automatically configures your local environment:
    *   **Auto-Configures `.env`**: Sets `EMBED_BASE_URL` and `LLM_ENDPOINTS_CSV` to point to the local instance (port 8081).
    *   **Surgical Migration**: Moves the required binary and model to a hidden `.m3-lmstudio` folder.
    *   **Space Saved**: Reports exactly how many MB of unused setup files were deleted.
    
    ```bash
    mcp-memory install-embedder
    ```

---

## 🔄 Phase 3: Portability & Maintenance

### Relocation Resilience
If you move the `m3-memory` folder later, the system will **self-heal**. The next time any M3 command is run, it will detect the new absolute path and automatically update the Windows Startup shortcut, macOS LaunchAgent, or Linux Systemd unit.

### Managing the Embedder
Use the unified dashboard to manage your local engine:
```bash
mcp-memory embedder status
mcp-memory embedder stop
mcp-memory embedder start
```

### Integrity Verification
To manually verify that your "brain" hasn't been corrupted or tampered with:
```bash
python bin/setup_embedder.py --verify
```

---

## 🛡️ FIPS-Ready Deployment (Hardened)

For environments requiring **FIPS 140-3** compliance, M3-Memory can be configured to use a validated cryptographic module (e.g., wolfSSL/wolfCrypt).

1.  **Enable FIPS Mode:**
    Set the backend and mode in your environment:
    ```bash
    export M3_CRYPTO_BACKEND=WOLFSSL
    export M3_FIPS_MODE=1
    ```

2.  **Hardened TLS:**
    In FIPS mode, M3 restricts all internal communication (e.g., to the embedder on port 8081) to **TLS 1.3 only** with FIPS-approved ciphersuites.

3.  **Key Access Management:**
    The secrets vault automatically transitions to **AES-256-GCM** and enforces mandatory `PRIVATE_KEY_UNLOCK`/`LOCK` sequences.

See the [🛡️ FIPS Compliance Guide](FIPS_COMPLIANCE.md) for deep technical details and control mappings.

---

## 🍎 Hardware Notes

*   **Apple Silicon (M1-M4):** The installer defaults to the high-performance native MLX embedder.
*   **Intel Macs:** LM Studio is not supported for sovereign installs on Intel. We recommend side-loading an Ollama binary and the `qwen3-embedding` GGUF manually.
*   **Linux ARM64:** Fully supported for low-power sovereign nodes (Raspberry Pi 5, etc.).
