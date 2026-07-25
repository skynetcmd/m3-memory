"""Authority policy for compiled syntheses (S6): does this page render as body?

`authority` is NOT a closed enum baked into render.py. It is a *policy*, so an
org can define its own review ladder (`peer-reviewed`, `needs-legal-review`, ...)
without forking. The default set is `{"canonical"}`; a config file at
M3_CONFIG_ROOT can widen it.

Two invariants, both load-bearing:

  * **Fail closed.** An UNKNOWN authority (a typo, an org value not in the set)
    renders behind a marker, never as authoritative body prose. A wrong value
    must degrade, never accidentally promote — mirrors registry.py's fail-loud
    allow-list reasoning, applied to the render gate.

  * **restricted OVERRIDES authority.** A GDPR-`restricted` synthesis (compiled
    from a since-erased member, pending derivability review) is non-rendering
    from the instant of erasure REGARDLESS of authority — even a `canonical`
    page stops rendering when restricted. `restricted` is a separate dimension
    (a legal-hold state), not a point on the editorial-quality axis, so it must
    not be reachable by widening the authority set.

Config (optional): M3_CONFIG_ROOT/.wiki_authority.json
    {"body_authorities": ["canonical", "peer-reviewed"]}
Malformed / absent → the default set (fail safe, and warned once).
"""
from __future__ import annotations

import json
import os

# The shipping default: only 'canonical' renders as body prose. Everything else
# (provisional, pending, draft, unknown) renders behind a marker until promoted.
_DEFAULT_BODY_AUTHORITIES = frozenset({"canonical"})

_CONFIG_BASENAME = ".wiki_authority.json"

# Cache the resolved set per config path so a batch render does one file read.
_CACHE: dict[str, frozenset[str]] = {}
_WARNED: set[str] = set()


def _config_root() -> str:
    """M3_CONFIG_ROOT, matching the engine's resolution order (env > default).

    A code-resolved config FILE, not an env var per policy (§3): headless
    launchers inherit no shell env, so the policy must live at a fixed path.
    """
    root = os.environ.get("M3_CONFIG_ROOT")
    if root:
        return root
    mem_root = os.environ.get("M3_MEMORY_ROOT")
    if mem_root:
        return os.path.join(mem_root, "config")
    return os.path.join(os.path.expanduser("~"), ".m3", "config")


def body_authorities() -> frozenset[str]:
    """The set of authority values that render as body prose. Config-overridable;
    defaults to {'canonical'}. Read once per config path, then cached."""
    path = os.path.join(_config_root(), _CONFIG_BASENAME)
    if path in _CACHE:
        return _CACHE[path]
    result = _DEFAULT_BODY_AUTHORITIES
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            vals = data.get("body_authorities")
            if isinstance(vals, list) and all(isinstance(v, str) for v in vals) and vals:
                result = frozenset(vals)
            else:
                _warn_once(path, "body_authorities must be a non-empty list of strings")
        except Exception as e:  # malformed config must not break rendering (§3)
            _warn_once(path, f"unreadable ({e!r})")
    _CACHE[path] = result
    return result


def _warn_once(path: str, why: str) -> None:
    if path in _WARNED:
        return
    _WARNED.add(path)
    import logging
    logging.getLogger("m3.wiki").warning(
        "wiki authority config %s: %s — using default %s",
        path, why, sorted(_DEFAULT_BODY_AUTHORITIES),
    )


def is_restricted(metadata: dict) -> bool:
    """True if a synthesis is under GDPR legal-hold (a member was erased).

    Checked as a top-level `restricted` flag OR nested under `review`; either is
    accepted so the erasure path can set it wherever is cheapest without the
    render gate caring.
    """
    if metadata.get("restricted"):
        return True
    review = metadata.get("review")
    return bool(isinstance(review, dict) and review.get("restricted"))


def renders_as_body(metadata: dict) -> bool:
    """The gate. True only if this synthesis may render as authoritative body
    prose. restricted always wins (returns False); otherwise the authority must
    be in the configured body set. Unknown/absent authority → False (fail closed).
    """
    if is_restricted(metadata):
        return False
    authority = metadata.get("authority")
    if not isinstance(authority, str):
        return False  # absent or malformed → not authoritative
    return authority in body_authorities()


def render_marker(metadata: dict) -> str:
    """A short, human-readable reason a synthesis is NOT rendering as body, for
    the placeholder line. Empty string when it DOES render as body."""
    if is_restricted(metadata):
        return "🔒 restricted (pending GDPR review) — content withheld"
    if renders_as_body(metadata):
        return ""
    authority = metadata.get("authority")
    if isinstance(authority, str) and authority:
        return f"🚧 {authority} — not yet reviewed; content withheld"
    return "🚧 unreviewed — content withheld"
