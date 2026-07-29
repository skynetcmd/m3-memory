"""chatlog_status must report hook wiring from the SAME source doctor uses.

Regression for the confusing disagreement where `m3 doctor` said "chatlog hooks:
OK" while `chatlog_status` reported every hooks[*].wired == False. The new
`hook_wiring` block is sourced from doctor's environment_probe.check(), so the two
can never disagree.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import chatlog_status as cs  # noqa: E402
from doctor import environment_probe  # noqa: E402


def test_hook_wiring_mirrors_environment_probe(monkeypatch):
    sentinel = {"status": "ok", "hooks_seen": 2,
                "findings": [{"kind": "x"}], "extra": "ignored"}
    monkeypatch.setattr(environment_probe, "check", lambda: sentinel)

    v = cs._hook_wiring_verdict()
    assert v == {"status": "ok", "hooks_seen": 2, "findings": [{"kind": "x"}]}


def test_hook_wiring_reports_issues_status(monkeypatch):
    monkeypatch.setattr(environment_probe, "check",
                        lambda: {"status": "issues", "hooks_seen": 1,
                                 "findings": [{"kind": "dead_path"}]})
    v = cs._hook_wiring_verdict()
    assert v["status"] == "issues" and v["hooks_seen"] == 1
    assert v["findings"] == [{"kind": "dead_path"}]


def test_hook_wiring_is_best_effort_on_probe_failure(monkeypatch):
    def boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(environment_probe, "check", boom)
    v = cs._hook_wiring_verdict()
    # never raises; degrades to unknown with the error surfaced
    assert v["status"] == "unknown" and v["hooks_seen"] == 0
    assert "probe exploded" in v.get("error", "")
