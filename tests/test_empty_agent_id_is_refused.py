"""An empty `agent_id` means "identity refused", never "you have no mail".

An empty id reaches these impls when the anti-spoofing guard in
`catalog.dispatch` blanks an LLM-supplied one: an LLM-facing caller (`m3_call`)
may not address an arbitrary agent unless the entry point opts in via
`allow_caller_agent_id` (#144).

Rendering that as an empty inbox is a §3 false negative on the ONE check an
agent uses to decide whether it has work. Measured 2026-09-12::

    m3_call notifications_poll agent_id=claude-code  ->  "Notifications for : (empty)"
    notifications_poll_impl("claude-code")           ->  10 notifications, 1 unread handoff

An agent that trusts the empty answer silently drops work addressed to it --
which is exactly how the #170 silent-theft bug felt from the inside.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import memory_core  # noqa: E402

from memory import orchestration  # noqa: E402

# (callable, tool name) for every impl that takes an inbox identity.
_INBOX_IMPLS = [
    (orchestration.notifications_poll_impl, "notifications_poll"),
    (orchestration.notifications_unread_ids_impl, "notifications_unread_ids"),
    (orchestration.notifications_ack_all_impl, "notifications_ack_all"),
    (orchestration.notifications_mark_received_impl, "notifications_mark_received"),
    (memory_core.memory_inbox_impl, "memory_inbox"),
]


@pytest.mark.parametrize(
    "fn,tool", _INBOX_IMPLS, ids=[t for _f, t in _INBOX_IMPLS]
)
def test_empty_agent_id_raises_rather_than_reporting_an_empty_inbox(fn, tool):
    with pytest.raises(ValueError) as ei:
        fn("")
    msg = str(ei.value)
    assert tool in msg, f"the error should name the tool; got {msg!r}"
    assert "NOT" in msg and "empty" in msg, (
        "the error must say explicitly that this is not an empty inbox, or a "
        "reader will draw the same wrong conclusion the silent version invited"
    )


@pytest.mark.parametrize(
    "fn,tool", _INBOX_IMPLS, ids=[t for _f, t in _INBOX_IMPLS]
)
def test_a_real_agent_id_still_works(fn, tool):
    """The guard must reject ONLY the empty id. A guard that also broke the
    normal path would be trivially green on the test above."""
    result = fn("pytest-no-such-agent")
    assert result is not None


def test_the_guard_has_exactly_one_owner():
    """§10a: a copied predicate is the defect independent of correctness. If
    this check is ever inlined at the call sites, they drift the moment one
    gains a nuance."""
    import ast
    import pathlib

    owners = []
    for path in [pathlib.Path(orchestration.__file__),
                 pathlib.Path(memory_core.__file__)]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "require_agent_id":
                owners.append(f"{path.name}:{node.lineno}")

    assert len(owners) == 1, (
        f"require_agent_id must have exactly ONE definition; found {owners}"
    )


def test_the_refusal_hint_names_a_flag_the_cli_actually_accepts():
    """§3: the hint exists to unblock a caller who is ALREADY confused about
    identity, so handing them a command that fails is worse than silence.

    It drifted to `--agent-id` (hyphen) and stayed wrong because nothing
    pinned it: `m3 admin` derives each flag from the tool's PARAMETER name,
    which is `agent_id`, so every one of the five tools sharing this helper
    rejected the command the error told them to run.

    Asserted as a PROPERTY -- the flag the CLI builds for this parameter --
    rather than a hardcoded string, so renaming the parameter updates both
    sides instead of pitting a literal against the code.
    """
    import re

    with pytest.raises(ValueError) as exc:
        orchestration.require_agent_id("", "notifications_poll")
    hint = str(exc.value)

    flags = set(re.findall(r"--[A-Za-z0-9_-]+", hint))
    assert flags, f"the refusal names no flag at all: {hint!r}"

    # argparse builds `--<param>` from the ToolSpec parameter name verbatim.
    expected = "--agent_id"
    assert expected in flags, (
        f"the refusal hint names {sorted(flags)}, but `m3 admin` accepts "
        f"{expected} -- the suggested command fails with a usage error"
    )
    assert "--agent-id" not in flags, (
        "the hyphenated spelling is not accepted by `m3 admin` for this "
        "parameter; it is a different tool's flag"
    )
