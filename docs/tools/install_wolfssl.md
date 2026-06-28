---
tool: bin/install_wolfssl.py
sha1: 0863a19803db
mtime_utc: 2026-06-28T01:29:35.789025+00:00
generated_utc: 2026-06-28T01:32:31.452364+00:00
private: false
---

# bin/install_wolfssl.py

## Purpose

install_wolfssl.py — build the OPEN-SOURCE wolfSSL library from official
source and install it where M3's secure crypto loader finds it (~/.m3/lib).

Why build instead of download a binary:
  - M3 is Apache-2.0; wolfSSL is GPLv2-or-commercial. M3 must NOT bundle or
    redistribute the binary. Building from the OFFICIAL source on the user's own
    machine keeps M3 license-clean — this script just automates the steps you'd
    run by hand.
  - For a crypto library, provenance matters. We clone only the official
    wolfSSL/wolfssl repo and you can audit/verify every step.

What you get: the OPEN-SOURCE wolfCrypt build — usable with M3_FIPS_MODE=1
(hardened, fail-closed, KAT-checked). It is NOT the CMVP-validated FIPS module
(that is commercial + NDA-gated; M3_FIPS_STRICT requires it). See
docs/FIPS_MODULE_BOUNDARY.md.

Usage:
    python bin/install_wolfssl.py            # clone, build, install to ~/.m3/lib
    python bin/install_wolfssl.py --print-sha # also print the SHA-256 to self-pin
    python bin/install_wolfssl.py --ref v5.9.2  # pin a specific wolfSSL tag

Prerequisites: git, plus a C toolchain —
    Linux/macOS: autoconf/automake/libtool + make + a C compiler (autotools), OR
                 cmake + a generator (Ninja/Make).
    Windows:     cmake + Visual Studio Build Tools (C++ workload).

---

## Entry points

- `def main()` (line 148)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--ref` | f'wolfSSL git tag/branch (default {DEFAULT_REF}).' | `DEFAULT_REF` |  | str |  |
| `--dest` | Install dir (default: M3's ~/.m3/lib). | None |  | str |  |
| `--print-sha` | Print the installed lib's SHA-256. | `False` |  | store_true |  |
| `--keep-build` | Don't delete the temp build tree. | `False` |  | store_true |  |

---

## Environment variables read

- `M3_CONFIG_ROOT`
- `M3_MEMORY_ROOT`

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `cmd`` (line 63)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `autogen.sh`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
