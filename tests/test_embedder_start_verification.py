"""Regression tests for the 2026-09-27 defect: a success line with no evidence.

`m3 embedder install` printed

    [OK] sovereign CPU embedder running on port 8082

while `m3 embedder status` said `stopped`. A stray `embed_server_inproc.py` left
running by the same install still held :8082, so the freshly registered service
could not bind and exited immediately. Because OS restart actions fire only on an
ABNORMAL exit, nothing would ever have revived it — tier-2 embedding was down
fleet-wide and the installer reported success. doctor found the contradiction
minutes later ("REGISTERED but STOPPED").

The file's existing tests (test_embedder_install_idempotency.py) taught this code
not to trust a NONZERO exit code. This is the mirror image: a ZERO exit code was
still trusted, and on launchd / systemd / SCM zero means only that the start
REQUEST was accepted, not that the daemon survived.

Two further traps fixed here, both the same root cause — a coarse substring
standing in for a precise state (SUBSTRING_HIT_IS_NOT_A_LIVE_ROW):
  * `"running" in blob` also matches "not running", so a DEAD service could be
    reported healthy — the exact inversion the probe exists to prevent.
  * matching only running/stopped read SCM's transitional START_PENDING as
    "never registered", which would re-install over a service still coming up.

All hermetic: no service manager, no socket, no sleep (§3 — a test that passes
only because a live local service is reachable fails in CI).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from m3_memory import embedder_admin as ea  # noqa: E402


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """The start poll must not cost real seconds in the suite."""
    monkeypatch.setattr(ea.time, "sleep", lambda _s: None)


@pytest.fixture()
def pair(tmp_path):
    """A plausible (binary, gguf) pair — neither is ever executed."""
    binary = tmp_path / "m3-embed-server"
    binary.write_bytes(b"\x00")
    gguf = tmp_path / "bge-m3.gguf"
    gguf.write_bytes(b"GGUF" + b"\x00" * 64)
    return binary, gguf


# ── the status vocabulary: negatives must beat positives ─────────────────────

@pytest.mark.parametrize("blob,expected", [
    ("running", True),
    ("RUNNING", True),
    ("active (running)", True),          # systemd's phrasing
    ("started", True),
    ("stopped", False),
    ("not running", False),              # the substring trap
    ("is not running", False),
    ("isn't running", False),
    ("inactive", False),                 # contains "active"
    ("activating", False),               # still coming up, not up
    ("START_PENDING", False),            # SCM transitional
    ("not installed", False),
    ("failed", False),
    ("dead", False),
    ("", False),                         # no evidence is not health
    ("wat", False),
])
def test_status_says_running_is_negation_aware(blob, expected):
    """A coarse `"running" in blob` reports a DEAD service as healthy.

    Four vocabularies reach this function (launchd, systemd, Windows SCM, and
    m3-embed-server's own wording), and this check is now load-bearing for the
    post-start verification, so a false positive here is a false success line.
    """
    assert ea._status_says_running(blob) is expected, blob


@pytest.mark.parametrize("blob,expected", [
    ("running", True),
    ("stopped", True),
    ("START_PENDING", True),     # registered, merely transitional
    ("activating", True),
    ("not installed", False),
    ("", False),
])
def test_service_reports_installed_accepts_transitional_states(
        pair, monkeypatch, blob, expected):
    """Matching only running/stopped read START_PENDING as "never registered",
    which would re-install over a service that was still coming up."""
    binary, gguf = pair
    monkeypatch.setattr(ea.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(
                            stdout=blob, stderr="", returncode=0))
    assert ea._service_reports_installed(binary, gguf) is expected


# ── the headline defect: no success line without evidence ────────────────────

def test_dead_service_is_never_reported_as_running(pair, monkeypatch, capsys):
    """THE 2026-09-27 REGRESSION. Port held, service stopped -> must NOT claim
    success, must name the real problem, must exit nonzero."""
    binary, gguf = pair
    monkeypatch.setattr(ea, "_service_reports_running", lambda *a, **k: False)
    monkeypatch.setattr(ea, "_port_in_use", lambda *a, **k: True)

    rc = ea._verify_started_or_explain(binary, gguf, start_rc=0)

    cap = capsys.readouterr()
    assert rc == ea.EXIT_REGISTERED_NOT_RUNNING
    assert "[OK]" not in cap.out, f"claimed success about a dead service: {cap.out!r}"
    assert "REGISTERED but NOT running" in cap.err
    assert "8082" in cap.err                      # names the contended port
    assert "embed_server_inproc.py" in cap.err    # names the usual culprit
    assert "m3 embedder start" in cap.err          # gives the way out


def test_live_service_is_reported_ok(pair, monkeypatch, capsys):
    """The happy path still prints the success line — and only here."""
    binary, gguf = pair
    monkeypatch.setattr(ea, "_service_reports_running", lambda *a, **k: True)

    rc = ea._verify_started_or_explain(binary, gguf, start_rc=0)

    assert rc == 0
    assert "[OK] sovereign CPU embedder running on port 8082" in capsys.readouterr().out


def test_dead_service_with_a_free_port_gets_start_advice(pair, monkeypatch, capsys):
    """Not running and the port is FREE is a different fault than a port
    conflict, so it must not get the port-conflict diagnosis."""
    binary, gguf = pair
    monkeypatch.setattr(ea, "_service_reports_running", lambda *a, **k: False)
    monkeypatch.setattr(ea, "_port_in_use", lambda *a, **k: False)

    rc = ea._verify_started_or_explain(binary, gguf, start_rc=7)

    err = capsys.readouterr().err
    assert rc == ea.EXIT_REGISTERED_NOT_RUNNING
    assert "already held by another process" not in err
    assert "`start` exited 7" in err


def test_verification_honours_a_custom_port(pair, monkeypatch, capsys):
    """M3_EMBED_SERVER_PORT is supported, so a hardcoded 8082 in the message is
    wrong advice — it sends the operator to look at the wrong socket."""
    binary, gguf = pair
    monkeypatch.setenv("M3_EMBED_SERVER_PORT", "9123")
    monkeypatch.setattr(ea, "_service_reports_running", lambda *a, **k: False)
    monkeypatch.setattr(ea, "_port_in_use", lambda *a, **k: True)

    ea._verify_started_or_explain(binary, gguf)

    err = capsys.readouterr().err
    assert "9123" in err
    assert "8082" not in err


def test_confirm_started_polls_until_the_daemon_is_up(pair, monkeypatch):
    """Startup is asynchronous on all three platforms: a single probe fired
    straight after `start` races the daemon and reads `stopped`."""
    binary, gguf = pair
    calls = {"n": 0}

    def _later(*_a, **_k):
        calls["n"] += 1
        return calls["n"] >= 3

    monkeypatch.setattr(ea, "_service_reports_running", _later)
    assert ea._confirm_started(binary, gguf) is True
    assert calls["n"] == 3


def test_confirm_started_gives_up_and_says_so(pair, monkeypatch):
    """A service that never comes up must return False, not hang the install."""
    binary, gguf = pair
    monkeypatch.setattr(ea, "_service_reports_running", lambda *a, **k: False)
    assert ea._confirm_started(binary, gguf, attempts=2) is False


# ── the command surfaces ─────────────────────────────────────────────────────

def test_cmd_start_exits_nonzero_when_the_service_dies(pair, monkeypatch, capsys):
    """`start` returning 0 means the REQUEST was accepted. cmd_start used to
    return that rc verbatim, so a daemon that died on a port conflict exited 0."""
    binary, gguf = pair
    monkeypatch.setattr(ea, "_binary_and_gguf_or_fail", lambda: (binary, gguf))
    monkeypatch.setattr(ea, "_service_reports_installed", lambda *a, **k: True)
    monkeypatch.setattr(ea, "_service_cmd", lambda *a, **k: 0)   # "signal sent"
    monkeypatch.setattr(ea, "_service_reports_running", lambda *a, **k: False)
    monkeypatch.setattr(ea, "_port_in_use", lambda *a, **k: True)

    rc = ea.cmd_start(SimpleNamespace())

    assert rc == ea.EXIT_REGISTERED_NOT_RUNNING
    assert "[OK]" not in capsys.readouterr().out


def test_cmd_install_does_not_claim_success_when_the_service_stays_down(
        pair, monkeypatch, capsys):
    """End-to-end shape of the reported defect: every subprocess step "succeeds"
    and the service is still not running."""
    binary, gguf = pair
    monkeypatch.setattr(ea, "_server_binary", lambda: binary)
    monkeypatch.setattr(ea, "_locate_gguf_or_explain", lambda: gguf)
    monkeypatch.setattr(ea, "_gguf_size_bytes", lambda _p: 437_778_496)
    monkeypatch.setattr(ea, "_service_cmd", lambda *a, **k: 0)
    monkeypatch.setattr(ea, "_service_reports_running", lambda *a, **k: False)
    monkeypatch.setattr(ea, "_port_in_use", lambda *a, **k: True)

    rc = ea.cmd_install(SimpleNamespace(concurrency=2))

    cap = capsys.readouterr()
    assert rc == ea.EXIT_REGISTERED_NOT_RUNNING
    assert "[OK] sovereign CPU embedder running" not in cap.out
    assert "REGISTERED but NOT running" in cap.err


def test_cmd_install_reports_ok_when_the_service_comes_up(pair, monkeypatch, capsys):
    """The ordinary install must stay quiet and successful — no false alarm."""
    binary, gguf = pair
    monkeypatch.setattr(ea, "_server_binary", lambda: binary)
    monkeypatch.setattr(ea, "_locate_gguf_or_explain", lambda: gguf)
    monkeypatch.setattr(ea, "_gguf_size_bytes", lambda _p: 437_778_496)
    monkeypatch.setattr(ea, "_service_cmd", lambda *a, **k: 0)
    monkeypatch.setattr(ea, "_service_reports_running", lambda *a, **k: True)

    rc = ea.cmd_install(SimpleNamespace(concurrency=2))

    assert rc == 0
    assert "[OK] sovereign CPU embedder running on port 8082" in capsys.readouterr().out


def test_already_registered_and_running_is_not_restarted(pair, monkeypatch, capsys):
    """An idempotent re-install must not bounce a healthy service — but it must
    confirm health from `status`, not from the port answering."""
    binary, gguf = pair
    monkeypatch.setattr(ea, "_server_binary", lambda: binary)
    monkeypatch.setattr(ea, "_locate_gguf_or_explain", lambda: gguf)
    monkeypatch.setattr(ea, "_gguf_size_bytes", lambda _p: 437_778_496)
    monkeypatch.setattr(ea, "_service_reports_installed", lambda *a, **k: True)
    monkeypatch.setattr(ea, "_service_binary_is_stale", lambda _b: False)
    monkeypatch.setattr(ea, "_service_reports_running", lambda *a, **k: True)
    seen: list[str] = []

    def _svc(_b, _g, sub, *extra):
        seen.append(sub)
        return 1 if sub == "install" else 0       # install: "already exists"

    monkeypatch.setattr(ea, "_service_cmd", _svc)

    rc = ea.cmd_install(SimpleNamespace(concurrency=2))

    assert rc == 0
    assert "start" not in seen, "bounced a service that was already running"
    assert "[OK]" in capsys.readouterr().out


def test_registered_but_stopped_is_started_not_assumed(pair, monkeypatch):
    """The other half of that branch: a registered-but-stopped service must be
    STARTED. Concluding "already serving" from the port probe alone is what
    shipped a dead service as success."""
    binary, gguf = pair
    monkeypatch.setattr(ea, "_server_binary", lambda: binary)
    monkeypatch.setattr(ea, "_locate_gguf_or_explain", lambda: gguf)
    monkeypatch.setattr(ea, "_gguf_size_bytes", lambda _p: 437_778_496)
    monkeypatch.setattr(ea, "_service_reports_installed", lambda *a, **k: True)
    monkeypatch.setattr(ea, "_service_binary_is_stale", lambda _b: False)
    # the port answers (a stray process), but OUR service is down
    monkeypatch.setattr(ea, "_port_in_use", lambda *a, **k: True)
    running = {"v": False}
    monkeypatch.setattr(ea, "_service_reports_running",
                        lambda *a, **k: running["v"])
    seen: list[str] = []

    def _svc(_b, _g, sub, *extra):
        seen.append(sub)
        if sub == "start":
            running["v"] = True      # the start actually works this time
        return 1 if sub == "install" else 0

    monkeypatch.setattr(ea, "_service_cmd", _svc)

    rc = ea.cmd_install(SimpleNamespace(concurrency=2))

    assert rc == 0
    assert "start" in seen, "a stopped service was assumed to be serving"


# ── operator advice must match the platform ──────────────────────────────────

@pytest.mark.parametrize("platform,expected,forbidden", [
    ("win32", "netstat", ("lsof", "ss -lptn")),
    ("darwin", "lsof", ("netstat", "ss -lptn")),
    ("linux", "ss -lptn", ("netstat -ano",)),
])
def test_port_holder_hint_is_os_correct(platform, expected, forbidden):
    """m3 supports 3 OSes and wrong advice is worse than none — the same rule
    the install/start hints already follow. `lsof` is absent on Windows and
    `netstat -ano` is a Windows spelling."""
    with mock.patch.object(sys, "platform", platform):
        hint = ea._port_holder_hint(8082)
    assert expected.lower() in hint.lower()
    for bad in forbidden:
        assert bad.lower() not in hint.lower(), f"{bad!r} leaked into {platform} advice"


def test_port_busy_notice_does_not_promise_it_is_fine(capsys, monkeypatch):
    """The old notice said an existing listener "may already be running ...
    which is idempotent", which is what made the real bind conflict look
    benign. It must state the failure mode instead."""
    monkeypatch.setattr(ea, "_port_in_use", lambda *a, **k: True)
    ea._warn_if_port_busy("install")
    out = capsys.readouterr().out
    assert "will not be able to bind" in out
    assert "m3 embedder status" in out


# ── RUNNING is not SERVING: the model loads after the process starts ─────────

def test_running_but_not_answering_is_not_reported_ok(pair, monkeypatch, capsys):
    """A service that is up but has not loaded its model yet must not get the
    success line; setup's doctor would read tier-2 offline right after it."""
    binary, gguf = pair
    monkeypatch.setattr(ea, "_service_reports_running", lambda *a, **k: True)
    monkeypatch.setattr(ea, "_wait_serving", lambda *a, **k: False)

    rc = ea._verify_started_or_explain(binary, gguf, start_rc=0)

    cap = capsys.readouterr()
    assert rc == 0                      # the service exists; doctor judges health
    assert "[OK]" not in cap.out
    assert "has not answered" in cap.err


def test_wait_serving_polls_until_health_answers(monkeypatch):
    calls = []

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _urlopen(url, timeout):
        calls.append(url)
        if len(calls) < 3:
            raise ConnectionRefusedError
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    assert ea._wait_serving_impl(8082, timeout=30) is True
    assert calls[-1] == "http://127.0.0.1:8082/health"
    assert len(calls) == 3


def test_wait_serving_gives_up_at_the_deadline(monkeypatch):
    def _refuse(*a, **k):
        raise ConnectionRefusedError
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(ea.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr("urllib.request.urlopen", _refuse)
    assert ea._wait_serving_impl(8082, timeout=30) is False
