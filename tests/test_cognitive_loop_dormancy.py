"""The cognitive-loop dormancy fixes: setup installs the loop by default, the
doctor surfaces a dormant loop, and the entity-work gate matches the extractor.

Three independent regressions this covers:
  1. `m3 setup` never installed the cognitive loop — the wizard shelled out
     `install-m3 --non-interactive` and only forwarded `--cognitive-loop` when a
     flag set it, and never prompted. So the derived layer stayed empty on every
     setup. Now: an interactive default-yes prompt, opt out with
     --no-cognitive-loop; non-interactive still honors the explicit flags.
  2. Nothing surfaced a dormant loop. schedule_probe only flags DANGLING jobs; a
     never-installed / not-running / zero-entity loop read as "schedules: OK".
     cognitive_loop_probe now nags on each of those (report-only).
  3. The entity-work gate (`_entity_work_sql`) hardcoded a type list that
     included message/chat_log — types m3_entities.DEFAULT_TYPES deliberately
     EXCLUDES — so the entity pass re-fired every cycle doing nothing for them.
     The gate now derives its IN-list from DEFAULT_TYPES.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


# ── 3. entity-work gate == extractor's DEFAULT_TYPES ────────────────────────

def test_entity_work_gate_matches_extractor_default_types():
    import m3_cognitive_loop as cl
    import m3_entities

    expected = ", ".join(f"'{t}'" for t in m3_entities.DEFAULT_TYPES)
    assert cl._ENTITY_WORK_TYPES_SQL == expected
    sql = cl._entity_work_sql("memory_items", "memory_item_entities")
    # The chatlog types the extractor skips must NOT be in the gate — else the
    # pass sees perpetual "work" it can never clear.
    for skipped in ("'message'", "'chat_log'", "'conversation'"):
        assert skipped not in sql
    # Durable types the extractor DOES process must be present.
    assert "'note'" in sql and "'observation'" in sql


# ── 2. doctor probe surfaces the dormant states ─────────────────────────────

def test_probe_nags_when_loop_not_installed(monkeypatch, capsys):
    from doctor import cognitive_loop_probe as p

    monkeypatch.setattr(p, "_installed_active", lambda: (False, None, "launchd"))
    monkeypatch.setattr(p, "_process_running", lambda: False)
    monkeypatch.setattr(p, "_entity_stats", lambda: (5000, 0))

    assert p.run(brief=False) == 0
    out = capsys.readouterr().out
    assert "NAG" in out and "not installed" in out
    assert "m3 setup" in out  # one-command fix is shown


def test_probe_nags_when_installed_but_down(monkeypatch, capsys):
    from doctor import cognitive_loop_probe as p

    monkeypatch.setattr(p, "_installed_active", lambda: (True, False, "systemd --user"))
    monkeypatch.setattr(p, "_process_running", lambda: False)
    monkeypatch.setattr(p, "_entity_stats", lambda: (5000, 10))

    assert p.run(brief=False) == 0
    out = capsys.readouterr().out
    assert "NAG" in out and "not running" in out


def test_probe_notes_zero_entities_when_live(monkeypatch, capsys):
    from doctor import cognitive_loop_probe as p

    monkeypatch.setattr(p, "_installed_active", lambda: (True, True, "launchd"))
    monkeypatch.setattr(p, "_process_running", lambda: True)
    monkeypatch.setattr(p, "_entity_stats", lambda: (5000, 0))

    assert p.run(brief=False) == 0
    out = capsys.readouterr().out
    assert "NOTE" in out and "extract_pending" in out  # backfill hint


def test_probe_ok_when_live_and_populated(monkeypatch, capsys):
    from doctor import cognitive_loop_probe as p

    monkeypatch.setattr(p, "_installed_active", lambda: (True, True, "launchd"))
    monkeypatch.setattr(p, "_process_running", lambda: True)
    monkeypatch.setattr(p, "_entity_stats", lambda: (5000, 4200))

    assert p.run(brief=False) == 0
    out = capsys.readouterr().out
    assert "OK" in out


def test_probe_unknown_states_do_not_cry_wolf(monkeypatch, capsys):
    """A blind spot (active=None, running=None) must NOT be reported as down."""
    from doctor import cognitive_loop_probe as p

    monkeypatch.setattr(p, "_installed_active", lambda: (True, None, "Task Scheduler"))
    monkeypatch.setattr(p, "_process_running", lambda: None)
    monkeypatch.setattr(p, "_entity_stats", lambda: (5000, 4200))

    assert p.run(brief=False) == 0
    out = capsys.readouterr().out
    assert "NAG" not in out  # unknown != down


def test_probe_brief_always_prints_one_line(monkeypatch, capsys):
    from doctor import cognitive_loop_probe as p

    monkeypatch.setattr(p, "_installed_active", lambda: (False, None, "launchd"))
    monkeypatch.setattr(p, "_process_running", lambda: None)
    monkeypatch.setattr(p, "_entity_stats", lambda: (1, 0))
    p.run(brief=True)
    assert capsys.readouterr().out.strip()  # exactly one non-empty line


# ── 1. `m3 setup` installs the loop by default ──────────────────────────────

def _plan_for(args_list):
    from m3_memory.setup_wizard import AgentTargets, _gather_plan, add_arguments
    parser = argparse.ArgumentParser()
    add_arguments(parser)
    return _gather_plan(AgentTargets(), parser.parse_args(args_list))


def test_noninteractive_honors_cognitive_loop_flags():
    assert _plan_for(["--non-interactive", "--cognitive-loop"]).cognitive_loop is True
    assert _plan_for(["--non-interactive", "--no-cognitive-loop"]).cognitive_loop is False
    # Non-interactive default stays OFF — a scripted/CI install must not silently
    # start a background LLM daemon.
    assert _plan_for(["--non-interactive"]).cognitive_loop is False


def test_interactive_prompts_default_yes(monkeypatch):
    """The interactive wizard must OFFER the loop (default yes) — the whole point
    of the fix. Simulate the prompt by forcing the yes/no helper's default."""
    import m3_memory.setup_wizard as sw

    seen = {"asked": False}
    orig = sw._ask_yes_no

    def _fake_ask(msg, default=False):
        if "cognitive-loop daemon" in msg:
            seen["asked"] = True
            return default  # accept the offered default
        return default if "dashboard" not in msg else False

    monkeypatch.setattr(sw, "_ask_yes_no", _fake_ask)
    # Neutralize the other interactive prompts so _gather_plan runs to the end.
    monkeypatch.setattr(sw, "_ask_choice", lambda *a, **k: k.get("default"))
    # Detected targets empty; no agents to probe.
    from m3_memory.setup_wizard import AgentTargets
    parser = argparse.ArgumentParser()
    sw.add_arguments(parser)
    args = parser.parse_args([])  # interactive
    try:
        plan = sw._gather_plan(AgentTargets(), args)
    finally:
        monkeypatch.setattr(sw, "_ask_yes_no", orig)
    assert seen["asked"], "wizard never offered the cognitive loop"
    assert plan.cognitive_loop is True  # default yes was accepted
