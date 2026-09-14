"""A notification must carry a return address (agent + session).

`agent_id` is the PRIMARY KEY of the agents table, so N concurrent sessions of
the SAME agent type collapse into ONE row. Measured 2026-09-13 on a live box:
5 live `mcp.*` bridges registered, `agent_list` showed a single `claude-code`
row whose last_seen was just whoever wrote most recently. A reply addressed to
the agent NAME reaches whichever sister polls first; the others never see it.

`_from` is a disambiguator so a reply can be routed — NOT a credential.
Identity in m3 is self-asserted by design (see bin/mcp_proxy.py's docstring).
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

from memory import orchestration  # noqa: E402


def _payload_of(monkeypatch, **kw) -> dict:
    """Call notify_impl with the DB stubbed, return the payload it would store."""
    captured: dict = {}

    class _Cur:
        pass

    class _DB:
        def execute(self, sql, params):
            captured["sql"] = sql
            captured["params"] = params
            return _Cur()

    class _Ctx:
        def __enter__(self):
            return _DB()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(orchestration, "_db", lambda: _Ctx())
    d = orchestration.dialect()
    monkeypatch.setattr(orchestration, "dialect", lambda: d)
    monkeypatch.setattr(d.__class__, "last_insert_id", lambda self, cur: 1,
                        raising=False)

    orchestration.notify_impl("claude-code", "handoff", **kw)
    # payload_json is the 3rd bind param: (agent_id, kind, payload_json, now)
    return json.loads(captured["params"][2])


def test_return_address_is_stamped(monkeypatch):
    p = _payload_of(monkeypatch, payload={"task": "review"},
                    from_agent="agy", from_session="sess-a3f9")
    assert p["_from"] == {"agent": "agy", "session": "sess-a3f9"}
    assert p["task"] == "review", "caller payload must survive intact"


def test_absent_when_not_supplied(monkeypatch):
    """No return address => no `_from` key at all. An empty dict would read as
    'the sender is anonymous' rather than 'this sender did not say'."""
    p = _payload_of(monkeypatch, payload={"task": "x"})
    assert "_from" not in p


def test_partial_return_address(monkeypatch):
    """Agent without session is still useful (cross-TYPE targeting was never
    ambiguous); only the supplied half is recorded."""
    p = _payload_of(monkeypatch, payload={}, from_agent="gemini-cli")
    assert p["_from"] == {"agent": "gemini-cli"}


def test_from_cannot_clobber_caller_keys(monkeypatch):
    """`_from` is namespaced so it cannot collide with a caller's own keys."""
    p = _payload_of(monkeypatch, payload={"agent": "MINE", "session": "MINE"},
                    from_agent="agy", from_session="s1")
    assert p["agent"] == "MINE" and p["session"] == "MINE"
    assert p["_from"]["agent"] == "agy"


def test_caller_payload_is_not_mutated(monkeypatch):
    """notify_impl must copy: stamping the return address into the CALLER's
    dict would leak `_from` back into their object."""
    original = {"task": "y"}
    _payload_of(monkeypatch, payload=original, from_agent="agy")
    assert original == {"task": "y"}, "caller's dict was mutated"


def test_declared_in_the_tool_catalog():
    """A parameter the impl accepts but the ToolSpec does not declare is
    silently DROPPED by execute_tool's allowed-keys filter — a declared
    capability with no reachable call path (§3)."""
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))
    from catalog import tools_admin, tools_memory  # noqa: E402

    def _props(specs, name):
        spec = next(s for s in specs if s.name == name)
        return spec.parameters["properties"]

    notify = _props(tools_admin.TOOLS, "notify")
    assert "from_agent" in notify and "from_session" in notify

    handoff = _props(tools_memory.TOOLS, "memory_handoff")
    assert "from_session" in handoff
