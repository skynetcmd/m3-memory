"""Cluster admission gate — decides whether a formed cluster earns a *synthesis*.

This is the clustering-layer sibling of the KAS score. KAS (anchor.py) *measures*
how well a topic is anchored; the admission gate *acts* on that measurement,
demoting a weakly-anchored multi-member cluster to the orphan list instead of
rendering it as an authoritative synthesis page. It changes the ARTIFACT (what
gets written), not just the number — so a low-quality page never ships, rather
than shipping with a low score attached.

Design mirrors `anchor.KASWeights` deliberately: a frozen, user-tunable tuple with
`from_tuple`, a module default, and a config file at
`M3_CONFIG_ROOT/.wiki_admission.json`. An adopter recalibrates without editing
code, exactly as with the KAS weights.

The rule (composite, so it catches BOTH weakness modes seen on the real corpus):

    a cluster RENDERS as a synthesis  iff
        kas >= min_kas                              # strong enough by score alone
        OR (provenance >= min_provenance            # else must clear BOTH
            AND backbone_ratio >= min_backbone_ratio)   structural floors

Rationale for the OR-of-(score, structural-floors) shape:
  * `min_kas` is the fast path — a well-anchored cluster is admitted outright.
  * The structural floors catch the two failure modes a single rule misses:
      - co-mention grab-bags: low backbone_ratio (synthetic edges dominate).
      - thin-provenance mega-pages: real but sparse `related` edges, low
        provenance share. These score just under `min_kas` yet have no
        `consolidates` backbone — the floors demote them.
  * Requiring BOTH floors (not either) protects legitimately-large topics that
    have strong provenance but a naturally lower ratio, and vice-versa.

Defaults (min_provenance=0.5, min_backbone_ratio=0.6, min_kas=0.5) were swept on
the live 3,337-memory corpus at thresholds 0.5/0.55/0.6: they demote exactly the
co-mention/thin-provenance pages (0 false positives on provenance-backed
clusters) and bring the 0.55 default to 0-low-anchor parity with 0.6. The rule
generalizes across all three thresholds — evidence it is a real structural signal,
not an overfit to one page.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

# ── edge classification: what the gate is ALLOWED to read ────────────────────
#
# The gate is STATE-INDEPENDENT (compile-invariant): its decision must not depend
# on anything a prior compile wrote. `consolidates`/`distills_from` are
# synthesis→member edges the COMPILE produces, so they are EXCLUDED from every gate
# signal — reading them would couple the admission decision to compile state (the
# root of the cold-start deadlock). The gate reads only user-authored member↔member
# structure, which is identical on the first compile and the thousandth.
_CO_MENTION_REL = "co_mentions"
# Compile-produced provenance — never read by the gate (KAS reporting still uses it).
_COMPILE_RELS = frozenset({"consolidates", "distills_from"})
# User-authored member↔member lineage that EXISTS before any compile. These are the
# provenance the gate counts — a real supersede/extends/refines chain among members.
_AUTHORED_PROVENANCE_RELS = frozenset({"supersedes", "extends", "refines", "supports"})


# ── the tunable gate tuple (mirrors anchor.KASWeights) ───────────────────────


@dataclass(frozen=True)
class AdmissionGate:
    """Tunable floors for the cluster admission decision. Every field overridable
    via M3_CONFIG_ROOT/.wiki_admission.json or the `from_tuple` constructor.

    Set `enabled=False` to render every multi-member cluster (the pre-gate
    behaviour) — useful for auditing what the gate would drop.
    """
    min_provenance: float = 0.5
    min_backbone_ratio: float = 0.6
    min_kas: float = 0.5
    enabled: bool = True

    @classmethod
    def from_tuple(cls, t: tuple) -> "AdmissionGate":
        """(min_provenance, min_backbone_ratio, min_kas) positionally. `enabled`
        keeps its default (True)."""
        names = ("min_provenance", "min_backbone_ratio", "min_kas")
        return cls(**{n: t[i] for i, n in enumerate(names) if i < len(t)})

    def admits(self, cluster, edges) -> bool:
        """True if this cluster earns a synthesis, judged ONLY on state-independent
        structure (see structural_signals). A singleton always passes (orphan-ness
        is decided upstream in cluster.py); the gate only judges multi-member ones.

        Rule (OR of independent admit-paths):
            kas            >= min_kas             — strong overall structural score
            backbone_ratio >= min_backbone_ratio  — well-connected by REAL edges
            provenance     >= min_provenance      — strong authored lineage

        Any ONE path admits. `backbone_ratio` is the primary discriminator: on the
        real corpus the distribution is bimodal — co-mention grab-bags sit at ~0.0,
        genuinely-linked topics at ~1.0 — so the ratio floor cleanly separates them
        even when a topic is linked by plain `related` edges (no authored lineage,
        so provenance 0 and kas just under the bar). Provenance and kas are extra
        admit-paths, not co-required floors: demanding provenance AND ratio would
        wrongly demote a ratio=1.0 topic that simply has no supersede/extends chain."""
        if not self.enabled or len(cluster.members) < 2:
            return True
        s = structural_signals(cluster, edges)
        return (s.kas >= self.min_kas
                or s.backbone_ratio >= self.min_backbone_ratio
                or s.provenance >= self.min_provenance)


_DEFAULT_GATE = AdmissionGate()


@dataclass(frozen=True)
class StructuralSignals:
    """The gate's state-independent view of a cluster. Every field derives ONLY
    from user-authored member↔member edges — no compile-produced `consolidates`,
    no synthesis members — so it is identical across compiles."""
    provenance: float       # share of real member-member edges that are authored lineage
    backbone_ratio: float   # real vs (real + co-mention) member-member edges
    kas: float              # structural KAS: weighted sum of the above + backbone
    load_bearing: int
    comention: int


def structural_signals(cluster, edges, weights=None) -> StructuralSignals:
    """Score a cluster on state-independent structure alone.

    Excludes, so the result never depends on what a compile wrote:
      * synthesis members (a `consolidates` edge pulls the synthesis into the
        cluster; we drop synthesis-typed members before scoring),
      * `consolidates`/`distills_from` edges (compile-produced, _COMPILE_RELS).

    Counts as PROVENANCE only user-authored lineage among members
    (_AUTHORED_PROVENANCE_RELS: supersedes/extends/refines/supports) — these exist
    before any compile. backbone_ratio is real-vs-co-mention over the same
    synthesis-free edge set. The structural KAS reuses anchor's weighted-sum kernel
    so the gate and the report speak the same units."""
    from . import anchor as _anchor
    from .select import EDGE_WEIGHTS
    ew = weights or EDGE_WEIGHTS
    kw = _anchor.KASWeights()

    # Synthesis-free member set: a consolidates edge can drag the synthesis into
    # the cluster; exclude it so neither it nor its edges skew the structure.
    members = [m for m in cluster.members if getattr(m, "type", None) != "synthesis"]
    member_ids = {m.id for m in members}
    n = max(1, len(members))

    backbone_strength = 0.0
    load_bearing = 0
    comention = 0
    authored = 0
    for e in edges:
        if e.from_id not in member_ids or e.to_id not in member_ids:
            continue
        if e.rel == _CO_MENTION_REL:
            comention += 1
            continue
        if e.rel in _COMPILE_RELS:
            continue  # compile-produced — invisible to the gate
        load_bearing += 1
        if e.rel in _AUTHORED_PROVENANCE_RELS:
            authored += 1
        backbone_strength += ew.get(e.rel, 1.0)

    total = load_bearing + comention
    factors = {
        "backbone": _anchor._saturate(backbone_strength / n, kw.backbone_half),
        "provenance": (authored / load_bearing) if load_bearing else 0.0,
        "confidence": kw.null_confidence,  # confidence is member-level, not structural
        "backbone_ratio": (load_bearing / total) if total else 0.0,
    }
    kas = _anchor.weighted_score(factors, kw.as_weights())
    return StructuralSignals(
        provenance=factors["provenance"], backbone_ratio=factors["backbone_ratio"],
        kas=round(kas, 4), load_bearing=load_bearing, comention=comention)


_CONFIG_BASENAME = ".wiki_admission.json"
_CACHE: "dict[str, AdmissionGate]" = {}
_WARNED: "set[str]" = set()


def _config_root() -> str:
    """M3_CONFIG_ROOT, matching the engine's resolution order (env > default).
    A code-resolved config FILE, not an env var (§3): headless launchers inherit
    no shell env, so the policy must live at a fixed path."""
    root = os.environ.get("M3_CONFIG_ROOT")
    if root:
        return root
    mem_root = os.environ.get("M3_MEMORY_ROOT")
    if mem_root:
        return os.path.join(mem_root, "config")
    return os.path.join(os.path.expanduser("~"), ".m3", "config")


def load_gate() -> AdmissionGate:
    """The active admission gate. Config-overridable; defaults to the reference
    calibration. Read once per config path, then cached. Any config problem falls
    back to the default and warns once — a bad config never breaks generation."""
    path = os.path.join(_config_root(), _CONFIG_BASENAME)
    if path in _CACHE:
        return _CACHE[path]
    gate = _DEFAULT_GATE
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            fields = {}
            for k in ("min_provenance", "min_backbone_ratio", "min_kas"):
                if k in data:
                    v = data[k]
                    if not isinstance(v, (int, float)) or not (0.0 <= v <= 1.0):
                        _warn_once(path, f"{k} must be a number in [0,1]")
                        raise ValueError
                    fields[k] = float(v)
            if "enabled" in data:
                if not isinstance(data["enabled"], bool):
                    _warn_once(path, "enabled must be a boolean")
                    raise ValueError
                fields["enabled"] = data["enabled"]
            gate = AdmissionGate(**{**_DEFAULT_GATE.__dict__, **fields})
        except Exception as e:  # malformed config must not break generation (§3)
            if not isinstance(e, ValueError):  # ValueError already warned above
                _warn_once(path, f"unreadable ({e!r})")
            gate = _DEFAULT_GATE
    _CACHE[path] = gate
    return gate


def _warn_once(path: str, why: str) -> None:
    if path in _WARNED:
        return
    _WARNED.add(path)
    import logging
    logging.getLogger("m3.wiki").warning(
        "wiki admission config %s: %s — using default %r", path, why, _DEFAULT_GATE
    )


def partition(clusters, edges, gate: "AdmissionGate | None" = None
              ) -> "tuple[list, list]":
    """Split clusters into (admitted, demoted) by the gate. Singletons and
    structurally-anchored clusters land in `admitted`; weakly-anchored multi-member
    clusters in `demoted` (they render as an orphan list, not a synthesis)."""
    gate = gate or load_gate()
    admitted: list = []
    demoted: list = []
    for c in clusters:
        (admitted if gate.admits(c, edges) else demoted).append(c)
    return admitted, demoted
