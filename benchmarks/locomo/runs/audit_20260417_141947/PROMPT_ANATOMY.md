# Phase 1 — Answerer Prompt Anatomy & Waste Analysis

Dataset: 500 LOCOMO questions across conv-26, conv-30, conv-41, conv-42 (all re-ingested with Tier 1 ranker active). Each Q's answerer prompt was reconstructed by replaying `format_retrieved` over the ranked hits the audit captured.

## Headline numbers (overall, n=500)

| Metric | Value |
|---|---|
| mean prompt size | **23,875 chars** (~5,970 tokens) |
| mean history section | 20,434 chars (86% of prompt) |
| mean rendered sessions | 16.3 per Q |
| mean rendered sessions containing gold | **1.04** (out of 16.3) |
| **% of prompt chars in sessions with NO gold** | **75.9%** |
| retrieval "any hit" rate | 86.0% |
| gold actually present in final prompt | **58.0%** |
| Qs where retrieval hit but gold pruned from prompt | **140 / 500 (28%)** |

## Where the characters go (500-Q totals)

```
Total prompt chars:       11,937,846  (100.0%)
├── Sessions WITHOUT gold: 9,058,550  ( 75.9%)   ← waste
├── Temporal anchors:        761,793  (  6.4%)
├── Sessions WITH gold:      620,316  (  5.2%)   ← signal (barely)
├── Timeline (all 35 sess):  582,226  (  4.9%)   ← mostly waste
├── Observations/summaries:  538,127  (  4.5%)
└── System + footer:         377,834  (  3.1%)
```

**The content the answerer actually needs (sessions with gold + observations + anchors + footer) is ~19% of what gets sent.** The rest is padding from 15 gold-free session blocks.

## Per-category breakdown

| Category | n | prompt_chars | gold_in_prompt | hit-but-missing | sessions_rendered | sessions_w/_gold | % wasted on gold-free sessions |
|---|---|---|---|---|---|---|---|
| adversarial | 112 | 24,547 | 59.8% | 34 | 16.9 | 0.91 | 94.7% |
| temporal    |  91 | 22,146 | 63.7% | 17 | 14.7 | 0.84 | 94.2% |
| single-hop  | 200 | 24,024 | 51.5% | 64 | 16.5 | 0.85 | 94.9% |
| multi-hop   |  75 | 23,371 | 68.0% | 17 | 16.0 | 1.91 | 87.7% |
| open-domain |  22 | 27,977 | 50.0% | 8  | 19.2 | 1.27 | 93.3% |

Multi-hop has the best waste ratio (87.7%) — it's the only category where ~2 sessions with gold are typically rendered. Every other category averages <1 session-with-gold out of 16+ rendered.

## The "hit but gold pruned" bug (140 / 500 Qs)

**Every single one of the 140 cases has the same shape:** retrieval found a gold dia_id at some rank, `format_retrieved` grouped hits by `conversation_id`, the gold session block *was* rendered — but the actual gold turn didn't appear in the 8 turns shown.

Mechanism (in `bin/bench_locomo.py::format_retrieved`, lines 460-526):
1. For each hit with `turn_index`, append to `by_session[cid][:8]`. **First-come-first-served.**
2. After processing all hits, for sessions that have fewer than 8 turns filled, backfill from DB via `ORDER BY created_at ASC LIMIT 5`.
3. Render each session's turns, sorted by `turn_index`.

Bug: if 8 *non-gold* turns from session S rank above the gold turn in the same session S, the gold turn is silently dropped. Backfill (step 2) is skipped because the slot is already full.

**Worst case seen (Q0):** gold = D1:3 (Caroline: "I went to a LGBTQ support group yesterday..."). Retrieval rank 96. Session 1 rendered with 8 turns — turn indices 0, 3, 4, 5, 6, 7, 8, 9. Turn 2 (the gold) excluded.

This bug alone explains a large fraction of the 42% gap between retrieval-hit-rate (86%) and gold-in-prompt-rate (58%).

## Gold position in prompt

- Mean first-gold offset: **12,923 chars deep** (roughly line 100 of the user message).
- The prompt structure puts timeline + anchors first (~2.7KB), then observations, then ~16 session blocks in date order. Gold tends to fall in the middle.
- Models with weak middle-context attention (most local LLMs under 14B) will struggle with this layout regardless of retrieval quality.

---

# Proposals

Ordered by expected impact × inverse-risk.

## A. Answer-time changes (no re-ingest needed)

### A1. Fix MAX_TURNS pruning — always include gold-ranked turns
**Savings:** +28pp on gold-in-prompt rate (58% → ~86%, matching retrieval hit rate).
**Risk:** Low. Single `format_retrieved` change.
**Implementation:** Before capping at MAX_TURNS, dedupe by `(session_id, turn_index)` and take top-8 by retrieval score, not by order-of-appearance. Alternatively, raise MAX_TURNS per-session only for the session containing the top-ranked hit.

