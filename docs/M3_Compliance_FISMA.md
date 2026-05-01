# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/icon.svg" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> FISMA / NIST SP 800-53 Alignment

> Last updated: May 2026. How M3 Memory supports federal information-security controls.

---

## Scope of this document

> **What this is:** A control-family mapping showing how M3 Memory supports FISMA / NIST SP 800-53 requirements when deployed inside a federal agency or contractor environment.
>
> **What this is not:** A statement that M3 itself is "FISMA certified." FISMA accredits federal information systems through agency Authorization to Operate (ATO) processes — not standalone software components. M3 is a memory substrate that helps your system meet the technical controls.

FISMA implementation is governed by NIST SP 800-53 control families. The table below shows where M3's local-first design supports those controls, where it meets them at parity, and where the deploying agency retains responsibility.

---

## FedRAMP applicability

**FedRAMP does not apply to M3 Memory.** FedRAMP authorizes *cloud service providers* (IaaS, PaaS, SaaS). M3 is local-first software with no cloud component — there is no service to authorize.

For agencies considering whether M3 fits a workload that would otherwise require a FedRAMP-authorized service: M3 keeps the entire memory data path on agency-controlled hardware, removing the shared-responsibility surface that FedRAMP authorizations are designed to evaluate. This isn't a substitute for FedRAMP where a cloud service is genuinely needed; it's an option for workloads that can stay local.

---

## M3 alignment with NIST SP 800-53 (FISMA)

> **Assessment language:**
> - **Superior** — M3 actively reduces complexity vs. typical cloud baseline.
> - **Strong** — solid implementation.
> - **Meets / Equivalent** — covered at parity; deploying organization retains ownership.

| FISMA / NIST Requirement | Derived From | How M3 Memory Supports It | Assessment |
|---|---|---|---|
| **Access Control (AC)** — least privilege, session control | NIST SP 800-53 Rev 5, AC family | Inherits host OS authentication and filesystem ACLs. No remote access or network listener by default — eliminates remote-access controls from scope. | **Superior** |
| **Audit & Accountability (AU)** | NIST SP 800-53 Rev 5, AU family | Bitemporal logging captures every write with valid-time and transaction-time. Native undo plus supersedes relationships preserve historical context. Optional Merkle-style integrity available. | **Superior** |
| **Configuration Management (CM)** | NIST SP 800-53 Rev 5, CM family | Minimal, auditable codebase — Native Python + SQLite. No external dependencies or containers required for the data path. | **Strong** |
| **Incident Response (IR)** | NIST SP 800-53 Rev 5, IR family | Local-only data path keeps incidents containable on agency-controlled hardware. No cloud tenant to coordinate with during response. | **Strong** |
| **Media Protection (MP)** — encryption at rest | NIST SP 800-53 Rev 5, MP family | Compatible with full-disk encryption (BitLocker / FileVault / LUKS) plus optional SQLite-level encryption (e.g. SQLCipher). Single-file DB simplifies sanitization. | **Meets / Equivalent** |
| **Physical & Environmental Protection (PE)** | NIST SP 800-53 Rev 5, PE family | Runs on agency-controlled hardware; M3 itself has no environmental requirements beyond the host. Physical security remains the deploying agency's responsibility. | **Strong** |
| **System & Communications Protection (SC)** | NIST SP 800-53 Rev 5, SC family | **Zero network required by default.** Eliminates an entire class of boundary-protection controls when M3 is the only memory layer in scope. | **Superior** |
| **System & Information Integrity (SI)** | NIST SP 800-53 Rev 5, SI family | Bitemporal logic plus local SLM fact extraction provides high integrity and tamper evidence. Updates flow through reproducible `pip` installs. | **Superior** |
| **Data Residency & Sovereignty** | FISMA / NIST control objectives | 100% local SQLite with no cloud component or telemetry. Full data residency on agency hardware, including air-gapped operation. | **Superior** |
| **Risk Assessment & Authorization (RA / ATO)** | NIST RMF / FISMA | Small attack surface and minimal dependency tree simplify agency risk assessment and ATO documentation. | **Strong** |

> ⚠️ **What M3 doesn't give you for free:** personnel screening, physical security, supply-chain risk management, contingency planning, and most program-level controls remain the agency's responsibility. M3 strengthens the technical-controls portion; it doesn't replace your authorization package.

---

See also: [CMMC 2.0 alignment](M3_Compliance_CMMC.md) · [Compliance index](COMPLIANCE.md) · [README](../README.md)

Last updated May 2026 — corrections welcome via [GitHub issue](https://github.com/skynetcmd/m3-memory/issues).
