"""Hermetic tests for the Windows Task Scheduler XML rendering.

Guards the rewrite where install_windows_tasks stopped shelling out to
schtasks CLI flags + a PowerShell post-hardening step and instead registers
each task from a full Task Scheduler XML definition (schtasks /Create /XML).
The XML is what carries MultipleInstances=IgnoreNew, ExecutionTimeLimit, and —
for the long-lived cognitive loop — a self-heal Repetition that revives a dead
loop within 30 minutes instead of waiting for the next boot.

These tests exercise the pure render functions only: no schtasks, no
PowerShell, no live Task Scheduler. That keeps them hermetic so they pass in
CI (Linux, no Windows Task Scheduler) — see DESIGN_PHILOSOPHIES §3.
"""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET

import pytest

_BIN = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import install_schedules as isch  # noqa: E402

# Task Scheduler namespace — every element is namespaced, so ElementTree finds
# need the {ns}tag form. Bundle it into a helper.
_NS = {"t": isch._TASK_NS}


def _spec(name, schedule, modifier="", time="00:00", args=None, desc="d"):
    return {
        "name": name,
        "schedule": schedule,
        "modifier": modifier,
        "time": time,
        "args": args or [r"C:\bin\x.py", "--log-file", r"C:\logs\x.log"],
        "description": desc,
    }


def _render(spec):
    """Render + parse; returns the root Element (asserts well-formedness)."""
    doc = isch._render_task_xml(spec, r"C:\venv\pythonw.exe", "DOMAIN\\user")
    return ET.fromstring(doc)


# ── Every real spec renders well-formed XML ───────────────────────────────────

def test_all_real_specs_render_valid_xml():
    specs = isch.get_schedule_specs(os.path.dirname(_BIN))
    assert specs, "no schedule specs returned"
    for s in specs:
        root = _render(s)  # raises ParseError if malformed
        assert root.tag == f"{{{isch._TASK_NS}}}Task"


# ── Trigger mapping per schedule type ─────────────────────────────────────────

def test_minute_schedule_is_timetrigger_with_repetition():
    root = _render(_spec("AgentOS_X", "MINUTE", modifier="15"))
    trig = root.find(".//t:Triggers/t:TimeTrigger", _NS)
    assert trig is not None
    interval = trig.find("./t:Repetition/t:Interval", _NS)
    assert interval is not None and interval.text == "PT15M"


def test_hourly_schedule_repetition_interval():
    root = _render(_spec("AgentOS_X", "HOURLY", modifier="1"))
    interval = root.find(".//t:TimeTrigger/t:Repetition/t:Interval", _NS)
    assert interval is not None and interval.text == "PT1H"


def test_daily_schedule_is_calendar_by_day():
    root = _render(_spec("AgentOS_X", "DAILY", time="03:00"))
    assert root.find(".//t:CalendarTrigger/t:ScheduleByDay", _NS) is not None
    sb = root.find(".//t:CalendarTrigger/t:StartBoundary", _NS)
    assert sb is not None and sb.text.endswith("T03:00:00")


def test_weekly_schedule_maps_day_token():
    root = _render(_spec("AgentOS_X", "WEEKLY", modifier="FRI", time="16:00"))
    dow = root.find(".//t:ScheduleByWeek/t:DaysOfWeek", _NS)
    assert dow is not None
    assert dow.find("./t:Friday", _NS) is not None


def test_weekly_unknown_day_defaults_to_sunday():
    root = _render(_spec("AgentOS_X", "WEEKLY", modifier="???"))
    dow = root.find(".//t:ScheduleByWeek/t:DaysOfWeek", _NS)
    assert dow.find("./t:Sunday", _NS) is not None


def test_monthly_schedule_day_of_month():
    root = _render(_spec("AgentOS_X", "MONTHLY", modifier="1", time="02:00"))
    day = root.find(".//t:ScheduleByMonth/t:DaysOfMonth/t:Day", _NS)
    assert day is not None and day.text == "1"


def test_onstart_schedule_is_boottrigger():
    root = _render(_spec("AgentOS_Plain", "ONSTART"))
    assert root.find(".//t:Triggers/t:BootTrigger", _NS) is not None


def test_onstart_has_both_boot_and_logon_triggers():
    # The original schtasks-era task had BOTH; emitting only BootTrigger would
    # regress boot-before-logon start under InteractiveToken. Guard both.
    root = _render(_spec("AgentOS_Plain", "ONSTART"))
    assert root.find(".//t:Triggers/t:BootTrigger", _NS) is not None
    assert root.find(".//t:Triggers/t:LogonTrigger", _NS) is not None


