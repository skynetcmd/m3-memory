"""`m3 setup` must not report success over a broken install.

_step_doctor used to `return True` in BOTH branches -- pass and fail -- and
_summary printed "Setup complete." unconditionally. So a setup that finished
with a red doctor still exited 0 under a green summary. That is the same
silent-success pattern the doctor probes exist to catch: a step reporting a
result it did not check.

Contract now:
  exit 0 -- installed AND `m3 doctor` clean
  exit 3 -- installed, verification FAILED (distinct from 2 = aborted)
"""

import subprocess

import pytest


@pytest.fixture
def wizard():
    from m3_memory import setup_wizard
    return setup_wizard


def test_step_doctor_returns_false_when_doctor_fails(wizard, monkeypatch):
    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, "doctor")
    monkeypatch.setattr(wizard, "_run", boom)
    assert wizard._step_doctor() is False, (
        "a failing doctor must be reported, not swallowed"
    )


def test_step_doctor_returns_true_when_doctor_passes(wizard, monkeypatch):
    monkeypatch.setattr(wizard, "_run", lambda *a, **k: None)
    assert wizard._step_doctor() is True


def test_summary_headline_reflects_the_verdict():
    """Build the plan from the REAL dataclass rather than a hand-rolled stub, so
    this test cannot drift as _summary grows fields."""
    import contextlib
    import io

    from m3_memory.setup_wizard import SetupPlan
    from m3_memory.wizard import summary as s

    plan = SetupPlan()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        s._summary(plan, None, verified=False)
    out = buf.getvalue()
    assert "VERIFICATION FAILED" in out, out
    assert "verified healthy" not in out, out

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        s._summary(plan, None, verified=True)
    assert "verified healthy" in buf.getvalue()


def test_summary_defaults_to_verified_for_existing_callers():
    """The new kwarg must not break a caller that does not pass it."""
    import inspect

    from m3_memory.wizard import summary as s
    sig = inspect.signature(s._summary)
    assert sig.parameters["verified"].default is True


def test_install_sh_does_not_claim_success_on_exit_3():
    """Under `set -e`, an unhandled exit 3 would abort install.sh before its
    guidance -- a bare failure with no next step."""
    import os
    import re

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(repo, "install.sh"), encoding="utf-8") as fh:
        src = fh.read()

    assert "SETUP_RC" in src, "install.sh does not capture the setup exit code"
    assert re.search(r"SETUP_RC\s*-ne\s*3", src), (
        "install.sh must special-case exit 3 (installed but unverified)"
    )
    # The unconditional success line must be gone.
    assert 'color_ok "Done. m3-memory is installed."' not in src, (
        "install.sh still claims success unconditionally after m3 setup"
    )
