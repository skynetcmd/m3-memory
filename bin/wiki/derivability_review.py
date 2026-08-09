"""GDPR derivability review queue — the follow-on to wiki.erasure.

wiki.erasure marks a synthesis `restricted` when a source member is erased
(Art. 17), which halts its rendering immediately and RETAINS the row/edges/history
for a later derivability review. This module IS that review: it turns the set of
restricted syntheses into an actionable queue and answers, per page, the two
questions a reviewer needs:

  1. DERIVABILITY (graph-native, deterministic). The erased members' rows are gone
     and their `consolidates`/`distills_from` edges cascade-deleted, but edges to
     SURVIVING sources remain. A restricted synthesis that STILL has surviving
     provenance sources is a candidate to UNRESTRICT — the erased member may have
     been redundant, so the prose may be fully derivable from what remains. One
     with ZERO surviving sources is orphaned by the erasure and must stay
     restricted (or be superseded by a clean recompile).

  2. THE 30-DAY SLA. A GDPR review obligation must not sit unattended. Each
     restricted synthesis carries an `erased_at` (wiki.erasure.erasure_records);
     age past REVIEW_SLA_DAYS (30) is flagged OVERDUE so it surfaces first.

Report-only, like every wiki lint check (§6): it NEVER clears `restricted`, never
mutates a row, never deletes. The unrestrict decision is a human/authorized act.
It surfaces the signal; a reviewer (optionally aided by the injected
DerivabilityJudge below) makes the call.

The OPTIONAL judge (`DerivabilityJudge`, injected — off by default, mirrors
wiki.citation_drift): reads the synthesis + its SURVIVING sources and opines
whether the prose is still fully supported without the erased content. Non-
deterministic → excluded from the byte-identity drift test; the deterministic
has-surviving-sources + age signal is always computed and is the queue's floor.

Backend-agnostic: operates on the (clusters, edges) already in hand at render
time — no direct sqlite3/psycopg, no new query.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Protocol

from llm_failover import apply_thinking_suppression

from . import authority as _authority

# Provenance relations: a synthesis was compiled FROM these — the same set
# wiki.citation_drift and the erasure hook use.
_PROVENANCE_RELS = ("consolidates", "distills_from")

# GDPR review SLA: a restricted synthesis older than this (since its most recent
# erasure) is OVERDUE for a derivability decision. Env-overridable for operators
# with a stricter policy; never longer without a documented rationale.
REVIEW_SLA_DAYS = 30


@dataclass
class DerivabilityVerdict:
    """One optional-judge opinion. `derivable=None` = abstain (judge unavailable /
    errored) — fail-open, never forces a decision either way."""
    synthesis_id: str
    derivable: Optional[bool]
    reason: str = ""


class DerivabilityJudge(Protocol):
    """Decides whether a restricted synthesis's claim is still fully supported by
    its SURVIVING sources (the erased ones removed). Injected so the queue is
    deterministically testable with a stub and no model runs on the drift-tested
    path (mirrors wiki.citation_drift.DriftJudge)."""

    def judge(self, synthesis_content: str, surviving_source_contents: list[str]) -> DerivabilityVerdict:
        ...


@dataclass
class ReviewItem:
    """One restricted synthesis in the queue, with its derivability signal."""
    synthesis_id: str
    title: str
    rank_key: tuple
    surviving_sources: int          # live provenance sources still present
    erasure_count: int              # how many erasures have touched this page
    erased_at: Optional[str]        # most recent erasure timestamp (ISO), or None
    age_days: Optional[float]       # days since erased_at (None if unknown)
    overdue: bool                   # age_days > REVIEW_SLA_DAYS
    # derivable_candidate: the DETERMINISTIC floor — has ≥1 surviving source, so the
    # prose MIGHT be fully derivable without the erased content. Not a decision.
    derivable_candidate: bool
    # Optional judge opinion (None when no judge injected or it abstained).
    judged_derivable: Optional[bool] = None
    judge_reason: str = ""


@dataclass
class ReviewReport:
    """The queue + coverage counts for the lint section."""
    items: list[ReviewItem] = field(default_factory=list)
    judged: int = 0
    abstained: int = 0

    @property
    def overdue(self) -> list[ReviewItem]:
        return [i for i in self.items if i.overdue]

    @property
    def orphaned(self) -> list[ReviewItem]:
        """Restricted with NO surviving sources — must stay restricted/supersede."""
        return [i for i in self.items if i.surviving_sources == 0]

    @property
    def unrestrict_candidates(self) -> list[ReviewItem]:
        """Have surviving sources → the prose may be derivable without the erased
        member. If a judge ran, prefer its opinion; else the deterministic floor."""
        out = []
        for i in self.items:
            if not i.derivable_candidate:
                continue
            if i.judged_derivable is False:
                continue  # judge overrode the floor: not derivable after all
            out.append(i)
        return out


def _newest_erased_at(metadata: dict) -> Optional[str]:
    """Most recent erasure timestamp from the erasure_records trail, or None."""
    review = metadata.get("review")
    if not isinstance(review, dict):
        return None
    recs = review.get("erasure_records")
    if not isinstance(recs, list) or not recs:
        return None
    stamps = [r.get("erased_at") for r in recs if isinstance(r, dict) and r.get("erased_at")]
    return max(stamps) if stamps else None


def _age_days(erased_at: Optional[str], now: Optional[datetime]) -> Optional[float]:
    """Days between `erased_at` (ISO-8601) and `now`. None if unparseable."""
    if not erased_at:
        return None
    try:
        ts = datetime.fromisoformat(erased_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = (now or datetime.now(timezone.utc)) - ts
    return delta.total_seconds() / 86400.0


def _surviving_source_ids(synthesis_id: str, edges, live_ids: set) -> list[str]:
    """Provenance sources of `synthesis_id` whose row is still present (survived
    the erasure). An edge to an erased member is gone (cascade), so a surviving
    edge whose target is in `live_ids` is a surviving source."""
    out = []
    for e in edges:
        if e.rel in _PROVENANCE_RELS and e.from_id == synthesis_id and e.to_id in live_ids:
            out.append(e.to_id)
    return out


def build_queue(clusters, edges, *, judge: "Optional[DerivabilityJudge]" = None,
                now: "Optional[datetime]" = None) -> ReviewReport:
    """Build the GDPR derivability review queue from the in-hand (clusters, edges).

    Deterministic floor always computed (surviving-source count + 30-day age). If
    a `judge` is injected, each restricted synthesis WITH surviving sources is also
    put to the judge (there is nothing for a judge to derive from when zero sources
    survive). Fail-open: a judge error/abstain leaves the deterministic signal
    intact and never forces a decision.

    `now` is injectable for deterministic age tests.
    """
    # Index every live member's id + content once.
    content_by_id: dict = {}
    live_ids: set = set()
    restricted_syn: list = []
    for c in clusters:
        for m in c.members:
            content_by_id[m.id] = m.content
            live_ids.add(m.id)
            if m.type == "synthesis" and _authority.is_restricted(m.metadata):
                restricted_syn.append(m)

    report = ReviewReport()
    seen: set = set()
    for m in sorted(restricted_syn, key=lambda x: x.rank_key()):
        if m.id in seen:
            continue
        seen.add(m.id)
        surviving = _surviving_source_ids(m.id, edges, live_ids)
        erased_at = _newest_erased_at(m.metadata)
        age = _age_days(erased_at, now)
        recs = (m.metadata.get("review") or {}).get("erasure_records") or []
        item = ReviewItem(
            synthesis_id=m.id,
            title=m.display_title,
            rank_key=m.rank_key(),
            surviving_sources=len(surviving),
            erasure_count=len(recs),
            erased_at=erased_at,
            age_days=age,
            overdue=(age is not None and age > REVIEW_SLA_DAYS),
            derivable_candidate=len(surviving) > 0,
        )
        # Optional judge: only meaningful when there ARE surviving sources to
        # derive from. Zero survivors → orphaned, nothing to judge.
        if judge is not None and surviving:
            try:
                v = judge.judge(m.content, [content_by_id[s] for s in surviving])
            except Exception:
                v = None  # fail open — a judge crash is not a decision
            if v is not None and v.derivable is not None:
                item.judged_derivable = v.derivable
                item.judge_reason = v.reason
                report.judged += 1
            else:
                report.abstained += 1  # crash OR abstain OR None-verdict → one count
        report.items.append(item)
    return report


def render_section(report: "Optional[ReviewReport]") -> list[str]:
    """Lint-section lines for the GDPR derivability queue. Always emitted when a
    report is provided (even at 0 items) so its absence never reads as 'not
    reviewed'. `report is None` → [] (check did not run)."""
    if report is None:
        return []
    items = report.items
    overdue = report.overdue
    lines = [f"## GDPR derivability review ({len(items)})", ""]
    if not items:
        lines.append("_No restricted syntheses; nothing awaiting a derivability review._")
        lines.append("")
        return lines
    orphaned = report.orphaned
    candidates = report.unrestrict_candidates
    summary = (f"_{len(items)} restricted synthesis(es) awaiting review — "
               f"{len(overdue)} OVERDUE (>{REVIEW_SLA_DAYS}d), "
               f"{len(candidates)} may be derivable from surviving sources "
               f"(unrestrict candidate), {len(orphaned)} orphaned (no surviving "
               f"source — keep restricted or supersede).")
    if report.judged:
        summary += f" Judge ran on {report.judged}, abstained on {report.abstained}."
    summary += "_"
    lines.append(summary)
    lines.append("")
    lines.append("> Report-only: `restricted` is never cleared automatically. A "
                 "reviewer decides whether the prose survives without the erased "
                 "source (unrestrict) or must be superseded.")
    lines.append("")
    for i in sorted(items, key=lambda x: (not x.overdue, x.rank_key)):
        age = f"{i.age_days:.0f}d" if i.age_days is not None else "age?"
        flag = " ⏰ OVERDUE" if i.overdue else ""
        if i.surviving_sources == 0:
            state = "orphaned — keep restricted / supersede"
        elif i.judged_derivable is True:
            state = f"judge: derivable ({i.surviving_sources} surviving src)"
        elif i.judged_derivable is False:
            state = f"judge: NOT derivable ({i.surviving_sources} surviving src)"
        else:
            state = f"unrestrict candidate ({i.surviving_sources} surviving src)"
        why = f" — {i.judge_reason}" if i.judge_reason else ""
        lines.append(f"- {i.title} · `id:{i.synthesis_id[:8]}` · {age}{flag} · "
                     f"{state}{why}")
    lines.append("")
    return lines


