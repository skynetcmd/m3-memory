"""`inject_agent_id` must block spoofing WITHOUT breaking the human CLI.

The flag forces an authenticated caller's identity into `agent_id` so an LLM
cannot read or ack another agent's rows. But it used to overwrite
unconditionally, and every in-repo caller passes ``agent_id=""`` -- so for tools
where `agent_id` is the RECIPIENT rather than the caller it forced the empty
string and broke them outright: ``m3 admin notifications_poll --agent_id X``
returned ``Notifications for : (empty)`` while the rows plainly existed. That is
why agent-to-agent handoffs had to be relayed by hand.

The obvious one-line fix -- keep the caller's arg whenever `agent_id` is falsy --
is NOT safe: the m3_call dispatcher (LLM-facing) also passes "", so it would let
an LLM poll anyone's notifications. The decision therefore belongs to the CALLER:
trusted non-LLM entry points pass ``allow_caller_agent_id=True``; m3_call never
does. Both halves are pinned here, because a fix that only proves the happy path
would silently re-open the spoof.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest

_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from catalog.dispatch import execute_tool_structured  # noqa: E402
from mcp_tool_catalog import TOOLS  # noqa: E402

_POLL = [s for s in TOOLS if s.name == "notifications_poll"]


@pytest.fixture(scope="module")
def poll_spec():
    if not _POLL:
        pytest.skip("notifications_poll not in the catalog")
    return _POLL[0]


def _call(spec, args, agent_id, *, allow=False):
    return asyncio.run(
        execute_tool_structured(spec, args, agent_id=agent_id,
                                allow_caller_agent_id=allow)
    )


def test_injecting_tool_is_still_marked_as_such(poll_spec):
    """Guard the premise: if inject_agent_id were turned off, the tests below
    would pass for the wrong reason."""
    assert poll_spec.inject_agent_id is True
    assert "agent_id" in (poll_spec.parameters.get("properties") or {})


def test_llm_path_cannot_spoof_another_agent(poll_spec):
    """m3_call passes agent_id="" and does NOT opt in, so a caller-supplied
    agent_id must be discarded -- otherwise an LLM reads anyone's inbox."""
    out = str(_call(poll_spec, {"agent_id": "someone-else", "unread_only": False},
                    agent_id=""))
    assert "someone-else" not in out, (
        "LLM path leaked a caller-supplied agent_id -- spoofing is possible"
    )


def test_authenticated_caller_identity_still_wins(poll_spec):
    """A real caller identity overrides whatever the args claim, even when the
    entry point opted in."""
    out = str(_call(poll_spec, {"agent_id": "someone-else", "unread_only": False},
                    agent_id="real-caller", allow=True))
    assert "someone-else" not in out
    assert "real-caller" in out


def test_cli_path_preserves_an_explicit_agent_id(poll_spec):
    """The human CLI opts in, so `--agent_id X` survives and the tool works."""
    out = str(_call(poll_spec, {"agent_id": "antigravity-agent", "unread_only": False},
                    agent_id="", allow=True))
    assert "Notifications for antigravity-agent" in out, out


def test_opt_in_defaults_to_off():
    """A caller that does not think about this must get the SAFE behaviour."""
    import inspect

    sig = inspect.signature(execute_tool_structured)
    assert sig.parameters["allow_caller_agent_id"].default is False


def test_m3_call_dispatcher_does_not_opt_in():
    """The LLM-facing dispatcher must never pass allow_caller_agent_id=True."""
    src = (_BIN / "catalog" / "dispatch.py").read_text(encoding="utf-8")
    after = src.split("async def dispatch", 1)
    body = after[1] if len(after) > 1 else src
    assert "allow_caller_agent_id=True" not in body, (
        "the m3_call dispatcher opted in -- that re-opens agent spoofing"
    )
