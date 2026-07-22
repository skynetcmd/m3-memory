# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> GDPR & the Right to Erasure in m3

> **This is not legal advice, and installing m3 does not make you GDPR compliant.**
> m3 provides *technical primitives* for data-subject rights. The *program-level*
> obligations (identity verification, legal-basis determination, exemption
> analysis, third-party notification, responding to the data subject, and keeping
> the case file) remain with the **deploying organization**. If you process other
> people's personal data, consult your Data Protection Officer (DPO) / legal
> counsel.

For most m3 users this page is academic: m3 is **local-first and single-user by
default**, and purely personal use generally falls under the GDPR "household
exemption" (Art. 2(2)(c)). It matters if you deploy m3 to hold **other people's**
personal data (a shared or multi-tenant deployment).

---

## What m3 provides (the technical layer)

| Capability | Tool / mechanism |
|---|---|
| **Right to erasure** (Art. 17) | `gdpr_forget` — hard-deletes all data for a `user_id`: memories, embeddings, relationships, history, and materialized surfaces. |
| **Right to portability** (Art. 20) | `gdpr_export` — exports a user's memories as portable JSON. |
| **Proof the erasure happened** | A record in `gdpr_requests` (subject, request type, item count, requested/completed timestamps) **and** an entry in the **tamper-evident, hash-chained audit trail**. |
| **Optional program-layer record** | `gdpr_forget`'s `compliance` field (see below) captures the erasure's context in the audit trail when the operator supplies it. |
| **No lingering copies in the wiki** | `m3 wiki generate` prunes pages for memories that no longer exist, so erased content does not survive in a previously-generated vault. |
| **No cloud, no telemetry** | Data stays on operator-controlled hardware; there is no third-party processor to notify by default. |

### The optional `compliance` record

`gdpr_forget` accepts an optional `compliance` object, logged verbatim to the audit
trail. It is **not required** and **not verified** — it lets an operator capture the
context an auditor expects, in the same tamper-evident log as the erasure itself:

| Field | Purpose |
|---|---|
| `legal_basis` | Which Art. 17(1) ground applies (a–f). |
| `reason` | Free-text rationale. |
| `verified_by` / `verification_method` | Who confirmed the requester's identity, and how. |
| `authorized_by` | Operator / DPO who approved the erasure. |
| `external_ref` | Case / ticket number linking to your DSAR record. |
| `retained_note` | What (if anything) was retained under an Art. 17(3) exemption, and why. |

Via MCP: `gdpr_forget(user_id="…", compliance={"legal_basis": "b) consent withdrawn", "authorized_by": "DPO Jane", "external_ref": "TICKET-42"})`.
The web dashboard's GDPR panel exposes the same fields under **Compliance details
(optional)**.

---

## What m3 does **not** do (the program layer — your responsibility)

These are the parts of an Art. 17 request that only your organization can perform.
m3 has no visibility into them and makes no attempt to enforce them. This is the
checklist a DPO / auditor typically expects a **data-subject-request log** to cover
(per ICO guidance and standard DSAR practice):

- **Identity verification** — confirm the requester *is* the data subject before
  erasing. m3 deletes whatever `user_id` you pass; it does not authenticate anyone.
- **Legal-basis determination** — decide *whether* Art. 17 applies (one of the
  grounds a–f) or whether an exemption lets you refuse.
- **Exemption analysis** (Art. 17(3)) — data you may/must retain (legal obligation,
  establishment/exercise/defense of legal claims, freedom of expression, public
  health, archiving). m3 does not assess this; you record it in `retained_note`.
- **Third-party notification** (Art. 19) — informing any recipients you disclosed
  the data to. m3 is local and has no third parties by default, but if *your*
  pipeline shares data downstream, you must notify them.
- **Responding to the data subject** — GDPR requires action within **one month**
  (extendable to three for complex cases), and a confirmation (or a documented
  refusal with reasons). m3 records timestamps; sending the response is yours.
- **The DSAR case file** — the overall request record: when it arrived, the channel,
  correspondence, decisions. m3's audit entry is one artifact within it, not a
  substitute for it.

Purpose-built DSAR platforms (OneTrust, Osano, DataGrail, …) exist to run this
program layer. m3 is a storage engine that executes and logs the deletion; it is
not a DSAR platform, and pairing it with one (or a manual process) is expected in a
regulated multi-tenant deployment.

---

## What survives a hard-delete (and why that's correct)

`gdpr_forget` erases the subject's **data** but deliberately keeps a **record that
the erasure occurred** — required to *demonstrate* compliance under the
accountability principle (Art. 5(2)). Retained: the `user_id` (as the subject of
the request), timestamps, item count, request id, your optional `compliance`
fields, and the tamper-evident chain. **Not** retained: the memories, their
content, embeddings, relationships, or history. Keeping proof-of-deletion is
permitted; it is not "still holding their data."

---

## References

- ICO — [Right to erasure](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/individual-rights/individual-rights/right-to-erasure/)
- Regulation text — Art. 17 (erasure), Art. 19 (notification), Art. 5(2) (accountability), Art. 17(3) (exemptions)
- See also [COMPLIANCE.md](COMPLIANCE.md) (m3's overall compliance posture) and
  [API_REFERENCE.md](API_REFERENCE.md#gdpr_forget) (the tool contract).
