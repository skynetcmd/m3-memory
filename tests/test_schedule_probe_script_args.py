"""The schedule probe must find the script past interpreter flags.

Regression origin (2026-09-14): _parse_exec_paths took the FIRST <Arguments>
token as the script path. AgentOS_NotificationWaiter ships as

    <Arguments>"-u" "...\\bin\\m3_notification_waiter.py" "--agent-id" ...

so the probe read "-u", found no such file, and reported a HEALTHY task --
State=Running, script present, two live pythonw processes -- as
"script '-u' (missing)", then told the operator to run `m3 setup` against it.

That is the section 3 false-alarm trap twice over: a warning that fires when
nothing is wrong trains the reader to ignore the one that matters, and this one
also invited a needless re-register of a working job.

Hermetic: pure string/XML parsing. No schtasks, no filesystem, no network.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from doctor.schedule_probe import _parse_exec_paths, _script_from_args  # noqa: E402

_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def _task_xml(command: str, arguments: str) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-16"?>'
        f'<Task xmlns="{_NS}"><Actions Context="Author"><Exec>'
        f"<Command>{command}</Command><Arguments>{arguments}</Arguments>"
        f"</Exec></Actions></Task>"
    )


# ── the exact shape that caused the false alarm ───────────────────────────────
def test_skips_dash_u_and_finds_the_real_script():
    args = ('"-u" "C:\\p\\bin\\m3_notification_waiter.py" '
            '"--agent-id" "agy" "--interval" "5"')
    assert _script_from_args(args) == "C:\\p\\bin\\m3_notification_waiter.py"


def test_parse_exec_paths_on_the_notification_waiter_shape():
    xml = _task_xml(
        '"C:\\p\\Scripts\\pythonw.exe"',
        '"-u" "C:\\p\\bin\\m3_notification_waiter.py" "--agent-id" "agy"',
    )
    interpreter, script = _parse_exec_paths(xml)
    assert interpreter == "C:\\p\\Scripts\\pythonw.exe"
    assert script == "C:\\p\\bin\\m3_notification_waiter.py", \
        "took an interpreter flag as the script path"


# ── the other real shapes on this machine ─────────────────────────────────────
@pytest.mark.parametrize("args,expected", [
    # No flags at all (AgentOS_EmbedServer / dashboard shape).
    ('"C:\\p\\bin\\embed_server_inproc.py" "--port" "8082"',
     "C:\\p\\bin\\embed_server_inproc.py"),
    # Several flags before the script (the PreToolUse hook shape).
    ('-S -E "C:\\p\\hooks\\pretool_dispatch.py"',
     "C:\\p\\hooks\\pretool_dispatch.py"),
    # A flag that takes no value, then the script.
    ('-u "C:\\p\\bin\\m3_loop_watchdog.py"', "C:\\p\\bin\\m3_loop_watchdog.py"),
    # Unquoted, single token.
    ("C:\\p\\bin\\sync_all.py", "C:\\p\\bin\\sync_all.py"),
])
def test_common_argument_shapes(args, expected):
    assert _script_from_args(args) == expected


# ── flags that CONSUME a value must not yield a fake script ───────────────────
@pytest.mark.parametrize("args", ['-c "import sys"', "-m pytest", "-m m3_memory.cli"])
def test_c_and_m_values_are_not_scripts(args):
    """-c takes a code string and -m a module name; neither is a file on disk.
    Returning one would invent a NEW false alarm of the kind this fixes."""
    assert _script_from_args(args) is None


def test_flags_only_yields_none():
    assert _script_from_args("-u -S -E") is None


def test_empty_arguments_yield_none():
    assert _script_from_args("") is None
    assert _script_from_args("   ") is None


# ── the probe must still CATCH a genuinely missing script ─────────────────────
def test_a_real_missing_script_is_still_found(tmp_path):
    """The fix must not blind the probe: a task whose script is genuinely
    absent must still surface. Guard against over-correction."""
    xml = _task_xml('"C:\\p\\Scripts\\pythonw.exe"',
                    '"-u" "C:\\definitely\\not\\here.py"')
    _, script = _parse_exec_paths(xml)
    assert script == "C:\\definitely\\not\\here.py", \
        "the probe must still report the path it cannot find"


def test_missing_exec_element_is_handled():
    xml = (f'<?xml version="1.0"?><Task xmlns="{_NS}">'
           f"<Actions Context=\"Author\"></Actions></Task>")
    assert _parse_exec_paths(xml) == (None, None)
