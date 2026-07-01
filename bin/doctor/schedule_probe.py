"""Dangling scheduled-task probe — do the AgentOS_* tasks still point at a
real interpreter and script?

The Windows ``AgentOS_*`` scheduled tasks (see bin/install_schedules.py) bake an
ABSOLUTE interpreter path (``…\\.venv\\Scripts\\pythonw.exe``) and script path
into their Task Scheduler XML. If the code install is deleted or moved — e.g.
``rmdir ~/pipx`` wipes the venv, or the repo clone is relocated — the tasks stay
registered but their interpreter/script no longer exists. They fire on schedule
and fail silently: ``LastTaskResult`` goes non-zero and the background governor
(``AgentOS_CognitiveLoop``) never resurrects, because self-heal can restart a
crashed loop but not a deleted interpreter.

Nothing surfaces this today: ``installer._path_is_stale`` only covers MCP-config
paths, and ``governor_probe`` only flags *legacy* (pre-governor) schedules, not
*dangling* ones.

This probe makes the state visible (DESIGN §3 fail-loud): if a registered task
points at a missing interpreter or script, it nags and prints the one-command
fix. It is report-only — a broken task is a recoverable state, not a doctor
failure — so it always returns 0 and never crashes the doctor run.

Windows-only: on macOS/Linux the cognitive loop is a launchd/systemd service and
the periodic tasks are crontab entries; dangling-detection for those is a
separate future check. Here we print a one-line "n/a" and return 0.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

logger = logging.getLogger("memory.doctor.schedule_probe")

# Task Scheduler XML namespace (all AgentOS_* tasks are registered under it).
_TASK_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"

# schtasks /Query returns this rc when the named task does not exist. A
# not-installed task is NOT dangling (that's "never set up" — a different,
# silent state), so we distinguish it from a real query failure.
_SCHTASKS_NOT_FOUND_RC = 1


def _expected_task_names(m3_memory_root: str) -> list[str]:
    """The AgentOS_* task names this install would register. Sourced from
    install_schedules.get_schedule_specs so the names stay single-sourced."""
    bin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    import install_schedules as sched  # noqa: E402

    return [t["name"] for t in sched.get_schedule_specs(m3_memory_root)]


def _query_task_xml(name: str) -> str | None:
    """Return the Task Scheduler XML for ``name``, or None if the task is not
    installed. Raises on an unexpected schtasks failure (caught by run())."""
    proc = subprocess.run(
        ["schtasks", "/Query", "/TN", name, "/XML"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        # Task-not-found is the common, benign case; anything else is a real
        # failure we let bubble so run() can report "could not query".
        stderr = (proc.stderr or "").upper()
        if proc.returncode == _SCHTASKS_NOT_FOUND_RC or "ERROR:" in stderr:
            return None
        raise RuntimeError(f"schtasks /Query {name} failed (rc={proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def _parse_action_paths(task_xml: str) -> tuple[str | None, str | None]:
    """Extract (interpreter_path, script_path) from a task's XML.

    interpreter = <Command> of the first <Exec> action.
    script      = first <Arguments> token (quotes stripped). None if absent.
    """
    root = ET.fromstring(task_xml)
    exec_el = root.find(f".//{_TASK_NS}Actions/{_TASK_NS}Exec")
    if exec_el is None:
        return None, None
    cmd_el = exec_el.find(f"{_TASK_NS}Command")
    # <Command> may be quoted (schtasks registers some tasks with a quoted
    # interpreter path). Strip surrounding quotes so os.path.exists() sees the
    # real path — an unstripped '"…pythonw.exe"' is a false "missing" positive.
    interpreter = cmd_el.text.strip().strip('"') if cmd_el is not None and cmd_el.text else None

    args_el = exec_el.find(f"{_TASK_NS}Arguments")
    script = None
    if args_el is not None and args_el.text:
        first = args_el.text.strip().split(" ")[0] if args_el.text.strip() else ""
        script = first.strip('"') or None
    return interpreter, script


def find_dangling(m3_memory_root: str) -> list[dict]:
    """Return one dict per REGISTERED task whose interpreter or script is missing.

    Each dict: {name, interpreter, script, missing: ["interpreter"|"script", ...]}.
    Not-installed tasks are skipped (not dangling). Query/parse errors for a
    single task are recorded with missing=["query-error"] rather than aborting
    the whole probe.
    """
    dangling: list[dict] = []
    for name in _expected_task_names(m3_memory_root):
        try:
            task_xml = _query_task_xml(name)
        except Exception as e:  # noqa: BLE001 — one bad task must not sink the probe
            dangling.append({"name": name, "interpreter": None, "script": None,
                             "missing": ["query-error"], "error": str(e)})
            continue
        if task_xml is None:
            continue  # not installed — not dangling

        interpreter, script = _parse_action_paths(task_xml)
        missing: list[str] = []
        if not interpreter or not os.path.exists(interpreter):
            missing.append("interpreter")
        # Only flag a missing script when the task actually declares one.
        if script and not os.path.exists(script):
            missing.append("script")
        if missing:
            dangling.append({"name": name, "interpreter": interpreter,
                             "script": script, "missing": missing})
    return dangling


def run(brief: bool = False) -> int:
    """Report registered AgentOS_* tasks that point at a missing interpreter or
    script. Always returns 0 (report-only). Never crashes the doctor."""
    if sys.platform != "win32":
        if brief:
            print("schedules: n/a (Windows-only check)")
        else:
            print()
            print("=== scheduled-task interpreters ===")
            print("  status   : n/a — this check covers Windows AgentOS_* tasks only.")
            print("             macOS/Linux use launchd/systemd/crontab (not checked here).")
        return 0

    # m3_memory_root is the code install root (parent of bin/). The scheduled
    # tasks were registered against whatever root install_schedules ran under;
    # here we only need the task NAMES, which are root-independent, so the exact
    # root value doesn't affect detection — the live XML carries the real paths.
    m3_memory_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    try:
        dangling = find_dangling(m3_memory_root)
    except Exception as e:  # noqa: BLE001 — probe must never crash the doctor
        if brief:
            print("schedules: unknown (probe failed)")
        else:
            print()
            print("=== scheduled-task interpreters ===")
            print(f"  status   : could not run schedule probe: {type(e).__name__}: {e}")
        return 0

    if brief:
        if dangling:
            print(f"⚠️  schedules: {len(dangling)} dangling task(s); run `m3 setup`")
        else:
            print("✅ schedules: OK (registered tasks resolve to a real interpreter)")
        return 0

    print()
    print("=== scheduled-task interpreters ===")
    if not dangling:
        print("  status   : OK — every registered AgentOS_* task points at an")
        print("             interpreter and script that still exist on disk.")
        return 0

    print(f"  status   : NAG — {len(dangling)} registered task(s) point at a missing")
    print("             interpreter/script. They fire on schedule but cannot launch:")
    for d in dangling:
        if "query-error" in d["missing"]:
            print(f"             - {d['name']} → could not query: {d.get('error', '')}")
            continue
        bits = []
        if "interpreter" in d["missing"]:
            bits.append(f"interpreter {d['interpreter']!r} (missing)")
        if "script" in d["missing"]:
            bits.append(f"script {d['script']!r} (missing)")
        print(f"             - {d['name']} → " + "; ".join(bits))
    print()
    print("  why      : the code install moved or was deleted (e.g. the venv/pipx")
    print("             dir was removed). The task stays registered but its baked-in")
    print("             absolute path no longer resolves, so the run fails silently —")
    print("             the background governor never comes back until re-registered.")
    print()
    print("  fix      : re-register the tasks against the current install:")
    print("               m3 setup")
    return 0
