"""catalog.tools_slim — narrowed variants of the highest-traffic tools.

P2 of the slim-tools work. These are ADDITIVE: the plain tool keeps its full
semantics and full parameter set, and `<name>_slim` is a new tool with the same
impl and a narrowed schema. Nothing is removed, nothing is demoted.

## Why a slim variant is mostly about PARAMETERS, not prose

Measured before designing (section 12c -- the cheap measurement over the
plausible model). The candidate full specs cost ~3,213 tokens of startup surface:
~445 in descriptions and ~2,768 in PARAMETERS. `memory_supersede` alone is
~1,016. A "slim" variant that only shortened its description would have left
88% of the cost in place -- and for one candidate the measurement killed the
variant outright (see chatlog_status below).

## Why THESE parameters

Mined from real transcripts, the same source as the P0 baseline -- not guessed
from what looks important:

    memory_search      query(85) k(74), then a tail of 1-4 calls   ->  2 of 19
    memory_write       type/title/content(209) importance(203)     ->  4 of 22
    chatlog_search     query(50) k(44) since(9) until(5)           ->  4 of 12
    memory_supersede   old_id(24) content(24) title(21) imp(12)    ->  4 of 18

`type_filter` is kept on search despite only 2 measured calls: it is the one
parameter that changes the result SET rather than tuning the ranking, so an
agent that needs it has no workaround within the tool.

## Why the descriptions name the omitted parameters

Section 12a -- descriptions drive routing. Saying "advanced options available
elsewhere" leaves an agent looking for `scope` with nothing to match on, and it
concludes the capability does not exist (the section 3 silent-failure shape).
Naming `scope`, `user_id`, `as_of` etc. in the prose means a semantic search for
those terms hits THIS description, which then routes to the escape hatch. Each
description also names the full tool and how to reach it.

## Why the specs are DERIVED, not copied

Section 10a -- a copied predicate is the defect, independent of correctness. If
these were hand-written literals, a later edit to `memory_write`'s `content`
description would reach the full tool and not the slim one, and the two would
silently disagree about the same parameter. `_slim_of()` pulls each kept
property straight from the live spec, so drift is impossible by construction:
the only thing this module states is WHICH keys to keep.
"""
from __future__ import annotations

from .spec import ToolSpec
from .tools_chatlog import TOOLS as _CHATLOG_TOOLS
from .tools_memory import TOOLS as _MEMORY_TOOLS

# name -> (kept parameter names, slim description)
#
# Keep order matters: it is the order an agent reads the schema in, so the
# required/most-used parameters come first.
_SLIM: dict[str, tuple[tuple[str, ...], str]] = {
    "memory_search": (
        ("query", "k", "type_filter", "as_records"),
        "Search memory (semantic + keyword). Common parameters only. "
        "For filters (user_id, scope, as_of, variant, recency_bias, explain, "
        "routing) call memory_search via m3_call, or load the memory domain "
        "with tools_load_domain.",
    ),
    "memory_write": (
        ("type", "content", "title", "importance"),
        "Write a memory. Common parameters only; contradiction detection still "
        "runs and supersedes a conflicting memory automatically. For metadata, "
        "scope, user_id, validity windows, auto-classification or embedding "
        "control, call memory_write via m3_call, or load the memory domain "
        "with tools_load_domain.",
    ),
    "chatlog_search": (
        ("query", "k", "since", "until"),
        "Search captured chat turns (FTS5 keyword; filter-only when query is "
        "empty). Common parameters only. For host_agent, provider, model_id, "
        "agent_id or search_mode filters, call chatlog_search via m3_call, or "
        "load the chatlog domain with tools_load_domain.",
    ),
    "memory_supersede": (
        ("old_id", "content", "title", "importance"),
        "Replace an existing memory with a corrected one, preserving the audit "
        "link. Common parameters only. For scope, user_id, validity windows, "
        "variant or embedding control, call memory_supersede via m3_call, or "
        "load the memory domain with tools_load_domain.",
    ),
}

# chatlog_status is DELIBERATELY ABSENT. It was in the original five, and the
# measurement killed it: its only parameters are the universally-injected
# `database` and `timeout`, so there is nothing to narrow, and a slim variant
# has to spend MORE description text naming the escape hatch than the full tool
# spends describing itself (217 chars vs 163). The "slim" variant measured
# LARGER than the original -- 724B against 670B.
#
# Shipping it would also be a section 12a defect in its own right: two catalog
# entries for identical capability, forcing an agent to choose between them for
# no benefit. A tool whose only justification is saving tokens, that costs more
# tokens, has no reason to exist.

_SOURCES = {t.name: t for t in (*_MEMORY_TOOLS, *_CHATLOG_TOOLS)}


def _slim_of(name: str) -> ToolSpec:
    """Build `<name>_slim` from the live full spec.

    Fails LOUD on an unknown name or an unknown kept parameter (section 3): a
    tool renamed upstream, or a parameter dropped by a refactor, must break the
    import rather than silently ship a slim variant missing the parameter its
    description promises. Both are import-time errors, so they surface in the
    catalog's own test run rather than at an agent's first call.
    """
    src = _SOURCES.get(name)
    if src is None:
        raise KeyError(
            f"tools_slim: no full spec named {name!r}. It was renamed or moved; "
            f"update _SLIM rather than letting the slim variant vanish."
        )
    keep, description = _SLIM[name]
    props = (src.parameters or {}).get("properties", {})
    missing = [k for k in keep if k not in props]
    if missing:
        raise KeyError(
            f"tools_slim: {name} has no parameter(s) {missing}. The full tool's "
            f"schema changed; the slim description promises parameters that no "
            f"longer exist."
        )
    required = [k for k in (src.parameters or {}).get("required", []) if k in keep]
    dropped_required = [
        k for k in (src.parameters or {}).get("required", []) if k not in keep
    ]
    if dropped_required:
        raise KeyError(
            f"tools_slim: {name} drops REQUIRED parameter(s) {dropped_required}. "
            f"A slim variant that cannot supply a required argument is unusable; "
            f"keep them or do not slim this tool."
        )
    return ToolSpec(
        name=f"{name}_slim",
        description=description,
        parameters={
            "type": "object",
            # Derived from the live spec -- never a hand-copied literal, so an
            # edit to the full tool's parameter description reaches both.
            "properties": {k: props[k] for k in keep},
            "required": required,
        },
        # THE SAME impl object. One owner, two call sites: there is no second
        # code path to keep in sync, and a fix to the impl reaches both tools.
        impl=src.impl,
        is_async=src.is_async,
        validators=src.validators,
        default_allowed=src.default_allowed,
        inject_agent_id=src.inject_agent_id,
    )


TOOLS: list[ToolSpec] = [_slim_of(name) for name in _SLIM]
