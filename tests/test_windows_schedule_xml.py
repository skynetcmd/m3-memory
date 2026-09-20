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


# _render_task_xml grew a required m3_memory_root when <WorkingDirectory>
# was added: the task must run FROM the payload, because an interpreter that
# resolves `m3_memory` only via cwd is blind anywhere else -- the 2026-09-12
# waiter outage, where the task ran from C:/Windows/system32 and detected
# nothing for an hour while reporting Running. A fixed fixture path keeps
# these tests about XML shape rather than about this machine.
_ROOT = r"C:\payload\m3_memory"


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
    doc = isch._render_task_xml(spec, r"C:\venv\pythonw.exe", "DOMAIN\\user", _ROOT)
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
        isch._render_task_xml(_spec("AgentOS_X", "YEARLY"), "py", "u", _ROOT)


# ── Self-heal repetition is scoped to the cognitive loop only ─────────────────

def test_cognitive_loop_selfheal_repetition_on_boot_trigger_only():
    # The loop keeps BOTH triggers (it runs as InteractiveToken, so a boot start
    # can be deferred until logon on a machine sitting at the lock screen), but
    # only ONE carries the self-heal repetition.
    #
    # This asserted BOTH until 2026-09-20, on the reasoning that a dead loop
    # should "revive from either path". That misreads what a repetition does:
    # it re-fires its OWN trigger on an unbounded timer for the whole OS
    # session, so one is already sufficient. Two gave two independent cadences,
    # and the task launched twice per interval with one launch existing purely
    # to lose the single-instance lock race -- start a process, log
    # "already running ... Exiting", return 0.
    #
    # IgnoreNew made that correct, which is why it read as harmless, but a
    # no-op still costs a process creation: a visible console flash on Windows,
    # and a task history that looks like a crash loop.
    root = _render(_spec("AgentOS_CognitiveLoop", "ONSTART"))
    boot = root.find(".//t:BootTrigger", _NS)
    interval = boot.find("./t:Repetition/t:Interval", _NS)
    assert interval is not None and interval.text == "PT30M", (
        "CognitiveLoop BootTrigger must carry the 30-min self-heal repetition"
    )
    logon = root.find(".//t:LogonTrigger", _NS)
    assert logon is not None, "CognitiveLoop must still emit a LogonTrigger"
    assert logon.find("./t:Repetition", _NS) is None, (
        "LogonTrigger must NOT repeat -- a second cadence only loses the lock "
        "race and flashes a window"
    )


def test_other_onstart_task_has_no_repetition():
    # A task NOT in _SELF_HEAL_TASKS gets no repetition (only the registered
    # long-lived singletons self-heal).
    root = _render(_spec("AgentOS_SomethingElse", "ONSTART"))
    for trig in ("BootTrigger", "LogonTrigger"):
        node = root.find(f".//t:{trig}", _NS)
        assert node.find("./t:Repetition", _NS) is None, (
            f"a task outside _SELF_HEAL_TASKS must not repeat; {trig} must not repeat"
        )


def test_embed_server_has_5min_self_heal_repetition():
    # The shared embed server is the SOLE embedder for the fleet; its death is a
    # fleet-wide outage, so it must carry a self-heal repetition on BOTH the boot
    # and logon triggers. Widened from PT1M to PT5M on 2026-07-19 — a 1-min
    # re-fire was needless process churn now that Hidden makes the re-fire an
    # invisible no-op. Safe because IgnoreNew + the server's own /health
    # pre-flight guarantee a re-fire never stacks a second GPU embedder.
    #
    # ⚠ The repetition is on the BOOT trigger only (changed 2026-09-20). The
    # comment above called the re-fire "an invisible no-op" because Hidden is
    # set; that is wrong -- the duplicate launch is still a process creation and
    # does flash. One unbounded repetition re-fires for the whole OS session,
    # so self-heal is unaffected.
    root = _render(_spec("AgentOS_EmbedServer", "ONSTART"))
    boot = root.find(".//t:BootTrigger", _NS)
    assert boot is not None, "EmbedServer must emit a BootTrigger"
    interval = boot.find("./t:Repetition/t:Interval", _NS)
    assert interval is not None and interval.text == "PT5M", (
        "EmbedServer BootTrigger must carry the 5-min self-heal repetition"
    )
    logon = root.find(".//t:LogonTrigger", _NS)
    assert logon is not None, "EmbedServer must still emit a LogonTrigger"
    assert logon.find("./t:Repetition", _NS) is None, (
        "LogonTrigger must NOT repeat -- see the CognitiveLoop test above"
    )


