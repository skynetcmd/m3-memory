"""Citation-drift check (commit 4b).

glukhov's most serious failure mode: "a page cites a source, but the claim no
longer matches what the source says … this creates false confidence." For each
synthesis, re-read its `consolidates` / `distills_from` sources and ask whether
the compiled claim still holds. Catches the summary-creep / overclaim case that
lint-over-the-wiki-alone cannot — and it is graph-native (the sources are known
from provenance edges), which a file-based wiki cannot do.

PLUMBING (deterministic, stub-testable):
  - pair each synthesis with its source members via provenance edges, hand the
    pair to an injected DriftJudge, collect verdicts into a lint report section,
    dedupe, fail open.
  - OFF BY DEFAULT: like wiki.synth's synthesizer, the judge is None unless
    injected. The default build never calls it and is byte-identical to a build
    without this module.
  - REPORT ONLY: it writes a lint section. It NEVER auto-supersedes, never sets a
    verdict, never mutates a row. Remediation is a human/authorized act (§6;
    promotability.py "heuristics never auto-promote").

THE REAL JUDGE (:class:`ModelDriftJudge`, below): model-backed, opt-in behind
`m3 wiki generate --check-drift`. Mirrors wiki.synth.Synthesizer — env-config, a
local OpenAI-compatible chat endpoint, a disk cache keyed by the exact inputs,
fail-open on any error. Its output is NON-DETERMINISTIC, so — exactly like
synth.py's lede path — it is EXCLUDED from the byte-identity drift test; only the
stub-driven plumbing is drift-tested, and a `requires_llm` fixture test validates
the prompt's recall/precision against labelled pairs (tests/fixtures/citation_drift/).
"""
from __future__ import annotations

import hashlib
import json
import os
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


# ── The real, model-backed judge (opt-in; `--check-drift`) ───────────────────
# Mirrors wiki.synth.Synthesizer: env-configured local chat endpoint, disk cache
# keyed by the exact inputs, fail-open on any error. Non-deterministic, so kept
# OUT of the byte-identity drift test — validated instead by the labelled fixture
# (tests/fixtures/citation_drift/) whose recall/precision is recorded below.
#
# VALIDATED OPERATING POINT (the "recall threshold", plan §5). Measured by
# tests/test_wiki_citation_drift_judge_live.py against the committed 12-pair fixture
# (tests/fixtures/citation_drift/pairs.json). The judge is tuned to FAVOR RECALL
# (catch real drift) while keeping precision high enough that the report stays
# trustworthy — a false "this drifted" trains the reader to ignore the section
# (§3), so the gate requires BOTH: recall >= 0.80 AND precision >= 0.80. Abstentions
# (model unreachable / unparseable) are fail-open and count as neither.
#
# MEASURED (2026-07-25, local instruct model via LM Studio, across runs): recall
# consistently 1.00 (all real drifts caught); precision 0.75-0.86 — the variance is
# 1-2 borderline "faithful summary read as a mild contradiction" false alarms that
# flip run to run (LLM non-determinism at temperature 0 is small but nonzero).
# THIS IS A FIND-DRIFT TOOL, so recall is the priority (a missed drift is silent
# false confidence; a false alarm is a human-dismissed review flag). The floors
# reflect that asymmetry: recall floor HIGH (0.80 — a recall regression is the real
# failure), precision floor LOOSER (0.70 — tolerate the borderline run-to-run
# variance without going red on a healthy judge). Set below the measured band so a
# genuine prompt regression still trips them.
DRIFT_RECALL_FLOOR = float(os.environ.get("M3_WIKI_DRIFT_RECALL_FLOOR", "0.80"))
DRIFT_PRECISION_FLOOR = float(os.environ.get("M3_WIKI_DRIFT_PRECISION_FLOOR", "0.70"))

_JUDGE_PROMPT_VERSION = "2"  # v2 (2026-08-09): wording/renaming is not drift — see _JUDGE_SYSTEM

