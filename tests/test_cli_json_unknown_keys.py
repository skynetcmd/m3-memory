"""`m3 <domain> <tool> --json {...}` must reject unknown keys, not drop them.

The flag path builds tool args by iterating the spec's declared ``properties``,
so a mistyped flag is a loud argparse error. The ``--json`` path used to pass the
parsed object straight through, so a near-miss key was silently discarded and the
call still reported ``ok``.

Found the hard way: ``task_create --json '{"owner": "..."}'`` (the parameter is
``owner_agent``) created a task with no owner and returned success, so a handoff
looked complete when nothing had been assigned. DESIGN_PHILOSOPHIES section 3 --
fail loud, never silent; a call that reports success while discarding your intent
is the worst version of a silent failure.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

_CLI = [sys.executable, "-m", "m3_memory.cli"]


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(_CLI + args, capture_output=True, text=True, timeout=180)


def test_unknown_json_key_is_rejected():
    """The exact mistake that motivated this: `owner` instead of `owner_agent`."""
    r = _run([
        "tasks", "task_create",
        "--json", '{"title":"t","created_by":"c","owner":"someone"}',
        "--dry-run", "--yes",
    ])
    assert r.returncode == 2, f"expected rejection, got {r.returncode}: {r.stdout}{r.stderr}"
    err = r.stderr
    assert "unknown key" in err
    assert "'owner'" in err


def test_rejection_suggests_the_intended_key():
    """A near-miss should name the parameter the user meant."""
    r = _run([
        "tasks", "task_create",
        "--json", '{"title":"t","created_by":"c","owner":"someone"}',
        "--dry-run", "--yes",
    ])
    assert "owner_agent" in r.stderr, r.stderr


def test_rejection_lists_accepted_keys():
    """Tell the user what IS accepted; a bare rejection makes them guess."""
    r = _run([
        "tasks", "task_create",
        "--json", '{"title":"t","created_by":"c","bogus":1}',
        "--dry-run", "--yes",
    ])
    assert "accepted:" in r.stderr
    for expected in ("title", "created_by", "owner_agent"):
        assert expected in r.stderr


def test_valid_keys_still_pass():
    """The guard must not reject a correct call."""
    r = _run([
        "tasks", "task_create",
        "--json", '{"title":"t","created_by":"c","owner_agent":"a"}',
        "--dry-run", "--yes",
    ])
    assert r.returncode == 0, f"{r.stdout}{r.stderr}"
    assert '"ok": true' in r.stdout


@pytest.mark.parametrize("extra", ["database", "timeout"])
def test_cli_level_extras_are_allowed(extra):
    """`database` and `timeout` are CLI-level knobs, not tool properties, but
    the impls accept them -- they must not be rejected as unknown."""
    payload = '{"title":"t","created_by":"c","%s":%s}' % (
        extra, '"x.db"' if extra == "database" else "30",
    )
    r = _run(["tasks", "task_create", "--json", payload, "--dry-run", "--yes"])
    assert "unknown key" not in r.stderr, r.stderr


def test_invalid_json_still_reports_clearly():
    """Pre-existing behaviour must survive the change."""
    r = _run(["tasks", "task_create", "--json", "{not json", "--dry-run", "--yes"])
    assert r.returncode == 2
    assert "not valid JSON" in r.stderr


def test_non_object_json_still_rejected():
    r = _run(["tasks", "task_create", "--json", '["a","b"]', "--dry-run", "--yes"])
    assert r.returncode == 2
    assert "must be a JSON object" in r.stderr
