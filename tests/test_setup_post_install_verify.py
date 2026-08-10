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


# ── verification must not grade subsystems the run declined ──────────────────

class _Plan:
    """Minimal stand-in for the setup plan (only the field under test)."""
    def __init__(self, use_shared_embedder: bool):
        self.use_shared_embedder = use_shared_embedder


def _captured_argv(wizard, monkeypatch, plan):
    seen = []
    monkeypatch.setattr(wizard, "_run", lambda argv, **kw: seen.append(list(argv)))
    wizard._step_doctor(plan)
    return seen[-1]


def test_doctor_skips_shared_embedder_when_opted_out(wizard, monkeypatch):
    """`--no-shared-embedder` must not then fail verification for its absence.

    setup prints "Optional CPU embedder (:8082) — SKIPPED (not installed). This
    is fine", and an unfiltered doctor immediately failed the run on
    `shared-embedder: N issue(s)` — config-missing / server-down /
    keepalive-missing, every one of them the direct, intended consequence of the
    opt-out. setup contradicted itself in consecutive lines and exited 3.

    This is what made CI's E2E job red on all six runners: it installs with
    --no-shared-embedder (a long-lived :8082 service would leak state on a
    shared runner) and was then graded on that service existing. Verified
    against a live payload doctor: exit 1 without the flag, exit 0 with it.
    """
    argv = _captured_argv(wizard, monkeypatch, _Plan(False))
    assert "--skip-shared-embedder" in argv, (
        "declining shared-embedder mode must also exclude it from verification"
    )
    # ...and the probes that depend on a live embedder. embedding-cascade and
    # the file-extraction probe nested inside it both resolve a LIVE endpoint
    # and return 1 when none answers; with no embedder installed that is the
    # EXPECTED state, and both feed `exit_code = max(...)`.
    assert "--skip-cascade" in argv, (
        "declining the embedder must also exclude the probes that require one"
    )


def test_doctor_checks_shared_embedder_when_opted_in(wizard, monkeypatch):
    """The skip is scoped to the opt-out — a normal install still gets graded."""
    argv = _captured_argv(wizard, monkeypatch, _Plan(True))
    assert "--skip-shared-embedder" not in argv, (
        "shared-embedder mode is the shipped default; it must stay verified"
    )
    assert "--skip-cascade" not in argv, (
        "a normal install must still have its embedding cascade graded"
    )


def test_doctor_without_a_plan_checks_everything(wizard, monkeypatch):
    """No plan (legacy callers/tests) keeps the previous check-everything path."""
    argv = _captured_argv(wizard, monkeypatch, None)
    assert "--skip-shared-embedder" not in argv
    assert "--skip-cascade" not in argv
