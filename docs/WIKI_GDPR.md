# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> The Wiki & the Right to Erasure

The [M3 Wiki](WIKI.md) compiles your memories into synthesized topic pages. A
synthesis is **derived content that outlives its source**: it quotes and
paraphrases the memories it was built from, and it persists as its own row after
those memories change or are deleted. That makes erasure (GDPR Art. 17) a
first-class concern for the wiki, and a distinct one from erasing an ordinary
memory — which the core [GDPR & the Right to Erasure](GDPR_COMPLIANCE.md) doc
covers.

This page documents the **third category** that the "What survives a hard-delete"
list in that doc does not: content *derived from* an erased source.

## What happens when you erase a memory that fed a synthesis

`gdpr_forget` hard-deletes the memory. But a synthesis compiled from it is a
separate row — often under a different `user_id` (the wiki compiles under its own
scope), so the erasure cascade never reaches it. Left alone, the page would keep
rendering prose derived from data the subject asked to be forgotten.

m3 closes that gap automatically. During `gdpr_forget`, **before** the cascade
removes the `consolidates` provenance edges, m3 scans them for syntheses derived
from the erased member and marks each one `restricted`:

1. **`restricted` is set immediately.** It is a separate metadata field that
   **overrides** the synthesis's `authority` — even a `canonical` page stops
   rendering from the instant of erasure. Exposure is bounded at ~zero.
2. **The prose is NOT deleted.** The erased member may have been **redundant** — if
   the claim is corroborated by surviving members, the page carries no information
   unique to the deleted row and nothing needs to change. Auto-deleting would
   destroy correct pages to satisfy a guess, and m3 never performs destructive edits
   automatically (see [`promotability`](WIKI.md) — heuristics never auto-mutate).
3. **The row, its edges, and its history are retained** for review.
4. **An erasure record is written** to the synthesis metadata (how many members
   were erased, when, and why) and to the erasure's audit-trail entry
   (`wiki_restricted`) — the derived-content half of the accountability trail.

### The review that follows (a deliberate human/authorized act)

A `restricted` synthesis is not the end state — it is a page **awaiting a
derivability decision**: *is this synthesis still derivable from the surviving
members alone?* Three outcomes, all human-confirmed, never automatic:

- **A — rebuild clean:** recompile from the surviving members only; the new prose
  supersedes the restricted page.
- **B — excise dependents:** keep the page as a base, remove only the claims that
  depend on the erased item.
- **C — keep base + survivor delta:** hold the page and show what is derivable from
  the non-erased members, for the reviewer to compare.

Whatever is committed **supersedes** the restricted synthesis (never an in-place
edit). Until then the page stays withheld. The review queue is surfaced in the
wiki's **`lint.md` → "Restricted — GDPR review"** section, so no restricted page is
hidden in a column.

> The automated review queue and the optional after-N-days LLM derivability check
> are a follow-on; the current release sets and surfaces the flag, and withholds
> rendering. The `restricted` flag halts publication regardless, so the review
> window governs *surfacing*, not destruction.

## The cluster cache and erasure

Clusters are **recomputed** each build, not stored — so there is no persistent
cluster cache to flush today. If a future release materializes clusters (a
`cluster_run` provenance record with `cluster_members` as a cache), erasure will
scan that membership for the erased id and flush the cached rows for any affected
run, while retaining the run record itself (the erasure record must outlive the
data). That design keeps **UUIDs only, never content**, in the cache — so a hit is
detectable and the cache holds no personal data.

## Erasure breaks `--as-of` reproducibility — and m3 says so

The wiki's `--as-of` reproducibility depends on superseded rows being *retained*
with their validity interval closed. **`gdpr_forget` hard-deletes** — an erased
member is gone from every temporal view. Recomputing a past build then yields a
*smaller* cluster, which may partition differently, which yields a **different
vault**. Byte-identical reproducibility is broken by a legally mandatory deletion.

This is correct and unavoidable — erasure wins over reproducibility. What m3 does
**not** do is hide it: a rebuild of an affected range is **loud**, naming the
counts rather than silently diverging:

> This build cannot reproduce an earlier one: N members were erased (GDPR Art. 17).
> The rebuilt vault WILL differ from the original.

Preserving content to keep reproducibility would be an Art. 17 violation hiding in
a cache; m3 does not do that. The three wrong resolutions m3 deliberately avoids:
silent divergence, caching content to fake reproducibility, and refusing to erase.

## Summary — what erasure does to the wiki

| Artifact | On erasure of a source member |
|---|---|
| The erased memory | Hard-deleted (as always) |
| A synthesis derived from it | **`restricted`** — stops rendering; prose retained for review |
| The synthesis's edges / history | Retained (for the derivability review) |
| Erasure record | Written to synthesis metadata + audit trail |
| `lint.md` | Lists the restricted page in "Restricted — GDPR review" |
| `--as-of` reproducibility of affected builds | Broken — and reported loudly, never silently |

---

## References

- [GDPR & the Right to Erasure in m3](GDPR_COMPLIANCE.md) — the core erasure doc
  (what survives a hard-delete, the `compliance` record, the program layer).
- [The M3 Wiki](WIKI.md) — how syntheses are compiled and rendered.
- Regulation text — Art. 17 (erasure), Art. 5(2) (accountability).
