"""Completing a task with no result_memory_id must be LOUD, not silent.

A task can reach ``completed`` carrying nothing to show for it. That is the same
silent-success shape as a call that returns ok while discarding your input: the
handoff looks finished and the findings are nowhere.

Seen for real: an agent-to-agent code review reached ``completed`` with
``result_memory_id`` empty, so nothing linked the task to the review and the next
reader had to be handed the memory id out of band.

Deliberately a WARNING, not an error:
  * ``failed`` and ``cancelled`` legitimately have no result;
  * a completion whose honest outcome is "nothing was needed" has no memory to
    point at;
  * 19 of 21 already-completed tasks predate the convention, so blocking would
    be a breaking change against near-universal existing practice.
So it is surfaced where a caller actually reads -- the returned string, and the
``task_get`` line a reviewer inspects -- rather than forbidden.
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from memory.orchestration import (  # noqa: E402
    task_create_impl,
    task_delete_impl,
    task_get_impl,
    task_set_result_impl,
    task_update_impl,
)

_UUID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


@pytest.fixture
def task_id():
    created = task_create_impl(title="pytest-completion-probe", created_by="pytest")
    m = _UUID.search(created)
    assert m, f"could not parse a task id from: {created!r}"
    tid = m.group(1)
    yield tid
    try:
        task_delete_impl(tid)
    except Exception:  # noqa: BLE001 - cleanup must not mask a test failure
        pass


def test_completing_without_a_result_warns(task_id):
    task_update_impl(task_id, state="in_progress")
    out = task_update_impl(task_id, state="completed")
    assert "state=completed" in out
    assert "WARNING" in out, out
    assert "result_memory_id" in out
    assert "task_set_result" in out, "the warning must name the remedy"


def test_completing_with_a_result_is_quiet(task_id):
    task_update_impl(task_id, state="in_progress")
    task_set_result_impl(task_id, "some-memory-id")
    out = task_update_impl(task_id, state="completed")
    assert "state=completed" in out
    assert "WARNING" not in out, out


def test_task_get_flags_a_completed_task_with_no_result(task_id):
    """The reader inspecting a finished task must see it, not just the caller
    who completed it -- '(none)' alone reads as a blank field."""
    task_update_impl(task_id, state="in_progress")
    task_update_impl(task_id, state="completed")
    line = [ln for ln in task_get_impl(task_id).split("\n") if "Result Memory" in ln]
    assert line, "task_get should render a Result Memory line"
    assert "completed with no findings" in line[0], line[0]


def test_task_get_is_quiet_for_an_unfinished_task(task_id):
    """An in-progress task with no result yet is normal, not a problem."""
    task_update_impl(task_id, state="in_progress")
    line = [ln for ln in task_get_impl(task_id).split("\n") if "Result Memory" in ln][0]
    assert "(none)" in line
    assert "no findings" not in line, line


@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
def test_other_terminal_states_do_not_warn(task_id, terminal):
    """Only `completed` claims an outcome; failed/cancelled legitimately carry
    no result and must not be nagged about it."""
    task_update_impl(task_id, state="in_progress")
    out = task_update_impl(task_id, state=terminal)
    assert "WARNING" not in out, out


def test_completion_still_succeeds_despite_the_warning(task_id):
    """The warning must not become a block -- the state change has to land."""
    task_update_impl(task_id, state="in_progress")
    task_update_impl(task_id, state="completed")
    assert "State: completed" in task_get_impl(task_id)
