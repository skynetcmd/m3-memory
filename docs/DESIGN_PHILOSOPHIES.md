# Design Philosophies

M3 Memory is built to a set of design tenets that gate every change — the
authority a contributor (human or agent) re-reads before calling work done, and
the checklist the pre-push hook echoes.

> **The full, canonical text is maintained privately** (it carries internal
> incident notes and post-mortems). This public page is the stable pointer the
> repo's docs and the pre-push hook link to, plus the one-line summary of each
> tenet. If you are working in this repo as an agent, treat the tenets below as
> binding; the numbered sections (§N) referenced throughout the docs map to this
> list.

## The tenets

1. **Local-first, sovereign, offline-capable.** SQLite is the only required
   store; every feature works fully offline (local embedding / LLM / retrieval,
   no telemetry). PostgreSQL is opt-in — as a primary backend for shared
   high-concurrency use, or as a warehouse sync tier. The storage seam is
   SQL/DB-API only and deliberately narrow: a new SQL backend is one
   self-contained file (see [EXTENDING.md](EXTENDING.md)); a document store does
   not fit and must not be forced into it.
2. **Modularity with shim-preserved identity.** `memory_core.py` is a shim over
   `bin/memory/` submodules; re-exports preserve object identity. One feature per
   change. Submodules never top-level-import the shim (cycle discipline).
3. **Robustness — fail loud, fail safe, never silent.** Structured returns; an
   empty result is zero/`[]`, never `None`; a missing dependency or misconfig
   raises an actionable error rather than silently degrading.
4. **Efficiency — don't waste resources.** Push work into the database (SQL, not
   Python-side aggregation); don't rebuild caches or reopen connections needlessly.
5. **Effectiveness — does it actually work?** A change must move a
   pre-registered metric. "It compiles" / "it runs" is the floor, not the bar.
6. **Hardening, security, runtime safety.** Parameterized SQL only; destructive
   operations are gated; run the security scanner on anything non-trivial.
7. **Privacy & multi-tenancy.** Scope is applied on every query at the SQL layer,
   never as a post-fetch filter; user data carries GDPR primitives.
8. **Performance — meets the budget under load.** Hot paths are EXPLAIN-validated
   and meet P50/P95/P99 budgets; regressions are caught, not discovered.
9. **GDPR / compliance hygiene.** Deletion cascades across every table that holds
   subject data; export is complete and faithful.
10. **Database hygiene (WAL discipline).** WAL mode is verified at init; no long
    locks held across slow work (e.g. an LLM call inside an open cursor).
11. **Bench / regression discipline.** Benchmarks are pre-registered and
    reproducible; bench data is never leaked to a public remote.
12. **Tool-shape discipline.** A tool's return value is an API surface for an
    agent — return structured rows, not prose.
13. **Code style & lint.** The whole tree lints and types clean; a latent error
    anywhere blocks a change.
14. **Working discipline.** Re-read §2–§8 against your change before calling it
    done. Don't push red. **If you see an error or footgun (or the potential for
    one), you own it to resolution** — fix it in scope or hand it off explicitly
    with agreement; never step silently around it.

*For the full rationale, worked examples, and incident references behind each
tenet, see the private canonical document (maintainers have access).*
