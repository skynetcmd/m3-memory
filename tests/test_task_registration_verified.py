"""Task registration must verify the OUTCOME, not trust the exit code.

`schtasks /Create` exiting 0 is a proxy for "the task exists". Querying it back
is the outcome. They are different claims, and on 2026-09-12 this repo shipped
four defects of exactly that shape -- a check testing something *adjacent* to
the thing that mattered:

* the waiter swallowed a failed poll into an empty set, indistinguishable from
  an empty inbox
* a drift guard was blind to its own majority case while its self-test asserted
  a tautology that could not fail
* ``<WorkingDirectory>`` shipped with 21 tests asserting the SIGNATURE and none
  asserting the element was emitted
* ``systemctl --user start`` returns 0 for a unit that does not exist

Nothing is known to be failing here today -- all 11 specs query back cleanly.
This guards the class, at the cost of one extra ``schtasks`` call per task at
install time.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import install_schedules as isch  # noqa: E402

_WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="schtasks is Windows-only"
)


def test_the_success_path_verifies_rather_than_trusting_rc_zero():
    """The defect #161 describes: `if result.returncode == 0` was taken as proof
    the task existed, with nothing querying it back."""
    import ast

    with open(isch.__file__, encoding="utf-8") as fh:
        src = fh.read()
    assert "_verify_task_registered" in src, "no verification helper"

    # Parsed, not string-searched. `if result.returncode == 0:` occurs several
    # times in this module (crontab, launchctl, systemctl, schtasks), and a
    # naive .index() finds the FIRST -- which is the crontab path and has
    # nothing to do with task registration. My first version of this test did
    # exactly that and checked an unrelated branch.
    tree = ast.parse(src)
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and "windows" in n.name.lower()
         and "task" in n.name.lower()),
        None,
    )
    if fn is None:
        # Fall back to whichever function actually contains the call.
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_verify_task_registered"
                for c in ast.walk(n)
            )
        )

    # The call must sit inside a branch testing returncode == 0, not in the
    # failure path -- a verifier that only runs after schtasks already reported
    # failure adds nothing.
    on_success = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        test = ast.unparse(node.test)
        if "returncode" not in test or "== 0" not in test:
            continue
        if any(
            isinstance(c, ast.Call)
            and getattr(c.func, "id", "") == "_verify_task_registered"
            for c in ast.walk(node)
        ):
            on_success = True
            break
    assert on_success, (
        "verification does not run inside the `returncode == 0` branch -- rc 0 "
        "is still being trusted as proof the task exists"
    )


def test_verification_failure_is_loud_and_fails_the_run():
    """A task that schtasks claims to have created but which cannot be queried
    back is a broken install, not a warning to scroll past."""
    with open(isch.__file__, encoding="utf-8") as fh:
        src = fh.read()
    idx = src.index("_verify_task_registered(task[")
    window = src[idx:idx + 400]
    assert "FAIL" in window, "a failed verification must be reported as FAIL"
    assert "success = False" in window, (
        "a failed verification must fail the run; otherwise the installer still "
        "exits 0 with a task that does not exist"
    )


def test_working_directory_is_part_of_what_gets_verified():
    """Existence alone is not enough.

    A task registered without <WorkingDirectory> runs from
    C:/Windows/system32. An interpreter whose only route to `m3_memory` is the
    implicit cwd entry on sys.path is then blind -- which is precisely how the
    notification waiter detected nothing for an hour while Task Scheduler
    reported Running.
    """
    src = isch._verify_task_registered.__doc__ or ""
    assert "WorkingDirectory" in src, "the docstring does not state this is checked"
    with open(isch.__file__, encoding="utf-8") as fh:
        body = fh.read()
    idx = body.index("def _verify_task_registered")
    window = body[idx:idx + 2200]
    assert "<WorkingDirectory>" in window, (
        "verification does not check for WorkingDirectory"
    )


def test_utf16_output_is_normalised_before_matching():
    """`schtasks /Query /XML` emits UTF-16. A naive substring match against the
    decoded text can miss on the BOM/NUL bytes and report a correctly-registered
    task as missing its WorkingDirectory -- a false alarm, which §3 treats as a
    violation in its own right."""
    with open(isch.__file__, encoding="utf-8") as fh:
        body = fh.read()
    idx = body.index("def _verify_task_registered")
    window = body[idx:idx + 2200]
    assert "\\x00" in window or "replace(" in window, (
        "UTF-16 NULs are not stripped before matching"
    )


@_WINDOWS_ONLY
def test_verifier_is_quiet_on_real_tasks_and_loud_on_a_missing_one():
    """Both directions, against the live Task Scheduler.

    A guard that only proves it can stay quiet has not been shown to catch
    anything; one that only proves it can fire may be a false alarm. §12c:
    plant a violation and watch it trip.
    """
    missing = isch._verify_task_registered("AgentOS_DefinitelyNotARealTask")
    assert missing is not None, "a nonexistent task verified as present"

    # If the box has any AgentOS_* task registered, it must verify cleanly --
    # otherwise this check would fire on a healthy install.
    import subprocess

    listing = subprocess.run(
        ["schtasks", "/Query", "/FO", "LIST"], capture_output=True, text=True, timeout=60
    )
    names = [
        ln.split(":", 1)[1].strip().lstrip("\\")
        for ln in (listing.stdout or "").splitlines()
        if ln.startswith("TaskName:") and "AgentOS_" in ln
    ]
    if not names:
        pytest.skip("no AgentOS_* tasks registered on this box")
    for name in names[:3]:
        assert isch._verify_task_registered(name) is None, (
            f"{name} is registered but failed verification — this would false-alarm "
            f"on a healthy install, which §3 treats as a violation"
        )
