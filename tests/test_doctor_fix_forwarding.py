"""`m3 doctor --fix [--fix-hooks]` must forward the repair flags to the payload
doctor (bin/memory_doctor.py).

Regression: the `doctor` subparser CONSUMES --fix into args.fix (for the
installer-level repoint), so it never lands in args.rest. _cmd_doctor forwarded
only --verbose, so the payload doctor ran in REPORT mode — its whole repair pass
(shared-embedder restart, agent stale-payload rewrite, DB heal) silently never
fired under `m3 doctor --fix`.
"""
from __future__ import annotations

import argparse

import m3_memory.cli as cli


def _run(monkeypatch, *, fix, fix_hooks, verbose=False):
    captured = {}
    monkeypatch.setattr("m3_memory.installer.doctor", lambda **k: 0)
    monkeypatch.setattr(cli, "_resolve_bin_script", lambda name: "/fake/memory_doctor.py")
    monkeypatch.setattr(cli, "_run_bin_script",
                        lambda name, rest: captured.__setitem__("rest", list(rest)) or 0)
    ns = argparse.Namespace(fix=fix, fix_hooks=fix_hooks, verbose=verbose, rest=[])
    cli._cmd_doctor(ns)
    return captured.get("rest", [])


def test_fix_and_fix_hooks_are_forwarded(monkeypatch):
    rest = _run(monkeypatch, fix=True, fix_hooks=True)
    assert "--fix" in rest
    assert "--fix-hooks" in rest


def test_fix_alone_forwards_fix_only(monkeypatch):
    rest = _run(monkeypatch, fix=True, fix_hooks=False)
    assert "--fix" in rest
    assert "--fix-hooks" not in rest


def test_no_flags_forwards_no_repair_flags(monkeypatch):
    rest = _run(monkeypatch, fix=False, fix_hooks=False)
    assert "--fix" not in rest
    assert "--fix-hooks" not in rest
