# M3 Memory — FIPS 140-3 Module Boundary & Operating Guide

> **Status: FIPS 140-3 *deployment-ready*, not FIPS-validated.**
> M3 Memory implements no cryptography of its own and is **not itself a
> validated cryptographic module** — no application is. FIPS 140-3 validates a
> *cryptographic module* (e.g. wolfCrypt, OpenSSL FIPS, AWS-LC FIPS), not the
> application that calls it. This document states precisely what M3 does, where
> the cryptographic boundary is, and what each mode requires — so a security
> reviewer or federal customer can evaluate the deployment, not marketing.

---

## 1. What "deployment-ready" means here

M3 is FIPS-deployment-ready because it:

1. **Implements no custom cryptography.** All crypto is delegated to a provider.
2. **Uses only FIPS-approved algorithms:** AES-256-GCM (authenticated
   encryption), SHA-256/384/512, HMAC-SHA-256, PBKDF2-HMAC-SHA256, and TLS 1.3.
   It blocks non-approved algorithms (MD5, SHA-1, DES, RC4, Fernet for new
   writes).
3. **Routes every cryptographic operation through a single boundary**
   (`bin/crypto_provider.py`) so a validated module can serve all of it.
4. **Can use a FIPS-validated provider** (wolfCrypt) and **fails closed** when
   required — it never silently falls back to non-validated crypto in FIPS mode.

It is **not** "FIPS 140-3 compliant / certified / validated" — those terms have
specific NIST/CMVP regulatory meaning and apply to a validated module + its
certificate, which M3 (an application) cannot hold.

---

## 2. The cryptographic boundary

```
            ┌─────────────────────────── M3 application (OUTSIDE the boundary) ──┐
            │  memory store · search · chatlog · sync · audit-trail · vault API  │
            │                              │                                     │
            │                   crypto_provider.py  ◄── the only crypto entry    │
            └──────────────────────────────┼────────────────────────────────────┘
                                           ▼
            ┌──────────────── cryptographic module (INSIDE the boundary) ────────┐
            │  DEFAULT: Python `cryptography` (OpenSSL) + `hashlib`               │
            │  WOLFSSL: wolfCrypt  (open-source build, OR CMVP-validated FIPS)    │
            │  Services: sha256() · encrypt()/decrypt() (AES-256-GCM) ·          │
            │            pbkdf2_sha256() · get_ssl_context() (TLS 1.3)            │
            └────────────────────────────────────────────────────────────────────┘
```

**Inside the boundary:** the crypto module (wolfCrypt or the Python
`cryptography`/OpenSSL stack) and the thin `crypto_provider.py` dispatch that
calls it. **Outside:** all M3 application logic, which only ever reaches crypto
through `crypto_provider`'s services.

**Services** (the crypto operations M3 exposes): `sha256`, `encrypt` /
`decrypt` (AES-256-GCM), `pbkdf2_sha256`, and `get_ssl_context` (TLS 1.3). These
are used by exactly three things: the encrypted secrets vault (`auth_utils.py`),
key derivation, and the tamper-evident audit hash chain (`audit_trail.py`).

**Roles:** M3 has no privileged crypto-operator role of its own — it is a
*crypto user*. The operator configures the mode (below) via environment; the
validated module enforces its own role separation per its CMVP certificate.

---

## 3. The three modes (tiers)

| Env | Backend | wolfSSL required? | CMVP-validated module required? | Use case |
|---|---|---|---|---|
| *(neither set)* | Python `cryptography`/OpenSSL + `hashlib` | no | no | Default — runs everywhere |
| **`M3_FIPS_MODE=1`** | **wolfCrypt** | **yes** (fail-closed if absent) | **no** — accepts open-source build | Homelab / dev: real wolfCrypt + hardened boundary, **no commercial license needed** |
| **`M3_FIPS_MODE=1`** + **`M3_FIPS_STRICT=1`** | **wolfCrypt (validated)** | yes | **yes** — refuses non-FIPS build | True FIPS 140-3: requires the commercial wolfSSL FIPS module |

### Why two tiers?

The **CMVP-validated wolfCrypt FIPS module** is a **commercial, NDA-gated**
product — it is not a free download, and its validation only covers the exact,
unmodified vendor binary. The **open-source wolfSSL** build is freely buildable
(GPLv2 or commercial) but **lacks the FIPS service symbols** (POST, entropy
callbacks) and therefore is not the validated module.

So:

- **`M3_FIPS_MODE`** lets homelab/dev users get wolfCrypt's performance and the
  hardened algorithm boundary on the open-source build, without paying for the
  FIPS license. It still **fails closed** if wolfCrypt is entirely absent, still
  runs the power-up Known-Answer-Tests, and still blocks non-approved algorithms.
