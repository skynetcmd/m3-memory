# The m3 Memory Model

> **One-page answer to: "Is this a durable, governed knowledge base — or a vector
> store with RAG sugar?"** It's the former. This doc is the synthesizing index;
> each section links to the deep doc and cites the real table/column/function so
> the claims can't drift from the code.

m3 is not a pile of conversation logs behind a similarity search. It is a **typed,
bitemporal, confidence-scored, self-maintaining knowledge base** with explainable
retrieval. Every property below is a first-class column or a named function, not a
roadmap item.

---

## 1. Typed, structured memory (not free text)

Every memory is a row in `memory_items` with typed fields — the store behaves like
a database of facts, not a transcript. Real columns (`PRAGMA table_info`):

| Field | Column(s) | What it gives you |
|---|---|---|
| **Kind** | `type` | note / decision / fact / preference / observation / belief / summary / message … |
| **Provenance** | `source`, `change_agent`, `origin_device`, `variant` | *how* it came to exist: explicit agent write vs. chatlog ingest vs. file extraction vs. enrichment; which agent/model wrote it; which ingestion pipeline |
| **Confidence** | `confidence`, `belief_alpha`, `belief_beta` | a clamped [0,1] score AND a Bayesian Beta posterior (α/β) that updates with evidence |
| **Corroboration** | `corroboration_count`, `contradiction_count` | how many independent sources agree / conflict |
| **Scope** | `scope` (`user`/`session`/`agent`/`org`), `user_id`, `conversation_id` | who it belongs to; enforced at the SQL layer |
| **Salience** | `importance`, `decay_rate`, `access_count`, `last_accessed_at` | how much it matters and how it ages |
| **Integrity** | `content_hash` | SHA-256 written on every write; `memory_verify` re-checks |

A real (redacted) row:
```json
{ "type": "note", "source": "agent", "change_agent": "claude-code", "scope": "agent",
  "confidence": 0.5, "belief_alpha": null, "belief_beta": null,
  "corroboration_count": 0, "contradiction_count": 0,
  "created_at": "2026-07-02T02:18:39Z", "valid_from": "2026-07-02T02:18:39Z",
  "valid_to": null, "expires_at": null, "decay_rate": 0.0 }
```
*Deep dive:* [TECHNICAL_DETAILS.md](TECHNICAL_DETAILS.md) (schema),
[CONFIDENCE_AND_TRUST.md](CONFIDENCE_AND_TRUST.md) (the confidence model).

---

## 2. Bitemporal history (what did we believe, and when?)

Two independent time axes let you reconstruct the past *and* the past-as-we-then-
understood-it:
- **Valid time** — `valid_from` / `valid_to`: the window a fact was *true in the world*.
- **Transaction time** — `created_at` / `updated_at`: when m3 *recorded or changed* it.

So m3 can answer **"what did we believe last Tuesday, and when was that corrected?"**:
a superseded fact isn't deleted — its `valid_to` is closed and a successor row is
written, with a `supersedes` relationship edge and a `supersede` history event. The
full timeline survives (`_mark_superseded`, `memory_supersede_impl`). `memory_history`
replays it.

*Deep dive:* [ARCHITECTURE.md](ARCHITECTURE.md) (scoping & bitemporal),
[TECHNICAL_DETAILS.md](TECHNICAL_DETAILS.md).

---

## 3. Automatic contradiction handling

On write, `_check_contradictions` compares the new item against existing memory
(vector similarity + type + content). A genuine conflict **automatically supersedes**
the stale fact (closes its validity interval, links the successor) and bumps
`contradiction_count`; corroboration bumps `corroboration_count` and the Beta
posterior. So the store doesn't accumulate contradictory history that makes outdated
facts look authoritative — conflicts are resolved, not piled up.

*Related tools:* `memory_supersede`, `memory_update`, and the autonomous cognitive
loop that drains enrichment/contradiction work.

---

## 4. Self-maintaining lifecycle (decay, dedup, expiry, revocation)

Memory isn't write-once-keep-forever:
- **Decay** — `decay_rate` + `decay_toward_neutral`: confidence drifts toward neutral
  as a memory ages unless reinforced.
- **Expiry / TTL** — `expires_at`: session-scoped memories auto-expire (24h); any
  memory can carry a TTL. Short-lived task notes and long-lived preferences age
  differently.
- **Refresh** — `refresh_on` / `refresh_reason`: memories can be queued for re-review.
- **Dedup / consolidate** — `memory_dedup_impl`, `memory_consolidate_impl`: near-
  duplicates merge; aged low-order groups roll up into higher-order `belief` memories.
- **Retention / revocation** — `memory_set_retention_impl`, and GDPR erasure via
  `gdpr_forget_impl` (hard-deletes all of a user's data across tables).

*Deep dive:* [CORE_FEATURES.md](CORE_FEATURES.md), [COMPLIANCE.md](COMPLIANCE.md).

---

## 5. Write-gating (remember fewer things, better)

Not every candidate fact becomes durable memory. Turns flow through an
`observation_queue` → enrichment → **promotability** scoring; only high-signal
memories are promoted. A **content-safety gate** rejects XSS / SQL-injection /
prompt-injection at the write boundary before anything is stored. This is why
retrieval stays clean instead of drowning in low-value noise.

---

## 6. Explainable retrieval (why was this remembered?)

Retrieval is **hybrid** — dense vector + FTS5 BM25, MMR-diversified, with recency and
temporal boosts and a cross-encoder rerank guard (`_hybrid_score_batch`,
`_apply_recency_bonus`, `_apply_temporal_boost`, `_apply_rerank`, MMR). It is
**goal-aware**, not just similarity: intent routing (`_maybe_route_query`,
`is_temporal_query`) filters by query type so a temporal question widens verbatim
recall and a factual one doesn't drag in unrelated context.

And it **shows its work.** `memory_suggest` is `memory_search(..., explain=True)`, and
every result carries an `_explanation` breakdown of the exact math:
```json
"_explanation": {
  "vector": 0.72, "bm25": 0.18, "importance": 0.6,
  "raw_hybrid": 0.61, "length_penalty": 1.0, "title_overlap": 0.05
}
```
(plus recency/temporal components). So "why did you remember this?" is answerable with
numbers, not a shrug — the core of trust and debuggability.

*Deep dive:* [CONFIDENCE_AND_TRUST.md](CONFIDENCE_AND_TRUST.md).

---

## 7. Measured, not asserted

Recall quality is benchmarked, not claimed: **LongMemEval-S — 92.0% end-to-end QA,
99.2% SHR@k=10**, with per-category breakdowns and methodology.
*See:* [benchmarks/longmemeval/LME-S_Benchmarking_Report.md](../benchmarks/longmemeval/LME-S_Benchmarking_Report.md).

---

## The through-line

Typed schema · provenance · confidence · bitemporal history · automatic contradiction
resolution · self-maintaining lifecycle · write-gating · explainable, goal-aware
retrieval · measured recall — held together, local-first, with GDPR erasure and
per-scope isolation. That is a **dependable memory brain**, and every piece of it is
already in the code cited above.
