"""Citation-drift check (commit 4b — SKELETON, off by default).

glukhov's most serious failure mode: "a page cites a source, but the claim no
longer matches what the source says … this creates false confidence." For each
synthesis, re-read its `consolidates` / `distills_from` sources and ask whether
the compiled claim still holds. Catches the summary-creep / overclaim case that
lint-over-the-wiki-alone cannot — and it is graph-native (the sources are known
from provenance edges), which a file-based wiki cannot do.

WHAT SHIPS NOW (all deterministic, all stub-testable):
  - the plumbing: pair each synthesis with its source members via provenance
    edges, hand the pair to an injected DriftJudge, collect verdicts into a lint
    report section, dedupe, fail open.
  - OFF BY DEFAULT: like wiki.synth's synthesizer, the judge is None unless
    injected. The default build never calls it and is byte-identical to a build
    without this module.
  - REPORT ONLY: it writes a lint section. It NEVER auto-supersedes, never sets a
    verdict, never mutates a row. Remediation is a human/authorized act (§6;
    promotability.py "heuristics never auto-promote").

WHAT IS DEFERRED (must NOT be enabled by default until done — plan §5):
  - the judge PROMPT.
  - the FIXTURE of hand-authored synthesis/source pairs with known verdicts.
  - the recall THRESHOLD, derived from that fixture and recorded in the plan.

The real (model-backed) DriftJudge is non-deterministic, so — exactly like
synth.py's lede path — it is EXCLUDED from the byte-identity drift test. Only the
stub-driven plumbing is drift-tested.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

# Same provenance relations the blast-radius walk uses: a synthesis was compiled
# FROM these sources, so these are what its claim must still hold against.
_PROVENANCE_RELS = ("consolidates", "distills_from")


@dataclass
class DriftVerdict:
    """One judge decision for one synthesis. `drifted=None` means "no opinion"
    (judge unavailable / errored / abstained) — treated as fail-open (no finding),
    never as a positive drift."""
    synthesis_id: str
    drifted: Optional[bool]
    reason: str = ""


class DriftJudge(Protocol):
    """Decides whether a synthesis's claim still holds against its sources.

    Injected so the plumbing is deterministically testable with a stub and no
    model runs on the drift-tested path (mirrors wiki.synth.Synthesizer)."""

    def judge(self, synthesis_content: str, source_contents: list[str]) -> DriftVerdict:
        ...


@dataclass
class DriftReport:
    """Collected findings + coverage, for the lint section. Coverage is REPORTED
    (§3): a check that silently skipped syntheses reads as 'clean' when it merely
    did not look."""
    checked: int = 0
    skipped_no_sources: int = 0
    abstained: int = 0
    findings: list[DriftVerdict] = field(default_factory=list)

    @property
    def drifted(self) -> list[DriftVerdict]:
        return [f for f in self.findings if f.drifted is True]


def _sources_for(synthesis_id: str, edges, content_by_id: dict) -> list[str]:
    """Source contents a synthesis was compiled from (provenance edges FROM it)."""
    out = []
    for e in edges:
        if e.rel in _PROVENANCE_RELS and e.from_id == synthesis_id:
            c = content_by_id.get(e.to_id)
            if c:
                out.append(c)
    return out


def check_drift(clusters, edges, judge: "Optional[DriftJudge]") -> Optional[DriftReport]:
    """Run the citation-drift check. Returns None when no judge is injected — the
    default, so the caller emits nothing and the build stays byte-identical.

    Fail-open throughout: a judge that raises, or returns drifted=None, yields no
    positive finding — a false 'this drifted' is worse than a miss (it trains the
    reader to ignore the report), so the skeleton never manufactures one.
    """
    if judge is None:
        return None

    # Index every member's content once (no per-synthesis re-query — the members
    # are already in hand from clustering).
    content_by_id: dict = {}
    syntheses: list = []
    for c in clusters:
        for m in c.members:
            content_by_id[m.id] = m.content
            if m.type == "synthesis":
                syntheses.append(m)

    report = DriftReport()
    seen: set[str] = set()
    for m in sorted(syntheses, key=lambda x: x.id):
        if m.id in seen:
            continue
        seen.add(m.id)
        sources = _sources_for(m.id, edges, content_by_id)
        if not sources:
            report.skipped_no_sources += 1
            continue
        report.checked += 1
        try:
            v = judge.judge(m.content, sources)
        except Exception:
            report.abstained += 1  # fail open: a judge error is NOT a drift
            continue
        if v.drifted is None:
            report.abstained += 1
        report.findings.append(v)
    return report


def render_section(report: Optional[DriftReport], links, self_path: str) -> list[str]:
    """Lint-section lines for the drift report. Empty list when the check did not
    run (no judge) — the caller then adds nothing, preserving determinism.

    When it DID run, the section is always emitted (even at 0 findings) with
    coverage, so its absence never reads as 'not checked' (§3).
    """
    if report is None:
        return []
    drifted = report.drifted
    lines = [f"## Citation drift ({len(drifted)})", ""]
    lines.append(
        f"_Checked {report.checked} synthesis(es) against their sources; "
        f"{report.skipped_no_sources} had no sources, "
        f"{report.abstained} abstained (judge unavailable)._")
    lines.append("")
    if drifted:
        lines.append("> ⚠️ These syntheses may no longer match their sources — "
                     "review (this is a flag, not an automatic change):")
        lines.append("")
        for v in sorted(drifted, key=lambda x: x.synthesis_id):
            why = f" — {v.reason}" if v.reason else ""
            lines.append(f"- `{v.synthesis_id[:8]}`{why}")
        lines.append("")
    return lines