_JUDGE_SYSTEM = (
    "You audit a knowledge-base for CITATION DRIFT: a summary claim that no longer "
    "matches the sources it was built from. You are given one SYNTHESIS (a compiled "
    "claim) and the SOURCE notes it was compiled from. Decide whether the synthesis "
    "still faithfully reflects the sources.\n"
    "It HAS DRIFTED if the synthesis: states something the sources do not support; "
    "overclaims (stronger/broader than the sources); contradicts a source; or omits "
    "a correction/reversal the sources now carry.\n"
    "It has NOT drifted if every claim in the synthesis is supported by at least one "
    "source and none is contradicted — even if the synthesis is a lossy summary.\n"
    "WORDING IS NOT DRIFT. Judge the SUBSTANCE of the claim, not whether it reuses "
    "the sources' vocabulary. All of these are FAITHFUL, not drift: a paraphrase; a "
    "shortened, renamed or informal label for something the sources name differently; "
    "and a correct higher-level characterisation of what the sources describe in "
    "detail. Only call it overclaiming when the synthesis asserts something BROADER "
    "OR STRONGER THAN THE FACTS the sources establish — never merely because it "
    "generalises, compresses, or renames them.\n"
    "Judge ONLY against the sources shown; do not use outside knowledge. When the "
    "sources are ambiguous or insufficient to tell, answer \"drifted\": false "
    "(do not manufacture a finding).\n"
    'Reply with ONE line of strict JSON and nothing else: '
    '{"drifted": true|false, "reason": "<=15 words"}.'
)


@dataclass
class DriftConfig:
    """Model-endpoint config for the judge (mirrors wiki.synth.SynthConfig)."""
    url: str = "http://127.0.0.1:1234/v1/chat/completions"
    model: str = ""            # empty → let the server pick its loaded model
    api_key_service: str = "LM_API_TOKEN"
    timeout_s: float = 30.0
    temperature: float = 0.0   # judge wants determinism, not creativity
    # Sized for a one-line JSON verdict. A REASONING model would blow this in
    # its thinking channel, so the fix is to turn thinking OFF (see the
    # reasoning_effort block in _call_model), not to inflate the budget.
    max_tokens: int = 120
    cache_dir: Optional[str] = None

    @classmethod
    def from_env(cls, cache_dir: Optional[str] = None) -> "DriftConfig":
        return cls(
            url=os.environ.get("M3_WIKI_DRIFT_URL",
                               os.environ.get("M3_WIKI_SYNTH_URL", cls.url)),
            model=os.environ.get("M3_WIKI_DRIFT_MODEL",
                                 os.environ.get("M3_WIKI_SYNTH_MODEL", cls.model)),
            timeout_s=float(os.environ.get("M3_WIKI_DRIFT_TIMEOUT", cls.timeout_s)),
            cache_dir=cache_dir,
        )


def _judge_hash(synthesis_content: str, source_contents: list[str], model: str) -> str:
    """Idempotence key: identical (prompt, model, synthesis, sources) → identical
    hash, so an unchanged pair is never re-judged. Sources are sorted so ordering
    doesn't perturb the key (the verdict does not depend on source order)."""
    h = hashlib.sha256()
    h.update(_JUDGE_PROMPT_VERSION.encode())
    h.update((model or "").encode())
    h.update((synthesis_content or "").encode())
    for s in sorted(source_contents):
        h.update(b"\x00")
        h.update((s or "").encode())
    return h.hexdigest()[:16]


def _build_judge_prompt(synthesis_content: str, source_contents: list[str]) -> str:
    lines = ["SYNTHESIS (the compiled claim under audit):", synthesis_content.strip(), ""]
    lines.append(f"SOURCES it was compiled from ({len(source_contents)}):")
    for i, s in enumerate(source_contents, 1):
        body = (s or "").strip()
        body = (body[:1200] + "…") if len(body) > 1200 else body
        lines.append(f"[{i}] {body}")
    lines.append("")
    lines.append('Has the synthesis drifted from these sources? Reply with the JSON only.')
    return "\n".join(lines)


def _parse_verdict(text: str) -> "tuple[Optional[bool], str]":
    """Extract (drifted, reason) from the model's reply. Tolerant: finds the JSON
    object even if the model wrapped it in prose/markdown. Returns (None, reason)
    when nothing parseable is found — fail-open (abstain), never a false drift."""
    if not text:
        return None, "empty judge reply"
    raw = text.strip()
    # Strip a ```json fence if present.
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[4:] if raw.lower().startswith("json") else raw
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None, "no JSON object in judge reply"
    try:
        obj = json.loads(raw[start:end + 1])
    except Exception:
        return None, "unparseable JSON in judge reply"
    d = obj.get("drifted")
    if not isinstance(d, bool):
        return None, "judge JSON missing boolean 'drifted'"
    reason = str(obj.get("reason", "")).strip()[:200]
    return d, reason


