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


def _patch(monkeypatch, *, installed, enabled, latest, pkg="2026.7.4.0"):
    monkeypatch.setattr(P, "_installed_version", lambda: installed)
    monkeypatch.setattr(P, "_enabled", lambda: enabled)
    monkeypatch.setattr(P, "_latest_cached_version", lambda: latest)
    monkeypatch.setattr(P, "_package_version", lambda: pkg)


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
