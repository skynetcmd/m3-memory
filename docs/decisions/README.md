# Decisions & Architecture Decision Records

This directory is the home for **design decisions** in M3 core. It holds two kinds of file,
both first-class:

1. **ADR files** — `ADR-NNNN-kebab-title.md`. Formal Architecture Decision Records: a
   single decision, its context, options weighed, and consequences. The durable answer to
   "why is it built this way?" so future contributors don't re-litigate settled decisions.
2. **Decision / design docs** — `ALL_CAPS_NAME.md` (e.g. `MIGRATION_PLAN.md`,
   `FILE_INGESTION_PLAN.md`). Longer-form design records, plans, and specs. Kept in their
   native form (not forced into ADR sections).

**Both kinds adhere to `../DESIGN_PHILOSOPHIES.md` (the seven tenets).** Every decision —
ADR or ALL-CAPS design doc — is written and reviewed against those tenets; a doc that adds
a feature states its **pre-registered metric + threshold** (tenet §5) before implementation,
is scope-correct (§7), and EXPLAIN-validated on the hot path (§8). DESIGN_PHILOSOPHIES is
the authority and stays at `docs/DESIGN_PHILOSOPHIES.md`; this dir holds the decisions made
under it.

## When to write an ADR

Write one when a change is **architectural and not obvious**: a new core table/migration,
a new module boundary, a cross-cutting policy (scoping, concurrency, embedding tiers), a
retrieval-shape change, or any decision you'd otherwise re-explain in three PRs. Routine
bug fixes and one-feature implementations do not need an ADR — but the *decision* behind a
non-obvious feature does.

Every ADR is evaluated against `../DESIGN_PHILOSOPHIES.md` (the seven tenets). An ADR that
adds a feature must state its **pre-registered metric + threshold** (tenet §5) before
implementation.

## Convention

- **Filename:** `ADR-NNNN-kebab-title.md` (zero-padded, monotonically increasing).
- **Status:** one of `Draft` · `Accepted` · `Superseded by ADR-NNNN` · `Rejected`.
  A Draft is reviewed before implementation; code does not merge ahead of an Accepted ADR.
- **Sections (minimum):** Context · Decision · Consequences · Alternatives considered ·
  (for features) Pre-registered metric · Validation plan.
- **Supersede, don't delete.** A reversed decision becomes `Superseded by ADR-NNNN`; the
  superseding ADR links back. History is preserved (mirrors the bitemporal-audit ethos).
- **One decision per ADR**, mirroring one-feature-per-PR (tenet §2).

## Contents

Both kinds listed above live here side by side. New formal decisions should prefer the
ADR format; longer-form plans/specs may use the `ALL_CAPS_NAME.md` form. The ALL-CAPS
design docs below were migrated from the flat `docs/` tree (2026-06-11) and are the
standing home for that kind going forward — not a one-off.

### ADRs

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-0001](ADR-0001-materialized-bypass-surface.md) | Materialized bypass-surface for rank-independent recall | Draft |

### Migrated design / plan docs

| Doc | Subject |
|-----|---------|
| [FILE_INGESTION_PLAN.md](FILE_INGESTION_PLAN.md) | files.db architecture + ingestion phasing |
| [MEMORY_ENTITY_EXTRACTION_PLAN.md](MEMORY_ENTITY_EXTRACTION_PLAN.md) | entity-extraction split (Phase 6) |
| [MIGRATION_PLAN.md](MIGRATION_PLAN.md) | schema-migration plan |
| [PHASE_7_PLAN.md](PHASE_7_PLAN.md) | Phase 7 plan |

> The `bin/build_kg_variant.py` v2 design (referenced from `AGENT_INSTRUCTIONS.md` as
> `docs/decisions/kg-builder-v2.md`) should be written up here as an ADR when next
> touched — that reference predated this directory.
