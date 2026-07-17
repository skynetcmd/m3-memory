"""Conformance of the m3 PydanticAI adapter against a real pydantic-ai install.

Gated on ``pydantic_ai`` being importable — skips cleanly where it isn't (and it
installs on every interpreter m3 supports, including 3.14, since it has no chromadb
/pydantic-v1 dependency, unlike CrewAI). Asserts the two things that need the live
framework: (1) ``M3MemoryToolset`` IS a PydanticAI ``AbstractToolset`` (formal Tier-2
conformance), and (2) an ``Agent`` constructed with it + the loose tools accepts and
runs them via ``TestModel`` (no network, no model key).
"""

from __future__ import annotations

import os
import sys

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _p in (_REPO, os.path.join(_REPO, "bin")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

pytest.importorskip("pydantic_ai", reason="requires `pip install pydantic-ai`")


def test_toolset_is_a_pydantic_ai_abstract_toolset():
    from pydantic_ai.toolsets import AbstractToolset

    from m3_memory.integrations.pydantic_ai import M3MemoryToolset

    ts = M3MemoryToolset()
    assert isinstance(ts, AbstractToolset), "M3MemoryToolset must be a PydanticAI toolset"
    assert ts.id == "m3-memory"
    assert set(ts.tools.keys()) == {"remember", "recall", "forget"}


def test_custom_toolset_id():
    from m3_memory.integrations.pydantic_ai import M3MemoryToolset

    assert M3MemoryToolset(id="mem").id == "mem"


def test_agent_accepts_toolset_and_runs_via_testmodel():
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from m3_memory.integrations.pydantic_ai import M3Deps, M3MemoryToolset

    # TestModel drives the agent to call each available tool once — this exercises
    # tool-schema resolution (the annotation-resolution path that string-annotated
    # RunContext broke during development) end-to-end.
    agent = Agent(TestModel(), deps_type=M3Deps, toolsets=[M3MemoryToolset()])
    res = agent.run_sync("go", deps=M3Deps(user_id="alice"))
    assert res.output is not None


def test_register_m3_tools_attaches_to_agent():
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from m3_memory.integrations.pydantic_ai import M3Deps, register_m3_tools

    agent = Agent(TestModel(), deps_type=M3Deps)
    returned = register_m3_tools(agent)
    assert returned is agent  # chainable
    res = agent.run_sync("go", deps=M3Deps(user_id="alice"))
    assert res.output is not None


def test_recall_processor_is_awaitable_and_no_op_without_user_messages():
    import asyncio

    from m3_memory.integrations.pydantic_ai import m3_recall_processor

    proc = m3_recall_processor(k=3)

    # A processor invoked with a non-M3Deps ctx (or no user text) returns the
    # messages unchanged — never raises into the run.
    class _Ctx:
        deps = None

    out = asyncio.run(proc(_Ctx(), []))
    assert out == []
