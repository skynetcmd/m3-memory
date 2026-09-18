# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> m3 Memory — Compliance & Assurance

> Last updated: May 2026. Corrections welcome via [issue](https://github.com/skynetcmd/m3-memory/issues).

This page is the entry point to m3 Memory's compliance documentation. The detailed control-family mappings live as standalone HTML pages so they print cleanly and copy easily into agency packages.

---

## What m3 helps with

m3 ships compliance-relevant primitives natively — built-in GDPR erasure/export MCP tools, a bitemporal audit log capturing what was known and when, full air-gap operability, and zero telemetry by default. Combined with the local-first design (SQLite single-file store, local SLM extraction), this materially reduces the technical-controls portion of compliance work in regulated environments. It does **not** replace a compliance program; physical security, personnel screening, supply-chain controls, and most program-level requirements remain with the deploying organization.

What m3 ships natively that's relevant here:

- **GDPR primitives.** `gdpr_forget` (Article 17 — right to erasure) and `gdpr_export` (Article 20 — data portability) are built-in MCP tools. No custom code, no third-party services. What m3 does vs. what remains the operator's responsibility is spelled out in **[GDPR_COMPLIANCE.md](GDPR_COMPLIANCE.md)**.
- **Bitemporal audit log.** Every write captures valid-time and transaction-time. Native undo via supersedes relationships preserves the full history of what was known, when.
- **Atomic concurrent writes.** SQLite WAL — multiple agents writing simultaneously without race conditions or silent failures.
- **Air-gap operability.** Loopback-only listeners (the shared embed server on
  `127.0.0.1:8082`, the optional dashboard on `127.0.0.1:8088`) — no LAN or
  external listener by default, no telemetry, no implicit egress. Same code path runs on a developer laptop and inside an air-gapped enclave.
- **Encryption-friendly.** Compatible with BitLocker / FileVault / LUKS at the disk layer and SQLCipher at the database layer.

---

## Framework alignment

| Framework | Coverage | Document |
|---|---|---|
| **NIST SP 800-53 (FISMA)** | Federal information systems — agency ATO support | [FISMA / 800-53 alignment](M3_Compliance_FISMA.md) |
| **CMMC 2.0 / NIST SP 800-171** | DoD contractors handling CUI — Level 2 controls | [CMMC 2.0 alignment](M3_Compliance_CMMC.md) |
| **GDPR (Articles 17 & 20)** | EU data subject rights — built-in MCP tools | See [README "Why trust this"](../README.md#-why-trust-this) and [API_REFERENCE.md](API_REFERENCE.md) |

### What about FedRAMP?

Because m3 has no cloud component, it keeps the data path entirely on agency-controlled hardware — eliminating the shared-responsibility surface that a FedRAMP authorization exists to evaluate. FedRAMP authorizes cloud service providers, so it simply does not apply. This is *not* a substitute for FedRAMP where a cloud service is genuinely required; it's the better answer for the workloads that don't need one.

---

## Honest scope

These compliance documents are written by the m3 team, not by accredited assessors. They map m3's behavior to control language; they don't substitute for an actual audit. Specifically:

- m3 itself is not "FISMA certified" or "CMMC certified" — those certifications apply to systems and organizations, not standalone software components.
- The control-by-control assessments reflect m3's design intent and observed behavior. Your assessor will evaluate the deployment, not the library.
- Where m3 inherits a control from the host OS (e.g. authentication), that's called out explicitly. Don't assume m3 carries those controls on its own.

If you're preparing an authorization package and have specific control questions, [open an issue](https://github.com/skynetcmd/m3-memory/issues) — we'll engage substantively.

---

## See also

- [m3 vs alternatives — sovereign substrates table](M3_Comparison_Table.md) — where m3 fits in the broader sovereign-memory landscape ([interactive version](https://html-preview.github.io/?url=https://github.com/skynetcmd/m3-memory/blob/main/docs/M3_Comparison_Table.html))
- [m3 vs alternatives — developer-tool guide](COMPARISON.md) — Mem0, Letta, Zep, LangChain Memory
- [Homelab patterns](HOMELAB_PATTERNS.md) — small-deployment guidance with similar local-first / sovereign requirements
- [Architecture](ARCHITECTURE.md) — system design that underlies the compliance posture
- [Technical details](TECHNICAL_DETAILS.md) — implementation specifics for assessors