# ── The real, model-backed derivability judge (opt-in) ───────────────────────
# Mirrors wiki.citation_drift.ModelDriftJudge and REUSES its DriftConfig (same
# local chat endpoint / token / cache-dir contract) so operators configure one
# endpoint. Non-deterministic → excluded from the byte-identity drift test; the
# deterministic surviving-source signal is the always-computed floor. Fail-open on
# ANY error (derivable=None → abstain), so a confused/unreachable model never
# forces an unrestrict decision on GDPR-held content.
_DERIV_PROMPT_VERSION = "1"

_DERIV_SYSTEM = (
    "You audit whether a knowledge-base SUMMARY is still fully supported after one "
    "of its sources was ERASED for privacy (GDPR Art. 17). You are given the "
    "SYNTHESIS (a compiled claim) and only its SURVIVING sources (the erased one is "
    "gone). Decide whether EVERY claim in the synthesis is still supported by the "
    "surviving sources alone.\n"
    "Answer \"derivable\": true only if nothing in the synthesis depends on the "
    "erased content — i.e. the surviving sources fully support it (the erased source "
    "was redundant). Answer false if any claim now lacks support (the synthesis "
    "leaned on the erased source). When unsure, answer false (fail safe: keep the "
    "page restricted rather than wrongly unrestrict privacy-erased content).\n"
    "Judge ONLY against the surviving sources shown; do not use outside knowledge.\n"
    'Reply with ONE line of strict JSON and nothing else: '
    '{"derivable": true|false, "reason": "<=15 words"}.'
)


