# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/icon.svg" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> CMMC 2.0 Alignment

> Last updated: May 2026. How M3 Memory supports defense-contractor security controls.

---

## Scope of this document

> **What this is:** A control-by-control mapping showing how M3 Memory supports CMMC 2.0 / NIST SP 800-171 requirements when deployed inside a contractor environment that handles **CUI** (Controlled Unclassified Information).
>
> **What this is not:** A statement that M3 itself is "CMMC certified." CMMC certifies organizations and their information systems, not standalone software components. M3 is a memory substrate that helps your organization meet the technical controls.

CMMC 2.0 Level 2 maps directly to NIST SP 800-171. The assessment table below shows where M3 reduces compliance burden, where it meets requirements at parity with other approaches, and where the deploying organization remains responsible.

---

## Bottom line

M3's local-first architecture removes the cloud-shared-responsibility model that drives most CMMC assessment cost and complexity. Practical implications:

- **Reduced attack surface.** No network listeners by default, no telemetry, no third-party dependencies in the data path.
- **Smaller assessment boundary.** CUI processed by M3 stays on user-controlled hardware — no shared cloud tenancy to evaluate.
- **Audit-ready by design.** Bitemporal logging captures who-wrote-what-when natively; no separate audit pipeline required.

M3 does not eliminate the contractor's compliance work — physical security, personnel screening, and many program-level controls still apply. It does shrink the technical-controls portion meaningfully.

---

## M3 alignment with CMMC 2.0 (NIST SP 800-171)

> **Assessment language:**
> - **Superior** — M3 actively reduces complexity vs. typical cloud baseline.
> - **Strong** — solid implementation.
> - **Meets / Equivalent** — covered at parity; deploying organization retains ownership.

| CMMC / NIST 800-171 Area | Key Requirements | How M3 Memory Supports It | Assessment |
|---|---|---|---|
| **Access Control** | Limit access to authorized users; enforce least privilege | Inherits host OS authentication and filesystem ACLs. No remote network listener by default — no remote access surface to harden. | **Superior** |
| **Audit & Accountability** | Generate, protect, and review audit records | Bitemporal logging captures every write with valid-time and transaction-time. Native undo + supersedes relationships preserve full historical context. Optional Merkle-style integrity available. | **Superior** |
| **Configuration Management** | Establish and maintain baseline configurations; manage change | Minimal, auditable codebase — Native Python + SQLite. No containers, no external services to baseline. Configuration is a single YAML file per profile. | **Strong** |
| **Identification & Authentication** | Identify and authenticate users and processes | Delegates to host OS authentication and local permissions. Pairs cleanly with smart card / PIV / Yubikey-backed OS login. | **Meets / Equivalent** |
| **Media Protection** | Protect CUI at rest; manage media sanitization | Compatible with full-disk encryption (BitLocker / FileVault / LUKS) and SQLite-level encryption extensions (e.g. SQLCipher). Single-file DB simplifies sanitization workflows. | **Strong** |
| **Incident Response** | Detect, respond to, and report incidents | Local-only data path means incidents stay containable on user-controlled hardware. No cloud tenant to coordinate with during response. | **Strong** |
| **System & Communications Protection** | Boundary protection; encryption in transit | **Zero network required by default.** Eliminates the entire class of network-boundary controls when M3 is the only memory layer in scope. | **Superior** |
| **System & Information Integrity** | Flaw remediation; malicious code protection; information handling | Bitemporal logic plus local SLM extraction provides high integrity and tamper evidence. Updates flow through standard `pip` with reproducible installs. | **Superior** |
| **Data Residency & Sovereignty** | Control where CUI is stored and processed | 100% local SQLite on user-controlled hardware. No cloud component, no telemetry, no implicit data egress. | **Superior** |
| **Risk Assessment** | Periodic risk assessment; third-party assessment at Level 2 | Small attack surface and minimal dependency tree simplify both internal risk assessments and third-party C3PAO audits. | **Strong** |

> ⚠️ **What M3 doesn't give you for free:** personnel security, physical security, awareness training, supply-chain risk management, and most program-level controls remain the deploying organization's responsibility. M3 strengthens the technical-controls portion; it doesn't replace your compliance program.

---

See also: [FISMA / NIST SP 800-53 alignment](M3_Compliance_FISMA.md) · [Compliance index](COMPLIANCE.md) · [README](../README.md)

Last updated May 2026 — corrections welcome via [GitHub issue](https://github.com/skynetcmd/m3-memory/issues).