- **`M3_FIPS_STRICT`** is the gate for *actual* FIPS 140-3 compliance: it
  requires the validated module (FIPS symbols present + POST passes) and
  refuses the open-source build with a clear, actionable error.

---

## 4. Power-up self-tests (KATs)

When the wolfCrypt backend initializes, M3 runs in-process **Known-Answer-Tests**
against canonical vectors before serving any crypto:

- **SHA-256** — NIST FIPS 180-4 example, `SHA256("abc")`.
- **PBKDF2-HMAC-SHA256** — RFC 7914 §11 vector.
- **AES-256-GCM** — fixed key+nonce known-answer (ciphertext‖tag) plus an
  authenticated decrypt round-trip.

A KAT mismatch is **fatal** under any FIPS tier (the module enters an error
state and refuses to serve). These complement the validated module's own POST
(`wolfCrypt_GetStatus_fips`): POST attests the module; the KATs attest that *this
process's bindings invoke it correctly*.

---

## 5. Getting wolfSSL: download, build, place

> M3 does **not** bundle or redistribute wolfSSL — it is GPLv2 / commercial, and
> M3 is Apache-2.0. You build it **yourself, from the official source**, on your
> own machine. M3 only automates (or documents) the steps; it never ships or
> hosts the binary.

### Option A — the helper (recommended)

