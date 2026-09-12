"""`restart_reaped_services` must honour its own contract on all three OSes.

Its docstring says: *"Whatever we stop, we start."* Before this was fixed it
called Windows `schtasks` with no platform guard, so on macOS and Linux it
raised `FileNotFoundError`, a bare `except` swallowed it, and every service the
fleet-wide reap had stopped stayed down. The reap half worked everywhere; only
the restart half was Windows-only.

These tests pin the two things that made the first fix attempt wrong:

* service identifiers come from a MAP, never a derivation
* a wrong or missing name FAILS LOUD rather than reading as a successful restart
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import install_schedules as isch  # noqa: E402


def test_identifiers_come_from_a_map_not_a_derivation():
    """A derivation was tried and scored 3 of 5 against the real launchd labels.

    Two entries defeat every expressible naming rule: embed-server is
    ``com.skynetcmd.m3-embed-server`` on macOS -- different product prefix,
    hyphens KEPT -- and has no systemd unit at all on Linux. Deriving names also
    fails quietly, because a start on a name that does not exist looks like a
    restart that worked.
    """
    import ast

    with open(isch.__file__, encoding="utf-8") as fh:
        text = fh.read()
    assert "_ROLE_TO_SERVICE" in text, "no explicit service map"

    # Parsed, not grepped. The source DISCUSSES the rejected derivation in a
    # comment explaining why a map is required, and a substring check cannot
    # tell a mention from a use -- the same false positive that made an earlier
    # drift guard pass against the very bug it was written for (§12c).
    tree = ast.parse(text)
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "restart_reaped_services"),
        None,
    )
    assert fn is not None, "restart_reaped_services not found"
    derived = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "replace"
    ]
    assert not derived, (
        "restart_reaped_services derives a service name with .replace(); "
        "names must be looked up in _ROLE_TO_SERVICE"
    )


@pytest.mark.parametrize("role", ["dashboard", "cognitive-loop", "embed-server"])
def test_every_reapable_role_is_mapped_on_every_platform(role):
    """Each entry must name all three platforms explicitly -- including `None`.

    `None` is a real answer ("this platform has no managed service for this
    role") and is handled distinctly from a missing entry. A silent KeyError
    here would put us back to services stopped and never restarted.
    """
    entry = isch._ROLE_TO_SERVICE.get(role)
    assert entry is not None, f"{role} has no _ROLE_TO_SERVICE entry"
    for key in ("win", "darwin", "linux"):
        assert key in entry, f"{role} has no {key} entry (use None, not omission)"


def test_embed_server_is_not_derivable():
    """The specific case that proves the map is necessary rather than tidy.

    Verified against a live macOS host (`launchctl list`, 2026-09-12) and
    against this installer's own systemd unit names.
    """
    e = isch._ROLE_TO_SERVICE["embed-server"]
    assert e["darwin"] == "com.skynetcmd.m3-embed-server", (
        "macOS uses a different product prefix AND keeps the hyphens"
    )
    assert e["linux"] is None, (
        "the Rust m3-embed-server manages its own service; there is no unit"
    )


def test_restart_command_is_platform_specific():
    """schtasks / launchctl / systemctl -- the whole point of the fix."""
    cmd = isch._restart_command("X")
    assert cmd[0] in {"schtasks", "launchctl", "systemctl"}, cmd
    key = isch._platform_key()
    expected = {"win": "schtasks", "darwin": "launchctl", "linux": "systemctl"}[key]
    assert cmd[0] == expected, f"on {key} the restart must use {expected}, got {cmd[0]}"


def test_linux_existence_is_checked_because_systemd_start_returns_zero():
    """systemd returns EXIT 0 for `start` on a unit that does not exist.

    Measured on WSL Ubuntu 24.04, 2026-09-12::

        systemctl --user start m3-nonexistent.service   -> exit 0
        systemctl --user show -p LoadState --value ...  -> "not-found"

    So a returncode check alone cannot detect a wrong Linux name, and a typo
    in the map would read as a successful restart -- the exact silent failure
    this function exists to prevent. The LoadState probe is the discriminator.
    """
    with open(isch.__file__, encoding="utf-8") as fh:
        text = fh.read()
    assert "LoadState" in text, (
        "no LoadState probe: on Linux a bad unit name would look like success"
    )
    assert "not-found" in text, "the LoadState result is not being compared"


def test_missing_service_manager_reports_once_and_stops():
    """A missing `schtasks`/`systemctl` is a PLATFORM fault, not a per-service
    one. Reporting it N times as a shrug is how the original bug read as noise;
    it must be one loud message that names the consequence."""
    with open(isch.__file__, encoding="utf-8") as fh:
        text = fh.read()
    assert "except FileNotFoundError" in text, (
        "the service manager being absent must be caught distinctly from a "
        "per-service failure"
    )
    assert "stay DOWN" in text, (
        "the operator must be told what the failure COSTS, not just that a "
        "command failed"
    )
