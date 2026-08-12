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


# ── cwd must not change the verdict ──────────────────────────────────────────

def test_version_probes_run_from_a_neutral_cwd():
    """The probe's answer must not depend on where the doctor was launched.

    Python puts the working directory at sys.path[0], so a process started
    inside a source checkout imports that tree's packages and reads its
    *.dist-info before the installed ones. For a probe whose whole job is
    comparing installed-vs-running versions, cwd is then a hidden input: the
    same probe, same interpreter, reported 0 findings from a temp dir and 3
    "stale launcher" FAILs from a checkout (measured 2026-08-11) — sending the
    user to uninstall a launcher that was never broken.

    Both sides of the comparison must stand in the same neutral place:
    _installed_version() resolves in a child pinned to _neutral_cwd(), and
    _probe_version() passes the same cwd to the launcher it runs.
    """
    import subprocess as _sp

    from doctor import entrypoint_probe as ep

    neutral = ep._neutral_cwd()
    assert neutral and os.path.isdir(neutral)
    # A checkout is exactly what must NOT be used: it carries its own dist-info.
    assert not os.path.exists(os.path.join(neutral, "pyproject.toml")), (
        f"_neutral_cwd() returned a source checkout: {neutral}"
    )

    seen = {}
    real_run = _sp.run

    def capture(cmd, **kw):
        seen["cwd"] = kw.get("cwd")
        return real_run(cmd, **kw)

    ep.subprocess.run = capture
    try:
        ep._probe_version(sys.executable)
    finally:
        ep.subprocess.run = real_run

    assert seen.get("cwd") == neutral, (
        f"_probe_version ran the launcher from {seen.get('cwd')!r}, not the "
        "neutral cwd — a checkout would shadow the launcher's own package"
    )


def test_installed_version_ignores_a_shadowing_cwd(tmp_path, monkeypatch):
    """A dist-info sitting in the working directory must not be believed.

    Reproduces the exact shape that caused the false FAIL: a checkout with its
    own m3_memory-<other version>.dist-info beside it.
    """
    from doctor import entrypoint_probe as ep

    fake = tmp_path / "m3_memory-1.2.3.dist-info"
    fake.mkdir()
    (fake / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: m3-memory\nVersion: 1.2.3\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    # Assert the MECHANISM, not just the answer. Checking `got != "1.2.3"` alone
    # passes for the wrong reason on a machine whose in-process lookup happens
    # to return something else — verified: deleting the subprocess entirely left
    # that weaker assertion green. What must hold is that the resolution is
    # delegated to a child pinned OUTSIDE the shadowing directory.
    seen = {}
    real_run = ep.subprocess.run

    def capture(cmd, **kw):
        seen.setdefault("cwd", kw.get("cwd"))
        return real_run(cmd, **kw)

    ep.subprocess.run = capture
    try:
        got = ep._installed_version()
    finally:
        ep.subprocess.run = real_run

    assert seen.get("cwd") == ep._neutral_cwd(), (
        "_installed_version() resolved the version in-process (or from "
        f"{seen.get('cwd')!r}) instead of a child pinned to a neutral cwd — "
        "a dist-info in the working directory would be believed"
    )
    assert got != "1.2.3", (
        "read the version from a dist-info in the CWD — the probe is still "
        "shadowable and will invent stale-launcher findings in any checkout"
    )
