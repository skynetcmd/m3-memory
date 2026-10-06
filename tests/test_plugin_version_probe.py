"""doctor's Claude Code plugin version/enabled probe.

Detects a stale plugin (a newer version cached than installed) and a disabled-
but-installed plugin (the `/plugin install` enable-flag footgun that makes m3
vanish from /mcp). Report-only — it prints the client-side fix commands but
never mutates ~/.claude and never bumps the exit code. Mocks the three data
sources so no real Claude Code config is touched.
"""
import sys
from pathlib import Path

_BIN = str(Path(__file__).resolve().parents[1] / "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from doctor import plugin_version_probe as P  # noqa: E402


def _patch(monkeypatch, *, installed, enabled, latest, pkg="2026.7.4.0",
           age_days=1.0):
    monkeypatch.setattr(P, "_installed_version", lambda: installed)
    monkeypatch.setattr(P, "_enabled", lambda: enabled)
    monkeypatch.setattr(P, "_latest_cached_version", lambda: latest)
    monkeypatch.setattr(P, "_package_version", lambda: pkg)
    monkeypatch.setattr(P, "_marketplace_age_days", lambda: age_days)


def test_healthy_current_enabled(monkeypatch, capsys):
    _patch(monkeypatch, installed="2026.7.4.0", enabled=True, latest="2026.7.4.0")
    assert P.run(brief=False) == 0
    out = capsys.readouterr().out
    assert "OK" in out and "current" in out


def test_disabled_is_flagged_with_enable_fix(monkeypatch, capsys):
    # The footgun: installed but enabledPlugins flipped to false.
    _patch(monkeypatch, installed="2026.7.4.0", enabled=False, latest="2026.7.4.0")
    P.run(brief=False)
    out = capsys.readouterr().out
    assert "DISABLED" in out
    assert 'enabledPlugins' in out and '"m3@skynetcmd": true' in out
    assert "/reload-plugins" in out


def test_stale_is_flagged_with_update_commands(monkeypatch, capsys):
    _patch(monkeypatch, installed="2026.4.24.11", enabled=True, latest="2026.7.4.0")
    P.run(brief=False)
    out = capsys.readouterr().out
    assert "2026.4.24.11" in out and "2026.7.4.0" in out
    assert "/plugin marketplace update skynetcmd" in out
    assert "/plugin install m3@skynetcmd" in out


def test_report_only_never_bumps_exit_code(monkeypatch):
    # Even a disabled+stale plugin returns 0 (user-recoverable, not a broken install).
    _patch(monkeypatch, installed="2026.4.24.11", enabled=False, latest="2026.7.4.0")
    assert P.run(brief=False) == 0
    assert P.run(brief=True) == 0


def test_not_installed_is_benign(monkeypatch, capsys):
    # CLI-only / manual-MCP users have no Claude Code plugin install — not an error.
    _patch(monkeypatch, installed=None, enabled=None, latest=None)
    assert P.run(brief=False) == 0
    out = capsys.readouterr().out
    assert "not installed" in out.lower()


def test_version_sort_key_is_numeric(monkeypatch):
    # 2026.7.4.0 must sort ABOVE 2026.4.24.11 (numeric, not lexical — '2026.7' >
    # '2026.4' but lexical string compare of '7' vs '24' would be wrong).
    assert P._ver_key("2026.7.4.0") > P._ver_key("2026.4.24.11")
    assert P._ver_key("2026.7.4.0") > P._ver_key("2026.5.30.1")
    assert P._ver_key("3.7.4") > P._ver_key("3.6.27")


def test_brief_glyphs(monkeypatch, capsys):
    _patch(monkeypatch, installed="2026.7.4.0", enabled=True, latest="2026.7.4.0")
    P.run(brief=True)
    assert "✅" in capsys.readouterr().out

    _patch(monkeypatch, installed="2026.7.4.0", enabled=False, latest="2026.7.4.0")
    P.run(brief=True)
    assert "⚠️" in capsys.readouterr().out


# ── marketplace-clone freshness ───────────────────────────────────────────────
# `_latest_cached_version` can only see versions the marketplace clone already
# fetched, so a clone that never refreshes made this probe report "current"
# forever while main was releases ahead (observed 2026-07-22: plugin pinned at
# 2026.7.13.0, clone 4 days cold, doctor said OK). These pin the honest wording —
# and, just as importantly, that an air-gapped box is never told it is broken.

def test_cold_clone_does_not_claim_currency(monkeypatch, capsys):
    """An unrefreshed clone means the version check is blind, not healthy."""
    _patch(monkeypatch, installed="2026.7.4.0", enabled=True,
           latest="2026.7.4.0", age_days=45.0)
    assert P.run(brief=False) == 0
    out = capsys.readouterr().out
    assert "45d ago" in out
    # Must NOT assert currency it cannot verify.
    assert "installed, enabled, and current." not in out
    assert "may exist upstream" in out


def test_cold_clone_reads_as_fine_when_offline(monkeypatch, capsys):
    """Air-gapped installs never refresh — that is expected, not a fault."""
    _patch(monkeypatch, installed="2026.7.4.0", enabled=True,
           latest="2026.7.4.0", age_days=400.0)
    assert P.run(brief=False) == 0
    out = capsys.readouterr().out
    assert out.lstrip().startswith("=== Claude Code plugin")
    assert "air-gapped" in out
    assert "nothing is wrong" in out
    # Never a FAIL/warning glyph, and never a nonzero exit, for being offline.
    assert "[FAIL]" not in out
    assert "⚠️" not in out


def test_cold_clone_brief_is_not_a_warning(monkeypatch, capsys):
    """Brief mode stays a ✅ FYI — no ⚠️ for an offline machine."""
    _patch(monkeypatch, installed="2026.7.4.0", enabled=True,
           latest="2026.7.4.0", age_days=45.0)
    P.run(brief=True)
    out = capsys.readouterr().out
    assert "✅" in out and "⚠️" not in out
    assert "45d old" in out


def test_unknown_clone_age_is_silent(monkeypatch, capsys):
    """No refresh timestamp at all (never fetched / foreign layout) must not
    invent a warning — absence of a signal is not evidence of staleness."""
    _patch(monkeypatch, installed="2026.7.4.0", enabled=True,
           latest="2026.7.4.0", age_days=None)
    assert P.run(brief=False) == 0
    out = capsys.readouterr().out
    assert "installed, enabled, and current." in out
    assert "marketplace last refreshed" not in out


def test_real_stale_version_still_nags(monkeypatch, capsys):
    """A genuinely newer cached version keeps its NAG even on a cold clone —
    the freshness FYI must not swallow a real, actionable update."""
    _patch(monkeypatch, installed="2026.7.4.0", enabled=True,
           latest="2026.7.22.0", age_days=45.0)
    assert P.run(brief=False) == 0
    out = capsys.readouterr().out
    assert "[NAG]" in out
    assert "/plugin marketplace update skynetcmd" in out


def test_a_plugin_older_than_the_package_is_flagged_even_with_a_cold_clone(monkeypatch, capsys):
    """The marketplace clone can be as old as the plugin, so "newest in cache"
    hides the gap; the installed package is the reference that cannot."""
    _patch(monkeypatch, installed="2026.9.20.1", enabled=True, latest="2026.9.20.1",
           pkg="2026.10.6.0", age_days=15.0)
    assert P.run(brief=True) == 0                      # report-only: no exit bump
    out = capsys.readouterr().out
    assert "⚠️" in out and "older than m3 2026.10.6.0" in out
    assert "m3 doctor --fix --fix-hooks" in out


def test_a_prerelease_package_does_not_flag_the_matching_plugin(monkeypatch, capsys):
    _patch(monkeypatch, installed="2026.10.6.0", enabled=True, latest="2026.10.6.0",
           pkg="2026.10.6.0rc13")
    assert P.run(brief=True) == 0
    assert "⚠️" not in capsys.readouterr().out


def test_repair_does_nothing_without_an_installed_plugin(monkeypatch):
    """`m3 upgrade` runs doctor --fix --fix-hooks everywhere; with no m3 plugin
    (pipx-only, or no Claude Code) the repair must not run the CLI and report a
    failure that is not one."""
    monkeypatch.setattr(P, "_installed_version", lambda: None)
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no CLI call without an installed plugin")))
    assert P.repair() == {"actions": []}


def test_repair_updates_an_installed_plugin(monkeypatch):
    monkeypatch.setattr(P, "_installed_version", lambda: "2026.9.20.1")
    res = P.repair(dry_run=True)
    assert [a["action"] for a in res["actions"]] == ["marketplace", "plugin"]


def test_doctor_fix_reaches_the_plugin_repair():
    """The --fix branch of memory_doctor returns before the report section, so a
    repair placed there never ran under --fix (plugins stayed releases behind)."""
    src = (Path(_BIN) / "memory_doctor.py").read_text(encoding="utf-8")
    repair_at = src.index("plugin_version_probe.repair(")
    fix_return_at = src.index('if res["summary"] == "failed" or shared_rc != 0:')
    assert repair_at < fix_return_at


def _fake_cli(monkeypatch, versions):
    """installed_plugins.json reads return `versions` in turn; the CLI succeeds."""
    seq = iter(versions)
    monkeypatch.setattr(P, "_installed_version", lambda: next(seq))
    import subprocess
    from types import SimpleNamespace
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="ok", stderr=""))


def test_repair_asks_for_a_restart_only_when_the_plugin_changed(monkeypatch):
    _fake_cli(monkeypatch, ["2026.10.5.0", "2026.10.5.0"])
    assert "restart" not in [a["action"] for a in P.repair()["actions"]]
    _fake_cli(monkeypatch, ["2026.9.20.1", "2026.10.5.0"])
    assert "restart" in [a["action"] for a in P.repair()["actions"]]