def test_all_tasks_are_hidden():
    # Anti-flash guarantee: every task must carry <Hidden>true</Hidden> in
    # <Settings>. pythonw.exe alone does NOT suppress the window — a non-Hidden
    # task under InteractiveToken flashes a console on every (self-heal) fire
    # (observed 2026-07-19). Regression guard so a future edit can't drop it.
    for sched in ("ONSTART", "MINUTE", "HOURLY"):
        root = _render(_spec("AgentOS_EmbedServer", sched))
        hidden = root.find(".//t:Settings/t:Hidden", _NS)
        assert hidden is not None and hidden.text == "true", (
            f"{sched} task must set <Hidden>true</Hidden> (else it flashes a window)"
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


# ── --verify mode (hermetic: mock schtasks /Query output) ─────────────────────

class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _query_xml(*, ignore_new=True, repetition="PT30M", hidden=True):
    """Minimal registered-task XML as schtasks /Query /XML ONE would emit."""
    rep = f"<Repetition><Interval>{repetition}</Interval></Repetition>" if repetition else ""
    multi = "IgnoreNew" if ignore_new else "Parallel"
    hid = "<Hidden>true</Hidden>" if hidden else ""
    return (
        f'<?xml version="1.0"?><Task xmlns="{isch._TASK_NS}">'
        f"<Triggers><BootTrigger>{rep}</BootTrigger></Triggers>"
        f"<Settings>{hid}<MultipleInstancesPolicy>{multi}</MultipleInstancesPolicy></Settings>"
        "</Task>"
    )


def test_verify_windows_pass(monkeypatch):
    # CognitiveLoop is a self-heal task: verify passes when the live XML carries
    # the PT30M repetition + IgnoreNew.
    monkeypatch.setattr(isch, "_os_name", lambda: "Windows")
    monkeypatch.setattr(
        isch.subprocess, "run",
        lambda *a, **k: _FakeProc(0, _query_xml(ignore_new=True, repetition="PT30M")),
    )
    assert isch._verify_windows_task("AgentOS_CognitiveLoop") is True


def test_verify_windows_not_hidden_fails(monkeypatch):
    # A registered task missing <Hidden>true</Hidden> must fail verify — it would
    # flash a console window on every self-heal fire (the 2026-07-19 regression).
    monkeypatch.setattr(isch, "_os_name", lambda: "Windows")
    monkeypatch.setattr(
        isch.subprocess, "run",
        lambda *a, **k: _FakeProc(0, _query_xml(ignore_new=True, repetition="PT30M", hidden=False)),
    )
    assert isch._verify_windows_task("AgentOS_CognitiveLoop") is False


def test_verify_windows_missing_repetition_fails(monkeypatch):
    monkeypatch.setattr(isch, "_os_name", lambda: "Windows")
    monkeypatch.setattr(
        isch.subprocess, "run",
        lambda *a, **k: _FakeProc(0, _query_xml(ignore_new=True, repetition="")),
    )
    assert isch._verify_windows_task("AgentOS_CognitiveLoop") is False


def test_verify_windows_not_registered_fails(monkeypatch):
    monkeypatch.setattr(isch, "_os_name", lambda: "Windows")
    monkeypatch.setattr(
        isch.subprocess, "run",
        lambda *a, **k: _FakeProc(1, "", "ERROR: The system cannot find the file specified."),
    )
    assert isch._verify_windows_task("AgentOS_CognitiveLoop") is False


def test_verify_windows_non_selfheal_task_needs_no_repetition(monkeypatch):
    # A task NOT in _SELF_HEAL_TASKS passes with IgnoreNew and no repetition.
    monkeypatch.setattr(isch, "_os_name", lambda: "Windows")
    monkeypatch.setattr(
        isch.subprocess, "run",
        lambda *a, **k: _FakeProc(0, _query_xml(ignore_new=True, repetition="")),
    )
    assert isch._verify_windows_task("AgentOS_SecretRotator") is True


def test_verify_schedules_reports_all_not_short_circuit(monkeypatch, capsys):
    # verify_schedules must check every task even after one fails.
    monkeypatch.setattr(isch, "_os_name", lambda: "Windows")

    def fake_run(cmd, *a, **k):
        # Fail only WeeklyAuditor; everything else is a clean IgnoreNew task.
        name = cmd[cmd.index("/TN") + 1] if "/TN" in cmd else ""
        if "WeeklyAuditor" in name:
            return _FakeProc(1, "", "not found")
        rep = "PT30M" if name in isch._SELF_HEAL_TASKS else ""
        return _FakeProc(0, _query_xml(ignore_new=True, repetition=rep))

    monkeypatch.setattr(isch.subprocess, "run", fake_run)
    root = os.path.dirname(_BIN)
    ok = isch.verify_schedules(None, root)
    out = capsys.readouterr().out
    assert ok is False                          # one failure -> overall False
    assert "WeeklyAuditor" in out               # the failure is reported
    assert "CognitiveLoop" in out               # AND later tasks still checked


# ──────────────────────────────────────────────────────────────────────────────
# --verify must reject a BOUNDED self-heal repetition.
#
# A <Repetition> carrying a <Duration> stops repeating after that duration, so
# the task self-heals only inside a short window after its trigger and is dead
# afterwards. The original check tested for the <Interval> substring alone and
# therefore PASSED such a task.
#
# Not hypothetical: AgentOS_Dashboard and AgentOS_CognitiveLoop were both
# registered by an older installer as
#     <Repetition><Interval>PT5M</Interval><Duration>PT10M</Duration></Repetition>
# The dashboard died on 2026-07-22 and stayed down for four days while
# `--verify` reported "[OK] self-heal Repetition PT5M present". A check that
# reports safety it is not verifying is worse than no check (DESIGN §3).
# ──────────────────────────────────────────────────────────────────────────────

_UNBOUNDED = (
    "<Task><Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
    "<Hidden>true</Hidden></Settings><Triggers><LogonTrigger>"
    "<Repetition><Interval>PT5M</Interval>"
    "<StopAtDurationEnd>false</StopAtDurationEnd></Repetition>"
    "</LogonTrigger></Triggers></Task>"
)
_BOUNDED = _UNBOUNDED.replace(
    "<StopAtDurationEnd>false</StopAtDurationEnd>",
    "<Duration>PT10M</Duration><StopAtDurationEnd>false</StopAtDurationEnd>",
)


def _run_verify(monkeypatch, xml_doc, name="AgentOS_Dashboard"):
    """Drive _verify_windows_task against a canned schtasks /XML response."""
    class _R:
        returncode = 0
        stdout = xml_doc
        stderr = ""

    monkeypatch.setattr(isch.subprocess, "run", lambda *a, **k: _R())
    return isch._verify_windows_task(name)


def test_verify_accepts_an_unbounded_self_heal_repetition(monkeypatch):
    assert _run_verify(monkeypatch, _UNBOUNDED) is True


def test_verify_rejects_a_bounded_self_heal_repetition(monkeypatch):
    """The regression. This XML has the right Interval and is still broken."""
    assert _run_verify(monkeypatch, _BOUNDED) is False


def test_verify_still_rejects_a_missing_repetition(monkeypatch):
    no_rep = _UNBOUNDED.replace(
        "<Repetition><Interval>PT5M</Interval>"
        "<StopAtDurationEnd>false</StopAtDurationEnd></Repetition>",
        "",
    )
    assert _run_verify(monkeypatch, no_rep) is False


def test_generated_xml_has_no_duration_for_self_heal_tasks():
    """The generator side of the same contract: what we WRITE must be unbounded.

    Guards the other direction — a future edit to _xml_repetition that
    reintroduces a <Duration> would recreate the stale-task bug for every new
    install, and the --verify fix above would only catch it after the fact.
    """
    specs = isch.get_schedule_specs(os.getcwd(), 8088)
    checked = 0
    for task in specs:
        if task["name"] not in isch._SELF_HEAL_TASKS:
            continue
        xml_doc = isch._render_task_xml(task, r"C:\python.exe", r"DOMAIN\user", _ROOT)
        root = ET.fromstring(xml_doc)
        for rep in root.iter():
            if rep.tag.endswith("Repetition"):
                kids = [c.tag.rsplit("}", 1)[-1] for c in rep]
                assert "Duration" not in kids, (
                    f"{task['name']}: self-heal Repetition must be UNBOUNDED; "
                    f"a <Duration> stops it repeating. Got {kids}"
                )
                checked += 1
    assert checked, "no self-heal task rendered — the guard tested nothing"


# ── WorkingDirectory: the fix for the 2026-09-12 blind-waiter outage ──────────

def test_task_xml_sets_working_directory_to_the_payload():
    """A task with no <WorkingDirectory> runs from C:/Windows/system32.

    That is not cosmetic. An interpreter whose only route to `m3_memory` is the
    implicit cwd entry on sys.path can import it from the checkout and NOWHERE
    else -- so the scheduled waiter spent an hour detecting nothing, through
    three probes, while Task Scheduler reported Running and every health check
    reported OK. Every manual test passed because humans run commands from the
    checkout; the failure reproduced only where nobody types.

    Pinning the element itself, not just the signature: the signature change was
    what the 21 broken tests noticed, and a signature can be satisfied while the
    XML omits the element entirely.
    """
    doc = isch._render_task_xml(
        _spec("AgentOS_T", "MINUTE", modifier="5"),
        r"C:\venv\pythonw.exe", "DOMAIN\\user", _ROOT,
    )
    root = ET.fromstring(doc)
    found = [e.text for e in root.iter() if e.tag.endswith("WorkingDirectory")]
    assert found, "no <WorkingDirectory> — the task will run from system32"
    assert found[0] == _ROOT, f"WorkingDirectory is {found[0]!r}, expected {_ROOT!r}"


def test_every_real_spec_gets_a_working_directory():
    """Not just the one spec above. A task registered without it is the exact
    silent failure this element exists to prevent, so no spec may be missed."""
    missing = []
    for task in isch.get_schedule_specs(os.path.dirname(_BIN)):
        doc = isch._render_task_xml(task, r"C:\venv\pythonw.exe", "DOMAIN\\user", _ROOT)
        root = ET.fromstring(doc)
        if not [e for e in root.iter() if e.tag.endswith("WorkingDirectory")]:
            missing.append(task["name"])
    assert not missing, f"specs with no WorkingDirectory: {missing}"