class ModelDriftJudge:
    """The real DriftJudge (satisfies the :class:`DriftJudge` Protocol). Calls a
    local OpenAI-compatible chat endpoint, caches per pair on disk, and fails open
    (returns drifted=None) on ANY error — an unreachable/confused model must never
    manufacture a citation-drift finding."""

    def __init__(self, cfg: DriftConfig) -> None:
        self.cfg = cfg
        self.calls = 0
        self.cache_hits = 0
        self.abstentions = 0

    def judge(self, synthesis_content: str, source_contents: list[str]) -> DriftVerdict:
        digest = _judge_hash(synthesis_content, source_contents, self.cfg.model)
        cached = self._read_cache(digest)
        if cached is not None:
            self.cache_hits += 1
            return DriftVerdict(synthesis_id="?", drifted=cached[0], reason=cached[1])
        reply, finish_reason = self._call_model(
            _build_judge_prompt(synthesis_content, source_contents))
        self.calls += 1
        drifted, reason = _parse_verdict(reply or "")
        if drifted is None:
            self.abstentions += 1
            if not (reply or "").strip() and finish_reason == "length":
                # Distinguish "model ran out of room" from "model said nothing":
                # a bare "empty judge reply" sent readers looking at the wrong
                # thing entirely (see DriftConfig.max_tokens).
                reason = ("judge hit max_tokens before answering — raise "
                          "DriftConfig.max_tokens (reasoning models need room "
                          "for the reasoning channel plus the reply)")
        else:
            self._write_cache(digest, drifted, reason)
        return DriftVerdict(synthesis_id="?", drifted=drifted, reason=reason)

    def summary(self) -> str:
        return (f"citation-drift judge: {self.calls} judged, "
                f"{self.cache_hits} cached, {self.abstentions} abstained")

    # -- model call (mirrors wiki.synth._call_model) -------------------------
    def _call_model(self, user_prompt: str) -> "tuple[Optional[str], str]":
        try:
            import httpx  # lazy: only when --check-drift is used
        except ImportError:
            return None, ""
        headers = {"Content-Type": "application/json"}
        token = self._resolve_key(self.cfg.api_key_service)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload: dict = {
            "messages": [
                {"role": "system", "content": _JUDGE_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "stream": False,
        }
        if self.cfg.model:
            payload["model"] = self.cfg.model
        # Turn a reasoning model's thinking OFF so the verdict lands in
        # `content` instead of the reasoning channel. Without this, qwen3.5:9b
        # returned finish_reason="length" with EMPTY content on every pair, the
        # judge abstained on 100% of syntheses, and the drift section reported
        # "0 findings" while checking nothing (measured 2026-08-09). Same shared
        # seam slm_intent/m3_entities/files_memory.extract use — gated to the
        # local runtimes that honor it ("none" 400s on real OpenAI cloud).
        try:
            from llm_failover import suppresses_thinking_via_effort
            if suppresses_thinking_via_effort(self.cfg.url):
                payload["reasoning_effort"] = "none"
        except Exception:  # noqa: BLE001
            pass
        try:
            r = httpx.post(self.cfg.url, json=payload, headers=headers,
                           timeout=self.cfg.timeout_s)
            r.raise_for_status()
            choice = r.json()["choices"][0]
            return choice["message"].get("content"), (choice.get("finish_reason") or "")
        except Exception:
            return None, ""  # fail open

    @staticmethod
    def _resolve_key(service: str) -> Optional[str]:
        if not service:
            return None
        try:
            import sys
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from auth_utils import get_api_key  # type: ignore
            return get_api_key(service)
        except Exception:
            return None

    # -- disk cache (mirrors wiki.synth cache) -------------------------------
    def _cache_path(self, digest: str) -> Optional[str]:
        if not self.cfg.cache_dir:
            return None
        return os.path.join(self.cfg.cache_dir, f"drift_{digest}.json")

    def _read_cache(self, digest: str) -> "Optional[tuple[bool, str]]":
        path = self._cache_path(digest)
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                obj = json.load(f)
            d = obj.get("drifted")
            if isinstance(d, bool):
                return d, str(obj.get("reason", ""))
        except Exception:
            pass
        return None

    def _write_cache(self, digest: str, drifted: bool, reason: str) -> None:
        path = self._cache_path(digest)
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"hash": digest, "drifted": drifted, "reason": reason},
                          f, ensure_ascii=False, indent=2)
        except Exception:
            pass  # a cache write failure must never break the check
