# 🛡️ M3-Memory: FIPS 140-3 Compliance & Assurance

> **Status:** FIPS-Ready (Transition-Ready)
> **Compliance Target:** NIST SP 800-140C / FIPS 140-3
> **Cryptographic Provider:** wolfSSL / wolfCrypt (Validated Module #4722 or equivalent)

---

## 🏛️ Control Mapping (NIST SP 800-53)

| Control ID | Name | M3-Memory Implementation |
| :--- | :--- | :--- |
| **SC-13** | Cryptographic Protection | All hashing and encryption is abstracted through `bin/crypto_provider.py`, supporting FIPS-validated wolfCrypt backends. |
| **SC-12** | Cryptographic Key Establishment | Key derivation uses PBKDF2-HMAC-SHA256 (600,000 iterations) with per-device salts, executed within the FIPS boundary. |
| **SC-8** | Transmission Confidentiality | Transmission to local/remote engines is restricted to TLS 1.3 with FIPS-approved ciphersuites via wolfSSL contexts. |
| **IA-7** | Cryptographic Module Authentication | Implements mandatory FIPS 140-3 Key Access Management (`PRIVATE_KEY_UNLOCK` / `LOCK`) for all vault operations. |

---

## 🔐 Cryptographic Boundary

### 1. Hardened Hashing (SHA-256)
All content addressing, audit logs, and sovereign integrity manifests use SHA-256. When `M3_CRYPTO_BACKEND=WOLFSSL` is enabled, M3 routes these calls through the validated `wolfcrypt.sha256` engine.

### 2. Modern Secrets Vault (AES-256-GCM)
M3 has transitioned from legacy Fernet (AES-128-CBC) to **AES-256-GCM**.
*   **Authentication:** GCM ensures both confidentiality and authenticity.
*   **Compliance:** Meets the FIPS 140-3 requirement for authenticated encryption of Sensitive Security Parameters (SSPs).
*   **Auto-Migration:** Old secrets are automatically re-encrypted to the GCM format upon first read.

### 3. FIPS 140-3 Operational Guardrails
M3 implements the strict initialization sequence required for 140-3:
*   **Pre-Operational Self-Tests (POST):** Verified via `wolfCrypt_GetStatus_fips()` at startup.
*   **Entropy Injection:** Explicit registration of `wc_GenerateSeed` via `wc_SetSeed_Cb`.
*   **In-Core Integrity:** Sovereign binaries are stabilized using contiguous memory linker flags to prevent ASLR-induced hash mismatches.

---

## 🛰️ Deployment Guidance

### Enabling FIPS Mode
To activate the hardened stack, set the following in your sovereign environment:

```bash
export M3_CRYPTO_BACKEND=WOLFSSL
export M3_FIPS_MODE=1
```

### Verified Hardening
Run the M3 Doctor to verify the FIPS boundary:
```bash
m3 doctor
```
Look for: `✓ FIPS Hardened TLS 1.3 Context Active`.

---

## 🍎 Hardware OE (Operational Environments)
M3 Sovereign FIPS binaries are currently optimized for:
*   **Windows x64** (Intel/AMD with AES-NI)
*   **Linux ARM64** (Raspberry Pi 5, Apple Silicon Virtualization)
*   **macOS ARM64** (Native Metal/MLX acceleration)
