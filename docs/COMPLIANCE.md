# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/icon.svg" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> M3 Memory — Compliance & Assurance

> Last updated: May 2026. Corrections welcome via [issue](https://github.com/skynetcmd/m3-memory/issues).

This page is the entry point to M3 Memory's compliance documentation. The detailed control-family mappings live as standalone HTML pages so they print cleanly and copy easily into agency packages.

---

## What M3 helps with

M3's local-first design — SQLite single-file store, local SLM extraction, zero telemetry by default — reduces the technical-controls portion of compliance work in regulated environments. It does **not** replace a compliance program; physical security, personnel screening, supply-chain controls, and most program-level requirements remain with the deploying organization.

What M3 ships natively that's relevant here:

- **GDPR primitives.** `gdpr_forget` (Article 17 — right to erasure) and `gdpr_export` (Article 20 — data portability) are built-in MCP tools. No custom code, no third-party services.
- **Bitemporal audit log.** Every write captures valid-time and transaction-time. Native undo via supersedes relationships preserves the full history of what was known, when.
- **Atomic concurrent writes.** SQLite WAL — multiple agents writing simultaneously without race conditions or silent failures.
- **Air-gap operability.** No network listeners, no telemetry, no implicit egress. Same code path runs on a developer laptop and inside an air-gapped enclave.
- **Encryption-friendly.** Compatible with BitLocker / FileVault / LUKS at the disk layer and SQLCipher at the database layer.

---

## Framework alignment

| Framework | Coverage | Document |
|---|---|---|
| **NIST SP 800-53 (FISMA)** | Federal information systems — agency ATO support | [FISMA / 800-53 alignment](https://html-preview.github.io/?url=https://github.com/skynetcmd/m3-memory/blob/main/docs/M3_Compliance_FISMA.html) |
| **CMMC 2.0 / NIST SP 800-171** | DoD contractors handling CUI — Level 2 controls | [CMMC 2.0 alignment](https://html-preview.github.io/?url=https://github.com/skynetcmd/m3-memory/blob/main/docs/M3_Compliance_CMMC.html) |
| **GDPR (Articles 17 & 20)** | EU data subject rights — built-in MCP tools | See [README "Why trust this"](../README.md#-why-trust-this) and [API_REFERENCE.md](API_REFERENCE.md) |

### What about FedRAMP?

FedRAMP authorizes cloud service providers. M3 has no cloud component, so FedRAMP does not apply. For workloads that can stay local, M3 keeps the data path on agency-controlled hardware — eliminating the shared-responsibility surface that a FedRAMP authorization is designed to evaluate. This is *not* a substitute for FedRAMP where a cloud service is genuinely required; it's an option for the workloads that don't need one.

---

## Honest scope

These compliance documents are written by the M3 team, not by accredited assessors. They map M3's behavior to control language; they don't substitute for an actual audit. Specifically:

- M3 itself is not "FISMA certified" or "CMMC certified" — those certifications apply to systems and organizations, not standalone software components.
- The control-by-control assessments reflect M3's design intent and observed behavior. Your assessor will evaluate the deployment, not the library.
- Where M3 inherits a control from the host OS (e.g. authentication), that's called out explicitly. Don't assume M3 carries those controls on its own.

If you're preparing an authorization package and have specific control questions, [open an issue](https://github.com/skynetcmd/m3-memory/issues) — we'll engage substantively.

---

## See also

- [M3 vs alternatives — sovereign substrates table](https://html-preview.github.io/?url=https://github.com/skynetcmd/m3-memory/blob/main/docs/M3_Comparison_Table.html) — where M3 fits in the broader sovereign-memory landscape
- [M3 vs alternatives — developer-tool guide](COMPARISON.md) — Mem0, Letta, Zep, LangChain Memory
- [Homelab patterns](HOMELAB_PATTERNS.md) — small-deployment guidance with similar local-first / sovereign requirements
- [Architecture](ARCHITECTURE.md) — system design that underlies the compliance posture
- [Technical details](TECHNICAL_DETAILS.md) — implementation specifics for assessors