### A2. Drop session blocks that contain no retrieval hit (just backfill-neighbors)
**Savings:** ~4.5M chars (38% of total prompt). Across 500 Qs this would drop mean prompt size from 23.9K → ~15K chars.
**Risk:** Medium for multi-hop/open-domain where backfill-neighbor turns occasionally provide useful context. Should be gated: keep sessions where at least one hit scored above a threshold OR always keep the top-5 highest-scoring sessions.
**Implementation:** In `format_retrieved`, track per-session max hit-score, drop sessions with no direct hit (i.e., only backfill).

### A3. Kill the "Timeline of All Sessions" block
**Savings:** ~582K chars across 500 Qs (4.9% of total). ~1,164 chars per Q.
**Risk:** Low. The session date appears redundantly in each `[Session on <date>]` header within history. The timeline block is only useful if the answerer needs to reason about sessions that didn't match retrieval — rare.
**Implementation:** One line in `ANSWER_TEMPLATE` / `bench_locomo.py`. For temporal questions that *do* need the full session list, gate by `q_signal == "temporal" and not anchors`.

### A4. Limit session blocks to top-K by relevance (K=5)
**Savings:** Median Q renders 16 sessions; dropping to top-5 cuts session-block chars by ~70%.
**Risk:** Medium. Adversarial and multi-hop Qs sometimes touch 2-3 sessions — 5 is generous. Single-hop almost always has 1 gold session.
**Implementation:** After ranking session blocks by max-hit-score-within-session, render only the top-5. Renders gold more saliently.

### A5. Interleave observations with sessions in score order, not blocks
**Savings:** None directly. Potential answer-quality improvement — observations currently always appear before session content, even when a top-ranked session turn would answer the Q directly.
**Risk:** Low structural change, but could regress on summarization-heavy Qs.

### A6. Move Temporal Anchors to end, after history
**Savings:** None. Argument: the anchor list is most useful *after* the answerer has seen history and identified which session matters. Placing it before forces the model to hold 25 anchors in working memory before seeing the content.
**Risk:** Low. Pure ordering.

### A7. Drop the "Today's date" footer for non-temporal Qs
**Savings:** ~50 chars/Q × 500 = 25K chars. Small.
**Risk:** None for signal Q-types, because the date only matters for temporal reasoning.

---

## B. Ingest-time changes

### B1. Emit per-session "turn summary" rows that link back to raw turns
**Savings:** Enables a "compact mode" where `format_retrieved` renders a 2-line summary of each low-scoring session instead of 8 raw turns.
**Risk:** Medium. Adds small-LLM call at ingest (Tier 2 territory). Behind flag.
**Implementation:** During `ingest_sample_with_graph`, generate one "session gist" per session via small LLM. Store as type `summary` with metadata pointing at member turn dia_ids. At answer time, prefer rendering gist + gold turns for sessions where no raw turns score in top-40.

### B2. Improve temporal-anchor coverage for gold turns
**Observation from trace:** For Q49 (gold D12:15 "We had a blast last year at the Pride fest"), no anchor was created for this turn even though "last year" is the exact phrase that needs resolution.
**Savings:** Indirect — raises gold score in temporal Qs by making the anchor-tagged turn match the question's time-word.
**Risk:** Low. Regex-based; no LLM cost.
**Implementation:** Audit `bench_locomo.ingest_sample_with_graph` temporal-anchor generator for coverage gaps. Confirm "last year", "last month", "this year", etc., are all extracted.

### B3. Index dia_id into the title/content for FTS retrieval
**Savings:** None directly. Potential retrieval quality: questions like "what did Caroline say in Session 12" become directly retrievable.
**Risk:** Low. 10-char title bloat per memory. Requires reingest.

### B4. Store role as first-class column, not just metadata_json
**Observation:** Tier 1's speaker-in-title helper works around the fact that FTS can't see `metadata_json.role`. A dedicated column with an index lets queries filter by speaker without the title hack.
**Risk:** Low. Backward-compat via migration.
**Savings:** Cleaner, not smaller — but enables removing the `[Speaker] ` title prefix, which visually doubles the speaker name on LOCOMO titles.

---

## Recommended order

1. **A1 (fix MAX_TURNS pruning)** — single bug fix with biggest signal-quality win. Bump gold-in-prompt from 58% → ~86%.
2. **A3 (drop timeline block)** — 5% smaller prompts, zero risk.
3. **A4 (cap session blocks at 5)** — ~50% smaller prompts if combined with A1.
4. **B2 (temporal-anchor coverage audit)** — cheap, helps temporal category specifically.
5. **A2 (drop no-hit session blocks)** — larger cut but wants benchmarking to confirm no quality regression on multi-hop.
6. **A6 + A7 (minor reorders)** — bundle opportunistically.

Defer B1 until after A1+A3+A4 measure, because the dominant win from B1 (session-level compaction) overlaps with A4. Defer B3/B4 to a separate schema-migration PR.

## Validation plan

After each proposal, rerun `benchmarks/Phase1/retrieval_audit.py --limit 500` and `analyze_prompt.py` to produce a comparable summary. Track three north-star metrics:

- `gold_in_prompt_rate` — % of Qs where gold content reaches the answerer
- `mean_prompt_chars` — context cost
- `mean_first_gold_offset` — how deep gold sits

Target after A1+A3+A4 combined: `gold_in_prompt_rate > 0.85`, `mean_prompt_chars < 12,000`, `mean_first_gold_offset < 6,000`.