M3 ships a build-from-source helper that clones the **official** wolfSSL repo,
builds the shared library with exactly the features M3 uses (AES-GCM, SHA-256,
PBKDF2), installs it to `~/.m3/lib/` (where M3's secure loader finds it — §below),
and prints the SHA-256 to self-pin:

```bash
m3 fips install-wolfssl            # clone + build + install to ~/.m3/lib
#   --ref v5.9.2-stable            # pin a specific wolfSSL release tag
#   --dest <dir>                   # install elsewhere (then set M3_WOLFSSL_LIB)
#   --print-sha                    # also print just the SHA-256
```

It is equivalent to `python bin/install_wolfssl.py` (auditable, self-contained).

**Prerequisites** (a C toolchain + git):

| OS | Install |
|---|---|
| Debian/Ubuntu | `sudo apt install git autoconf automake libtool make gcc` |
| Fedora/RHEL | `sudo dnf install git autoconf automake libtool make gcc` |
| macOS | `xcode-select --install` (clang+make+git) **plus** a build system: the smallest is **cmake** — `brew install cmake`, or a standalone [CMake.app](https://cmake.org/download/) if you don't use Homebrew. (Or the autotools trio: `brew install autoconf automake libtool`.) The helper auto-finds Homebrew / CMake.app even when they're off your shell PATH. |
| Windows | Git, **CMake**, and **Visual Studio Build Tools** (C++ workload) |

The result is the **open-source (non-FIPS) build** — works with `M3_FIPS_MODE=1`.
It is **not** the CMVP-validated FIPS module (`M3_FIPS_STRICT` requires that;
obtain it via wolfSSL's commercial channel — §3).

### Option B — build by hand

If you'd rather run the steps yourself (or audit them):

```bash
git clone --branch v5.9.2-stable --depth 1 https://github.com/wolfSSL/wolfssl.git
cd wolfssl
# Linux/macOS (autotools):
./autogen.sh && ./configure --enable-aesgcm --enable-sha256 --enable-pwdbased \
  --disable-examples --disable-crypttests && make -j
#   -> src/.libs/libwolfssl.so   (or .dylib on macOS)
# Windows (CMake + Visual Studio):
cmake -G "Visual Studio 17 2022" -A x64 -DBUILD_SHARED_LIBS=ON \
  -DWOLFSSL_AESGCM=yes -DWOLFSSL_PWDBASED=yes . && cmake --build . --config Release
#   -> Release\wolfssl.dll
```

> wolfSSL release tags are suffixed `-stable` (e.g. `v5.9.2-stable`).

### Where to place it

Copy the built library to **`~/.m3/lib/`** (M3's loader checks there
automatically), or to any path and point `M3_WOLFSSL_LIB` at it:

```bash
mkdir -p ~/.m3/lib
cp src/.libs/libwolfssl.so ~/.m3/lib/            # Linux
cp src/.libs/libwolfssl.dylib ~/.m3/lib/         # macOS
#  Windows: copy Release\wolfssl.dll  %USERPROFILE%\.m3\lib\
```

Then enable FIPS mode and verify with `m3 doctor` (the **crypto (FIPS)** section
shows the loaded path + SHA-256 to self-pin).

**Where M3 looks (secure, search-order-hardened).** M3 NEVER loads the crypto
library by bare name — that would delegate to the OS loader search order
(Windows: app dir + **CWD** + `%PATH%`; Linux: `LD_LIBRARY_PATH` + runpath),
a DLL-hijack vector where an attacker drops a weaker/backdoored `wolfssl.dll`
earlier in the path. Instead M3 resolves a **trusted absolute path** from this
precedence and loads exactly that file:

1. **`M3_WOLFSSL_LIB`** — an explicit absolute path you pin (strongest).
2. **`~/.m3/lib/`** (M3's own lib dir; honours the decoupled roots).
3. **Trusted system dirs only** — `/usr/local/lib`, `/usr/lib`, `/lib`,
   `/opt/homebrew/lib` (Unix) or `%SystemRoot%\System32` (Windows). The **CWD
   and `%PATH%`/`LD_LIBRARY_PATH` are deliberately excluded.**

**Integrity pin (optional, strongest) — you pin *your own* build.** Because M3
does **not** bundle wolfSSL, you build it yourself (§5). So the pin is a
**self-pin / trust-on-first-use** model, not a check against some vendor hash:
right after you build (or obtain) and *trust* the library, compute ITS SHA-256
and pin that value. M3 then verifies the file against your pin on every load and
**refuses a mismatch** — catching any later tampering or in-place swap, even at
a trusted path. There is no canonical hash to compare to; the trust anchor is
the build you produced.

```bash
# After building/obtaining the library you trust, compute and pin ITS hash:
sha256sum ~/.m3/lib/libwolfssl.so        # Linux/macOS
#  -> use the value from YOUR build
export M3_WOLFSSL_SHA256=<that hash>
# Windows (PowerShell):
#   (Get-FileHash $env:USERPROFILE\.m3\lib\wolfssl.dll -Algorithm SHA256).Hash
```

Re-pin whenever you intentionally rebuild/upgrade wolfSSL (the new build has a
new hash — expected). A mismatch you did *not* expect means the library
changed underneath you: investigate before proceeding.

M3 also verifies the loaded library exposes the core crypto symbols
(`wc_AesGcmSetKey/Encrypt/Decrypt`, `wc_Sha256Hash`, `wc_PBKDF2`).
Run `m3 doctor` — the **crypto (FIPS)** section prints the exact absolute path
the library was loaded from, so you can confirm it's the trusted one.

**Validated FIPS build (for `M3_FIPS_STRICT`):** obtain the CMVP-validated
wolfCrypt FIPS module through wolfSSL's commercial channel (it ships the FIPS
service symbols + certificate). Build/configure it per wolfSSL's FIPS guidance.

---

## 6. Known limitations (stated plainly, not papered over)

FIPS-readiness in a Python application has real boundaries. M3 does not pretend
otherwise:

- **Key zeroization is best-effort.** CPython `bytes`/`str` are immutable and
  garbage-collected; key material in Python objects cannot be reliably wiped
  from memory. True zeroization requires keys to remain resident in the
  cryptographic module and never materialize in Python — a deeper redesign than
  M3 currently does. M3 minimizes key lifetime but **does not claim guaranteed
  zeroization** of Python-side key buffers.
- **Random-number generation scope.** M3's security-critical randomness (nonces,
  salts, tokens) uses the OS CSPRNG (`os.urandom` via `secrets`), which is a
  FIPS-grade *entropy source* but is the OS DRBG, not necessarily wolfCrypt's
  CTR-DRBG. Non-security identifiers (`uuid4` row/session IDs) are not routed
  through a FIPS DRBG by design. Strict deployments needing wolfCrypt-DRBG-only
  randomness should treat this as an open item.
- **The validation is wolfCrypt's, not M3's.** M3 uses a validated module "in an
  approved manner"; it carries no certificate of its own. Your deployment's
  assessor evaluates the combined system.

---

## 7. Verifying your deployment

```bash
m3 doctor          # shows the active crypto backend / FIPS tier
python bin/test_fips_integrity.py   # exercises the crypto abstraction + KATs
```

A correctly configured strict deployment reports the validated wolfCrypt module
active and the power-up KATs passing. A `M3_FIPS_MODE` (non-strict) deployment
reports wolfCrypt active but **not** validated — which is expected and correct
for the open-source build.

*See also: [`crypto_provider.py`](../bin/crypto_provider.py),
[`FIPS_COMPLIANCE.md`](FIPS_COMPLIANCE.md),
[`DESIGN_PHILOSOPHIES.md`](DESIGN_PHILOSOPHIES.md) §6.*