def test_unsupported_schedule_raises():
    with pytest.raises(ValueError):
        isch._render_task_xml(_spec("AgentOS_X", "YEARLY"), "py", "u")


# ── Self-heal repetition is scoped to the cognitive loop only ─────────────────

def test_cognitive_loop_selfheal_repetition_on_both_triggers():
    # The loop can come up at boot OR logon (whichever is first); both triggers
    # must carry the 30-min self-heal repetition so a dead loop revives from
    # either path.
    root = _render(_spec("AgentOS_CognitiveLoop", "ONSTART"))
    for trig in ("BootTrigger", "LogonTrigger"):
        node = root.find(f".//t:{trig}", _NS)
        interval = node.find("./t:Repetition/t:Interval", _NS)
        assert interval is not None and interval.text == "PT30M", (
            f"CognitiveLoop {trig} must carry the 30-min self-heal repetition"
        )


def test_other_onstart_task_has_no_repetition():
    root = _render(_spec("AgentOS_SomethingElse", "ONSTART"))
    for trig in ("BootTrigger", "LogonTrigger"):
        node = root.find(f".//t:{trig}", _NS)
        assert node.find("./t:Repetition", _NS) is None, (
            f"only the cognitive loop should self-heal; {trig} must not repeat"
        )


def test_selfheal_registry_matches_real_loop_name():
    # Guards a rename drift: the self-heal task name must exist in the real specs.
    names = {s["name"] for s in isch.get_schedule_specs(os.path.dirname(_BIN))}
    for heal_name in isch._SELF_HEAL_TASKS:
        assert heal_name in names, f"{heal_name} not among real specs"


# ── Hardening settings present on every task ──────────────────────────────────

def test_settings_ignore_new_and_least_privilege():
    root = _render(_spec("AgentOS_X", "MINUTE", modifier="5"))
    pol = root.find(".//t:Settings/t:MultipleInstancesPolicy", _NS)
    assert pol is not None and pol.text == "IgnoreNew"
    rl = root.find(".//t:Principals/t:Principal/t:RunLevel", _NS)
    assert rl is not None and rl.text == "LeastPrivilege"


def test_continuous_loop_has_no_execution_time_limit():
    root = _render(_spec("AgentOS_CognitiveLoop", "ONSTART"))
    lim = root.find(".//t:Settings/t:ExecutionTimeLimit", _NS)
    # PT0S == no limit; the continuous loop must never be killed mid-flight.
    assert lim is not None and lim.text == "PT0S"


def test_finite_task_has_one_hour_time_limit():
    root = _render(_spec("AgentOS_X", "DAILY"))
    lim = root.find(".//t:Settings/t:ExecutionTimeLimit", _NS)
    assert lim is not None and lim.text == "PT1H"


# ── Action / argument construction ────────────────────────────────────────────

def test_command_and_quoted_arguments():
    spec = _spec("AgentOS_X", "MINUTE", modifier="5",
                 args=[r"C:\bin\job.py", "--flag", r"C:\path with space\out.log"])
    root = _render(spec)
    cmd = root.find(".//t:Actions/t:Exec/t:Command", _NS)
    assert cmd is not None and cmd.text == r"C:\venv\pythonw.exe"
    argu = root.find(".//t:Actions/t:Exec/t:Arguments", _NS)
    # Each argv element is individually double-quoted so spaced paths survive.
    assert argu is not None
    assert '"C:\\path with space\\out.log"' in argu.text


def test_description_is_xml_escaped():
    root = _render(_spec("AgentOS_X", "MINUTE", modifier="5",
                         desc="a & b <c> \"d\""))
    # ElementTree round-trips the entities, so a successful parse + exact text
    # match proves the raw string was escaped (an unescaped & would ParseError).
    desc = root.find(".//t:RegistrationInfo/t:Description", _NS)
    assert desc is not None and desc.text == 'a & b <c> "d"'


def test_all_five_xml_metacharacters_escaped():
    # The local escaper must cover & < > " ' — a raw one would break the parse.
    root = _render(_spec("AgentOS_X", "MINUTE", modifier="5",
                         desc="""& < > " '"""))
    desc = root.find(".//t:RegistrationInfo/t:Description", _NS)
    assert desc is not None and desc.text == """& < > " '"""
