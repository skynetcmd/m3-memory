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


def test_step_doctor_survives_a_non_calledprocess_failure(wizard, monkeypatch):
    """A doctor that fails to EXECUTE must not kill the wizard silently.

    Only CalledProcessError was caught, so any other failure (OSError spawning
    the child, a crash before it wrote a byte) escaped _step_doctor and exited
    the process 1 — a code the wizard never returns itself (0/2/3). On
    windows-latest, where the E2E lane has never been green, the log ended
    mid-step: "Step 5/5: verifying the install (m3 doctor)" followed straight by
    "##[error]Process completed with exit code 1" — no summary, no verdict, no
    cause. §3: fail loud, never silent.

    The install has already succeeded by this point, so the right outcome is
    "installed but unverified" (False -> exit 3) with the reason named.
    """
    def boom(*a, **k):
        raise OSError(8, "Exec format error")
    monkeypatch.setattr(wizard, "_run", boom)
    assert wizard._step_doctor() is False, (
        "a doctor that cannot run must be reported, not allowed to kill setup"
    )


def test_step_doctor_still_propagates_keyboard_interrupt(wizard, monkeypatch):
    """Ctrl-C must stay interruptible — the catch-all must not swallow it."""
    def interrupt(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(wizard, "_run", interrupt)
    import pytest as _pytest
    with _pytest.raises(KeyboardInterrupt):
        wizard._step_doctor()


# ── failure telemetry ────────────────────────────────────────────────────────

def test_telemetry_surfaces_captured_output(wizard, capsys):
    """A failing doctor's output must be recoverable even when streaming lost it.

    `_run` streams the child's stdio. On windows-latest the setup step ends with
    "Step 5/5" then `exit 1` and NOT ONE BYTE from either stream, while the same
    argv run standalone on that runner exits 0 and prints normally. Capturing
    owns the pipes, so whatever the child wrote lands somewhere we can print.
    """
    import sys as _sys
    argv = [_sys.executable, "-c",
            "import sys; print('verdict line'); print('why', file=sys.stderr); sys.exit(1)"]
    wizard._doctor_failure_telemetry(argv, 1)
    cap = capsys.readouterr()
    blob = cap.out + cap.err
    assert "verdict line" in blob
    assert "why" in blob
    assert "captured rc=1" in blob


def test_telemetry_names_the_no_output_case(wizard, capsys):
    """Silence under CAPTURE is itself the finding — say so, don't just print 0B.

    It distinguishes "the output was lost in transit" from "the child died
    before writing", which is the open question on the Windows E2E lane.
    """
    import sys as _sys
    wizard._doctor_failure_telemetry([_sys.executable, "-c", "import sys; sys.exit(1)"], 1)
    blob = "".join(capsys.readouterr())
    assert "dying before it writes" in blob


def test_telemetry_never_raises_on_a_bad_command(wizard, capsys):
    """Diagnostics must not mask the verdict they are explaining (§3)."""
    wizard._doctor_failure_telemetry(["definitely-not-a-real-binary-xyz"], 1)
    assert "could not start" in "".join(capsys.readouterr())


def test_failed_verification_still_returns_false_with_telemetry(wizard, monkeypatch):
    """Telemetry is additive: the verdict is unchanged."""
    import subprocess as _sp

    def boom(*a, **k):
        raise _sp.CalledProcessError(1, "doctor")
    monkeypatch.setattr(wizard, "_run", boom)
    monkeypatch.setattr(wizard, "_doctor_failure_telemetry", lambda *a, **k: None)
    assert wizard._step_doctor() is False


def test_verification_timeout_is_bounded_and_named(wizard, monkeypatch, capsys):
    """A hanging doctor must fail the verification, not hang the install.

    The doctor probes live endpoints (embedder, LLM, warehouse). A probe that
    BLOCKS rather than fails would stall setup forever — and on CI that burns
    the whole runner (GitHub's default job cap is 6 hours). Observed on
    windows-latest: the E2E job sat in_progress for 25+ minutes on this step
    while its ubuntu/macOS twins finish in well under 10.
    """
    import subprocess as _sp

    monkeypatch.setattr(wizard, "_DOCTOR_TIMEOUT_S", 0.5)
    monkeypatch.setattr(wizard, "_doctor_failure_telemetry", lambda *a, **k: None)

    def hang(cmd, **kw):
        raise _sp.TimeoutExpired(cmd, 0.5)
    monkeypatch.setattr(wizard, "_run", hang)

    assert wizard._step_doctor() is False, "a hung doctor must fail verification"
    blob = "".join(capsys.readouterr())
    assert "TIMED OUT" in blob
    assert "hanging, not failing" in blob


def test_run_forwards_a_timeout(wizard, monkeypatch):
    """_run must actually pass the timeout through to subprocess."""
    seen = {}

    def fake(cmd, **kw):
        seen.update(kw)
        class _CP:
            returncode = 0
        return _CP()
    monkeypatch.setattr(wizard.subprocess, "run", fake)
    wizard._run(["x"], timeout=12.5)
    assert seen.get("timeout") == 12.5


# ── post-install daemon liveness ─────────────────────────────────────────────

class _P:
    """Minimal SetupPlan stand-in."""
    def __init__(self, loop=False, dash=False):
        self.cognitive_loop = loop
        self.install_dashboard = dash


def _fake_halt(roles):
    import types
    m = types.ModuleType("m3_halt")
    m.list_live_processes = lambda *a, **k: [
        types.SimpleNamespace(role=r, pid=i) for i, r in enumerate(roles, 1)
    ]
    return m


def test_daemons_running_is_success(wizard, monkeypatch, capsys):
    monkeypatch.setattr(wizard, "_import_m3_halt",
                        lambda: _fake_halt(["cognitive-loop", "dashboard"]))
    assert wizard._step_verify_daemons(_P(loop=True, dash=True)) is True
    assert "cognitive-loop: running" in capsys.readouterr().out


def test_a_stopped_daemon_fails_verification(wizard, monkeypatch, capsys):
    """An upgrade STOPS the daemons and owns restarting them.

    Reporting success over a system whose writers are all down is the silent
    success this step exists to prevent — and it is exactly the state an upgrade
    leaves if a restart fails. The cognitive-loop/dashboard probes print
    "not running" as WARNINGS that feed no exit code, so nothing caught it.
    """
    monkeypatch.setattr(wizard, "_import_m3_halt",
                        lambda: _fake_halt(["dashboard"]))
    assert wizard._step_verify_daemons(_P(loop=True, dash=True)) is False
    blob = "".join(capsys.readouterr())
    assert "cognitive-loop: NOT running" in blob
    assert "m3 doctor --fix" in blob, "must name the repair"


def test_nothing_enabled_is_vacuously_ok(wizard, monkeypatch):
    monkeypatch.setattr(wizard, "_import_m3_halt", lambda: _fake_halt([]))
    assert wizard._step_verify_daemons(_P()) is True


def test_unreadable_registry_does_not_fail_the_install(wizard, monkeypatch, capsys):
    """Degrade to UNKNOWN, never to a false alarm (§3)."""
    monkeypatch.setattr(wizard, "_import_m3_halt", lambda: None)
    assert wizard._step_verify_daemons(_P(loop=True)) is True
    assert "UNKNOWN" in "".join(capsys.readouterr())


def test_embed_server_is_not_registry_checked(wizard, monkeypatch, capsys):
    """The embed server is a SERVICE, not a registered writer.

    It never joins the PID registry, so a registry check reports "NOT running"
    while it serves :8082 — verified live (health 200, no registry entry). Its
    liveness belongs to doctor's HTTP /health probe.
    """
    monkeypatch.setattr(wizard, "_import_m3_halt", lambda: _fake_halt([]))
    plan = _P()
    plan.use_shared_embedder = True
    assert wizard._step_verify_daemons(plan) is True
    assert "embed-server" not in "".join(capsys.readouterr())
