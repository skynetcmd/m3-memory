"""`m3 governor migrate` must never block an unattended elevated window.

Deleting scheduled tasks needs Administrator on Windows, so the doctor tells
users to run this FROM AN ELEVATED SHELL — which `Start-Process -Verb RunAs`
opens as a SEPARATE window. A bare `input()` there does not raise EOFError: the
window has a real console, just nobody typing into it. The prompt blocks
forever, and the user is left staring at an admin window that does nothing it
can explain until they kill it.

That is worse than a crash. An elevated process sitting open with no output
reads as m3 having hung — or as something quietly doing work it will not name —
and it costs trust in exactly the moment we asked for the highest privilege.

The guard: prompt only when stdin is a tty. Otherwise refuse the destructive
default, name the flag that makes it non-interactive, and exit 0.
"""
from __future__ import annotations

import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import governor_cli as gc  # noqa: E402


class _NotATty(io.StringIO):
    """A stdin that exists and reads empty — an idle console, not EOF."""

    def isatty(self) -> bool:
        return False


class _IsATty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _fake_eligible(monkeypatch, names=("AgentOS_Fake1", "AgentOS_Fake2")):
    monkeypatch.setattr(gc.gm, "detect_scheduled_tasks",
                        lambda: {"eligible": list(names)})


def test_no_tty_refuses_instead_of_prompting(monkeypatch, capsys):
    """The regression itself: no console -> no question, and NO deletion."""
    _fake_eligible(monkeypatch)

    def _must_not_delete(_names):
        raise AssertionError("deleted scheduled tasks without consent")

    monkeypatch.setattr(gc.gm, "try_remove_scheduled_tasks", _must_not_delete)
    monkeypatch.setattr(sys, "stdin", _NotATty(""))

    rc = gc.cmd_migrate(yes=False)
    out = capsys.readouterr().out

    assert rc == 0, "an unattended run is not an error — it is a no-op"
    assert "No interactive console" in out
    assert "--yes" in out, "must name the flag that makes this non-interactive"


def test_no_tty_never_calls_input(monkeypatch):
    """Belt and braces: input() must not be reached at all without a tty.

    Asserting on the message alone would still pass if input() were called
    first and happened to return — the hang is what we are preventing.
    """
    _fake_eligible(monkeypatch)
    monkeypatch.setattr(gc.gm, "try_remove_scheduled_tasks", lambda n: ([], []))
    monkeypatch.setattr(sys, "stdin", _NotATty(""))

    def _boom(*_a, **_k):
        raise AssertionError("input() called with no tty — this is the hang")

    monkeypatch.setattr("builtins.input", _boom)
    assert gc.cmd_migrate(yes=False) == 0


def test_yes_flag_skips_the_prompt_entirely(monkeypatch):
    """--yes is the documented unattended path; it must not consult stdin."""
    _fake_eligible(monkeypatch)
    removed = {}
    monkeypatch.setattr(gc.gm, "try_remove_scheduled_tasks",
                        lambda names: (removed.setdefault("names", list(names)), [])[1] or (list(names), []))

    def _boom(*_a, **_k):
        raise AssertionError("input() called despite --yes")

    monkeypatch.setattr("builtins.input", _boom)
    monkeypatch.setattr(sys, "stdin", _NotATty(""))

    assert gc.cmd_migrate(yes=True) == 0
    assert removed.get("names") == ["AgentOS_Fake1", "AgentOS_Fake2"]


def test_a_real_tty_still_gets_the_confirmation(monkeypatch):
    """The guard must not silently disable consent for interactive users."""
    _fake_eligible(monkeypatch)
    monkeypatch.setattr(gc.gm, "try_remove_scheduled_tasks", lambda n: (list(n), []))
    monkeypatch.setattr(sys, "stdin", _IsATty("n\n"))

    asked = {"n": 0}

    def _answer(_prompt=""):
        asked["n"] += 1
        return "n"

    monkeypatch.setattr("builtins.input", _answer)
    rc = gc.cmd_migrate(yes=False)
    assert asked["n"] == 1, "an interactive user must still be asked"
    assert rc == 0


# ── the same rule, everywhere a prompt can land in an unattended console ─────

def test_migrate_memory_confirm_declines_without_a_tty(monkeypatch):
    """migrate_memory._confirm must not block either.

    The installer runs migrate_memory.py as a SUBPROCESS during `m3 setup`, and
    on Windows schema work can land in an elevated window. It passes --yes
    today, but _confirm is the shared helper: one caller forgetting the flag
    reintroduces the hang. Declining costs a re-run; hanging costs trust in a
    privileged process.
    """
    import migrate_memory as mm

    def _boom(*_a, **_k):
        raise AssertionError("input() called with no tty — this is the hang")

    monkeypatch.setattr("builtins.input", _boom)
    monkeypatch.setattr(sys, "stdin", _NotATty(""))

    assert mm._confirm("Drop everything?", False) is False, "must decline, not hang"
    assert mm._confirm("Drop everything?", True) is True, "--yes must still pass"


def test_migrate_memory_confirm_still_asks_on_a_real_tty(monkeypatch):
    """The guard must not silently remove consent from interactive users."""
    import migrate_memory as mm

    asked = {"n": 0}

    def _answer(_prompt=""):
        asked["n"] += 1
        return "y"

    monkeypatch.setattr("builtins.input", _answer)
    monkeypatch.setattr(sys, "stdin", _IsATty("y\n"))
    assert mm._confirm("Proceed?", False) is True
    assert asked["n"] == 1


def test_no_unguarded_prompt_in_elevation_reachable_scripts():
    """Class-level guard: any script m3 can run ELEVATED must gate its prompts.

    A prompt in a window opened by `Start-Process -Verb RunAs` blocks forever —
    the console is real, just unattended, so EOFError never fires. Rather than
    re-audit by hand each time a script is added, assert that every module here
    which calls input() also consults isatty().
    """
    import pathlib
    import re

    bin_dir = pathlib.Path(__file__).resolve().parent.parent / "bin"
    # Scripts reachable from an elevated launch (setup_wizard / cli elevation
    # paths) or run as a subprocess by the installer.
    reachable = ("governor_cli.py", "migrate_memory.py", "install_schedules.py")

    offenders = []
    for name in reachable:
        src = (bin_dir / name).read_text(encoding="utf-8", errors="replace")
        prompts = len(re.findall(r"(?<![\w.])input\s*\(", src))
        if prompts and "isatty" not in src:
            offenders.append(f"{name}: {prompts} input() call(s), no isatty guard")
    assert not offenders, (
        "prompt reachable from an elevated/unattended console with no tty "
        "guard — it will hang the window:\n  " + "\n  ".join(offenders)
    )
