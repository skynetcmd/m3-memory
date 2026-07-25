"""Console entrypoints on PATH must run THIS install.

Nothing else checks this. plugin_version_probe compares the Claude Code plugin
manifest against the payload; schedule_probe checks that scheduled jobs point at
a live interpreter. Neither asks the simpler question: when the user types `m3`,
do they get the code that was just installed?

Two real failures this guards, both observed 2026-07-25:
  - shadowing: a `pip --user` shim earlier on PATH won over the pipx install, so
    `mcp-memory` served 3-week-old code while the installer reported success.
  - orphaned launcher: a failed `pipx install --force` uninstalled the package
    but left the .exe, so PATH lookup "succeeded" and every invocation raised
    ModuleNotFoundError.
"""

import os
import sys

import pytest

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)


@pytest.fixture
def probe():
    from doctor import entrypoint_probe
    return entrypoint_probe


def test_clean_install_reports_nothing(probe, monkeypatch):
    monkeypatch.setattr(probe, "_installed_version", lambda: "2026.7.25.0")
    monkeypatch.setattr(probe, "_which_all",
                        lambda n: [f"/pipx/bin/{n}"] if n == "m3" else [])
    monkeypatch.setattr(probe, "_probe_version", lambda p, timeout=20.0: ("2026.7.25.0", None))
    assert probe.check() == []
    assert probe.run(brief=True) == 0


def test_stale_entrypoint_is_flagged(probe, monkeypatch):
    """The launcher runs, but it is an older build than the installed package."""
    monkeypatch.setattr(probe, "_installed_version", lambda: "2026.7.25.0")
    monkeypatch.setattr(probe, "_which_all",
                        lambda n: [f"/old/bin/{n}"] if n == "m3" else [])
    monkeypatch.setattr(probe, "_probe_version", lambda p, timeout=20.0: ("2026.4.8", None))

    findings = probe.check()
    assert [f["kind"] for f in findings] == ["stale"]
    assert "2026.4.8" in findings[0]["detail"]
    assert probe.run(brief=True) == 1, "a stale entrypoint must fail the doctor"


def test_orphaned_launcher_is_flagged(probe, monkeypatch):
    """Package uninstalled, .exe left behind -> ModuleNotFoundError on invoke.
    PATH lookup succeeds, so only actually RUNNING it reveals the breakage."""
    monkeypatch.setattr(probe, "_installed_version", lambda: "2026.7.25.0")
    monkeypatch.setattr(probe, "_which_all",
                        lambda n: [f"/dead/bin/{n}"] if n == "m3" else [])
    monkeypatch.setattr(
        probe, "_probe_version",
        lambda p, timeout=20.0: (None, "ModuleNotFoundError: No module named 'm3_memory'"),
    )

    findings = probe.check()
    assert [f["kind"] for f in findings] == ["broken"]
    assert probe.run(brief=True) == 1


def test_shadowing_is_flagged_even_when_the_winner_is_current(probe, monkeypatch):
    """The winner being correct today is luck -- PATH order is not a contract.
    A second copy on PATH is reported so it can be removed before it wins."""
    monkeypatch.setattr(probe, "_installed_version", lambda: "2026.7.25.0")
    monkeypatch.setattr(
        probe, "_which_all",
        lambda n: [f"/pipx/bin/{n}", f"/usr/local/bin/{n}"] if n == "m3" else [],
    )
    monkeypatch.setattr(probe, "_probe_version", lambda p, timeout=20.0: ("2026.7.25.0", None))

    findings = probe.check()
    assert [f["kind"] for f in findings] == ["shadowed"]
    assert "/usr/local/bin/m3" in findings[0]["detail"]
    assert probe.run(brief=True) == 1


def test_absent_optional_entrypoint_is_not_a_finding(probe, monkeypatch):
    """Not every install exposes every script -- absence is not breakage."""
    monkeypatch.setattr(probe, "_installed_version", lambda: "2026.7.25.0")
    monkeypatch.setattr(probe, "_which_all", lambda n: [])
    assert probe.check() == []


def test_probe_never_raises(probe, monkeypatch):
    """The doctor must survive a probe blowing up."""
    def boom(*a, **k):
        raise RuntimeError("PATH exploded")
    monkeypatch.setattr(probe, "_which_all", boom)
    assert probe.run(brief=True) == 0


def test_version_resolves_from_repo_checkout(probe, monkeypatch):
    """The doctor runs as bin/memory_doctor.py, so under an interpreter without
    m3-memory installed BOTH metadata lookups and the package import fail. The
    probe must still find the version (from pyproject.toml) rather than degrade
    to SKIPPED -- a dev machine is exactly where mismatched entrypoints happen.
    """
    import importlib.metadata as md

    def no_dist(_name):
        raise md.PackageNotFoundError("m3-memory")

    monkeypatch.setattr(md, "version", no_dist)
    monkeypatch.setitem(sys.modules, "m3_memory", None)  # force the import to fail

    version = probe._installed_version()
    assert version, "version must resolve from pyproject.toml in a checkout"
    assert version[0].isdigit(), version


def test_undeterminable_version_reports_skipped_not_ok(probe, monkeypatch, capsys):
    """With no expected version the stale/shadowed comparisons cannot run. Say
    SKIPPED -- printing OK would claim a check that never happened."""
    monkeypatch.setattr(probe, "_installed_version", lambda: None)
    monkeypatch.setattr(probe, "_which_all", lambda n: [])

    assert probe.run(brief=True) == 0
    assert "SKIPPED" in capsys.readouterr().out
