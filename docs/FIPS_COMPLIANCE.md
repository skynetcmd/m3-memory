# 🛡️ M3-Memory: FIPS 140-3 Readiness & Assurance

> **Status:** FIPS 140-3 **deployment-ready** — NOT itself a validated module.
> **Cryptographic Provider:** wolfCrypt (open-source build, or the CMVP-validated
> wolfSSL FIPS module under `M3_FIPS_STRICT`) — obtained separately, not bundled.
>
> **Read [`FIPS_MODULE_BOUNDARY.md`](FIPS_MODULE_BOUNDARY.md) first** — it is the
> authoritative, precise statement of the boundary, the three modes, and the
> known limitations. This page maps M3's behavior to NIST control language; an
> assessor evaluates the *deployment*, since M3 (an application) holds no
> validation certificate of its own. The control mappings below describe
> *intended behavior when configured with a validated provider*, not a
> certification claim.

---

## 🏛️ Control Mapping (NIST SP 800-53)

| Control ID | Name | M3-Memory Implementation |
| :--- | :--- | :--- |
| **SC-13** | Cryptographic Protection | All hashing and encryption is abstracted through `bin/crypto_provider.py`, supporting FIPS-validated wolfCrypt backends. |
| **SC-12** | Cryptographic Key Establishment | Key derivation uses PBKDF2-HMAC-SHA256 (600,000 iterations) with per-device salts, executed within the FIPS boundary. |
| **SC-8** | Transmission Confidentiality | Transmission to local/remote engines is restricted to TLS 1.3 with FIPS-approved ciphersuites via wolfSSL contexts. |
| **IA-7** | Cryptographic Module Authentication | Crypto operations are delegated to the validated module (wolfCrypt); module-level role/authentication is enforced by that module per its certificate, not re-implemented by M3. (Note: M3's `lock_key`/`unlock_key` hooks are no-ops on the open-source build, which lacks those symbols — see boundary doc §6.) |

---

## 🔐 Cryptographic Boundary

### 1. Hardened Hashing (SHA-256)
All content addressing, audit logs, and sovereign integrity manifests use SHA-256. When `M3_CRYPTO_BACKEND=WOLFSSL` is enabled, M3 routes these calls through the validated `wolfcrypt.sha256` engine.

### 2. Modern Secrets Vault (AES-256-GCM)
M3 has transitioned from legacy Fernet (AES-128-CBC) to **AES-256-GCM**.
*   **Authentication:** GCM ensures both confidentiality and authenticity.
*   **Approved algorithm:** AES-256-GCM is a FIPS-approved authenticated cipher; using it (via a validated provider) is how a deployment satisfies the authenticated-encryption requirement for Sensitive Security Parameters (SSPs). The *compliance* belongs to the validated module + deployment, not to M3 itself.
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