def _build_deriv_prompt(synthesis_content: str, surviving: list[str]) -> str:
    lines = ["SYNTHESIS (the restricted compiled claim):", synthesis_content.strip(), ""]
    lines.append(f"SURVIVING SOURCES (the erased one is gone) ({len(surviving)}):")
    for i, s in enumerate(surviving, 1):
        body = (s or "").strip()
        body = (body[:1200] + "…") if len(body) > 1200 else body
        lines.append(f"[{i}] {body}")
    lines.append("")
    lines.append("Is the synthesis still fully derivable from these surviving "
                 "sources alone? Reply with the JSON only.")
    return "\n".join(lines)


class ModelDerivabilityJudge:
    """The real DerivabilityJudge (satisfies the Protocol). Calls a local chat
    endpoint, caches per pair on disk, fails open (derivable=None) on ANY error."""

    def __init__(self, cfg=None) -> None:
        # Reuse citation_drift's DriftConfig for the endpoint (single config).
        from .citation_drift import DriftConfig
        self.cfg = cfg or DriftConfig.from_env()
        self.calls = 0
        self.cache_hits = 0
        self.abstentions = 0

    def judge(self, synthesis_content: str, surviving_source_contents: list[str]) -> DerivabilityVerdict:
        from .citation_drift import _judge_hash
        digest = _judge_hash(synthesis_content,
                             surviving_source_contents + [_DERIV_PROMPT_VERSION],
                             self.cfg.model)
        cached = self._read_cache(digest)
        if cached is not None:
            self.cache_hits += 1
            return DerivabilityVerdict(synthesis_id="?", derivable=cached[0], reason=cached[1])
        reply = self._call_model(_build_deriv_prompt(synthesis_content, surviving_source_contents))
        self.calls += 1
        # _parse_verdict extracts a boolean under key 'drifted'; our key is
        # 'derivable', so parse the JSON directly with the same tolerance.
        derivable, reason = _parse_bool(reply or "", "derivable")
        if derivable is None:
            self.abstentions += 1
        else:
            self._write_cache(digest, derivable, reason)
        return DerivabilityVerdict(synthesis_id="?", derivable=derivable, reason=reason)

    def summary(self) -> str:
        return (f"derivability judge: {self.calls} judged, "
                f"{self.cache_hits} cached, {self.abstentions} abstained")

    def _call_model(self, user_prompt: str) -> "Optional[str]":
        try:
            import httpx
        except ImportError:
            return None
        headers = {"Content-Type": "application/json"}
        token = self._resolve_key(self.cfg.api_key_service)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload: dict = {
            "messages": [{"role": "system", "content": _DERIV_SYSTEM},
                         {"role": "user", "content": user_prompt}],
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "stream": False,
        }
        if self.cfg.model:
            payload["model"] = self.cfg.model
        # Without this a reasoning model returns finish_reason="length" with an
        # EMPTY content, and the review abstains on every item while reporting
        # a clean result (the citation-drift failure, same shape).
        apply_thinking_suppression(payload, self.cfg.url)
        try:
            r = httpx.post(self.cfg.url, json=payload, headers=headers,
                           timeout=self.cfg.timeout_s)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception:
            return None

    @staticmethod
    def _resolve_key(service: str) -> "Optional[str]":
        if not service:
            return None
        try:
            import sys
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from auth_utils import get_api_key  # type: ignore
            return get_api_key(service)
        except Exception:
            return None

    def _cache_path(self, digest: str) -> "Optional[str]":
        if not self.cfg.cache_dir:
            return None
        return os.path.join(self.cfg.cache_dir, f"deriv_{digest}.json")

    def _read_cache(self, digest: str) -> "Optional[tuple[bool, str]]":
        path = self._cache_path(digest)
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                obj = json.load(f)
            d = obj.get("derivable")
            if isinstance(d, bool):
                return d, str(obj.get("reason", ""))
        except Exception:
            pass
        return None

    def _write_cache(self, digest: str, derivable: bool, reason: str) -> None:
        path = self._cache_path(digest)
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"hash": digest, "derivable": derivable, "reason": reason},
                          f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def _parse_bool(text: str, key: str) -> "tuple[Optional[bool], str]":
    """Extract (bool, reason) for `key` from a model reply, tolerant of prose /
    ```json fences. Returns (None, reason) when nothing parseable is found."""
    if not text:
        return None, "empty judge reply"
    raw = text.strip()
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
    val = obj.get(key)
    if not isinstance(val, bool):
        return None, f"judge JSON missing boolean '{key}'"
    return val, str(obj.get("reason", "")).strip()[:200]
