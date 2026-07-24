"""Regression tests for bin/watch_pr_checks.py — the silent-success guard.

Incident 2026-07-24: a monitor reported ALL-GREEN twice while CI was still
running, because an empty `gh` query was read as success. These tests lock in
the invariant that fixed it: an empty / failed / unparseable query is UNKNOWN,
NEVER green, and the watch loop only terminates on a positive terminal signal.
"""
import os
import subprocess
import sys

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import watch_pr_checks as W  # noqa: E402

# ── _summarize: the core classifier ─────────────────────────────────────────

def test_none_is_unknown_never_green():
    """THE incident: an empty/failed query must NOT read as green."""
    assert W._summarize(None) == "UNKNOWN"


def test_all_pass_is_green():
    checks = [{"bucket": "pass"}, {"bucket": "pass"}]
    assert W._summarize(checks) == "green"


def test_any_fail_is_failed():
    checks = [{"bucket": "pass"}, {"bucket": "fail"}, {"bucket": "pass"}]
    assert W._summarize(checks) == "failed"


def test_any_pending_is_running():
    checks = [{"bucket": "pass"}, {"bucket": "pending"}]
    assert W._summarize(checks) == "running"


def test_unrecognized_bucket_is_not_green():
    """An unknown bucket must fail safe to running, never green."""
    assert W._summarize([{"bucket": "pass"}, {"bucket": "weird"}]) == "running"


# ── _query_pr: every failure mode collapses to None (UNKNOWN) ────────────────

def _fake_run(returncode=0, stdout=""):
    def _run(*a, **k):
        return subprocess.CompletedProcess(a[0] if a else [], returncode,
                                           stdout=stdout, stderr="")
    return _run


def test_query_empty_stdout_is_none(monkeypatch):
    monkeypatch.setattr(W.subprocess, "run", _fake_run(0, ""))
    assert W._query_pr("1", None) is None


def test_query_nonzero_rc_is_none(monkeypatch):
    """gh auth failure / not-a-repo → None, not a false green."""
    monkeypatch.setattr(W.subprocess, "run", _fake_run(1, "some error"))
    assert W._query_pr("1", None) is None


def test_query_non_json_is_none(monkeypatch):
    monkeypatch.setattr(W.subprocess, "run", _fake_run(0, "not json at all"))
    assert W._query_pr("1", None) is None


def test_query_empty_array_is_none(monkeypatch):
    """No checks reported yet is UNKNOWN, not green."""
    monkeypatch.setattr(W.subprocess, "run", _fake_run(0, "[]"))
    assert W._query_pr("1", None) is None


def test_query_gh_missing_is_none(monkeypatch):
    def _raise(*a, **k):
        raise OSError("gh not found")
    monkeypatch.setattr(W.subprocess, "run", _raise)
    assert W._query_pr("1", None) is None


def test_query_valid_returns_list(monkeypatch):
    monkeypatch.setattr(W.subprocess, "run",
                        _fake_run(0, '[{"name":"t","state":"SUCCESS","bucket":"pass"}]'))
    result = W._query_pr("1", None)
    assert isinstance(result, list) and result[0]["bucket"] == "pass"


# ── main loop: --once never declares terminal on UNKNOWN ─────────────────────

def test_main_once_unknown_is_not_terminal(monkeypatch, capsys):
    """The end-to-end guarantee: a PR whose query is UNKNOWN does NOT exit green.
    A single --once poll returns 2 (not-terminal), exactly as the incident
    required — silence/absence is never success."""
    monkeypatch.setattr(W, "_query_pr", lambda pr, repo: None)
    rc = W.main(["99", "--once"])
    assert rc == 2
    assert "ALL-GREEN" not in capsys.readouterr().out


def test_main_once_all_green_exits_zero(monkeypatch):
    monkeypatch.setattr(W, "_query_pr", lambda pr, repo: [{"bucket": "pass"}])
    assert W.main(["99", "--once"]) == 0


def test_main_once_any_fail_exits_one(monkeypatch):
    def q(pr, repo):
        return [{"bucket": "fail"}] if pr == "100" else [{"bucket": "pass"}]
    monkeypatch.setattr(W, "_query_pr", q)
    assert W.main(["99", "100", "--once"]) == 1


def test_main_mixed_unknown_and_green_is_not_terminal(monkeypatch):
    """If ANY watched PR is UNKNOWN, the whole run is not terminal — you can't
    declare victory while blind to one PR."""
    def q(pr, repo):
        return [{"bucket": "pass"}] if pr == "99" else None
    monkeypatch.setattr(W, "_query_pr", q)
    assert W.main(["99", "100", "--once"]) == 2
