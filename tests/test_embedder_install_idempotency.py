"""Regression tests for the 2026-07-27 upgrade defects.

All four were user-visible in ONE `m3 setup` run on a healthy machine:
  1. `m3-embed-server install` failed because the service ALREADY existed, and
     setup reported the embedder "SKIPPED (not installed)" while it was
     Automatic, running, and serving :8082.
  2. The failure advice printed systemd/nohup/crontab — on Windows.
  3. An upgrade replaces the binary on disk but the service keeps running the
     OLD image, with nothing to detect or fix it.
  4. Discovery never probed m3's OWN model dir, so it depended on LM Studio /
     Ollama / llama.cpp staying installed.

Two more surfaced when .9 was validated on the machine that reported the
originals (hence the .10 fixes at the bottom of this file):
  5. The staleness check read psutil's `exe`, which is EMPTY for a LocalSystem
     service queried unelevated — so on real Windows it found nothing and the
     stale process kept serving old code, silently.
  6. A .8-era config pinned to an LM Studio path outranked ~/.m3/models forever,
     so only fresh installs ever became sovereign, and deleting the third-party
     copy broke install while a good model sat unused.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

from m3_memory import embedder_admin as ea  # noqa: E402


# ── 2. OS-correct failure advice ─────────────────────────────────────────────

@pytest.mark.parametrize("platform,expected,forbidden", [
    ("win32", "Administrator", ("crontab", "nohup", "systemd")),
    ("darwin", "launchd", ("crontab", "Administrator terminal")),
    ("linux", "systemd", ("Administrator terminal",)),
])
def test_install_failure_hint_is_os_correct(platform, expected, forbidden):
    """m3 supports 3 OSes; the recovery advice must match the one you're on.

    The original text printed `nohup`/`crontab -e`/`~/.m3/engine/...` on every
    platform, so a Windows user was told to use tools that don't exist there
    for a service that registers with SCM.
    """
    with mock.patch.object(sys, "platform", platform):
        hint = ea._install_failure_hint(Path("/models/bge.gguf"))
    assert expected.lower() in hint.lower()
    for bad in forbidden:
        assert bad.lower() not in hint.lower(), f"{bad!r} leaked into {platform} advice"


# ── 1. install is idempotent when the service already exists ─────────────────

def _fake_binary(tmp_path: Path) -> Path:
    binary = tmp_path / "m3-embed-server.exe"
    binary.write_bytes(b"\x00")
    return binary


def test_service_reports_installed_parses_status(tmp_path, monkeypatch):
    """`status` prints running/stopped when registered, 'not installed' when not.

    Exit code alone is not enough to tell those apart, which is why the opaque
    'IO error in winapi call' was misread as "no service".
    """
    binary = _fake_binary(tmp_path)
    gguf = tmp_path / "m.gguf"

    for stdout, expected in [
        ("running\n", True),
        ("stopped\n", True),
        ("not installed\n", False),
    ]:
        monkeypatch.setattr(
            ea.subprocess, "run",
            lambda *a, _o=stdout, **k: SimpleNamespace(stdout=_o, stderr="", returncode=0),
        )
        assert ea._service_reports_installed(binary, gguf) is expected, stdout


def test_service_reports_installed_survives_a_crash(tmp_path, monkeypatch):
    """A probe that can't run must return False, never raise into setup (§3)."""
    binary = _fake_binary(tmp_path)
    monkeypatch.setattr(ea.subprocess, "run",
                        mock.Mock(side_effect=OSError("boom")))
    assert ea._service_reports_installed(binary, tmp_path / "m.gguf") is False


# ── 3. stale-binary detection after an upgrade ───────────────────────────────

def test_stale_when_process_predates_its_binary(tmp_path, monkeypatch):
    """A process started BEFORE its binary's mtime is running the old image.

    This is the upgrade case: pipx replaces the .exe on disk, Windows keeps the
    original mapped, and the service serves stale code until restarted.
    """
    binary = _fake_binary(tmp_path)
    bin_mtime = binary.stat().st_mtime

    fake_proc = SimpleNamespace(info={
        "name": "m3-embed-server",
        "exe": str(binary),
        "create_time": bin_mtime - 3600,   # started an hour before the upgrade
    })
    fake_psutil = SimpleNamespace(
        process_iter=lambda attrs=None: [fake_proc],
        Error=Exception,
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    assert ea._service_binary_is_stale(binary) is True


def test_not_stale_when_process_started_after_its_binary(tmp_path, monkeypatch):
    """A freshly-restarted service must NOT be flagged — no needless bounce."""
    binary = _fake_binary(tmp_path)
    fake_proc = SimpleNamespace(info={
        "name": "m3-embed-server",
        "exe": str(binary),
        "create_time": binary.stat().st_mtime + 60,
    })
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
        process_iter=lambda attrs=None: [fake_proc], Error=Exception))
    assert ea._service_binary_is_stale(binary) is False


def test_not_stale_when_no_matching_process(tmp_path, monkeypatch):
    """Nothing running -> not stale (don't guess)."""
    binary = _fake_binary(tmp_path)
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
        process_iter=lambda attrs=None: [], Error=Exception))
    assert ea._service_binary_is_stale(binary) is False


# ── 4. m3's own model dir ────────────────────────────────────────────────────

def test_models_root_precedence(monkeypatch):
    """M3_MODELS_ROOT > M3_MEMORY_ROOT/models > ~/.m3/models — matching the
    established engine/config root contract exactly."""
    from m3_core.paths import get_m3_models_root

    monkeypatch.delenv("M3_MODELS_ROOT", raising=False)
    monkeypatch.delenv("M3_MEMORY_ROOT", raising=False)
    assert get_m3_models_root() == str(Path.home() / ".m3" / "models")

    monkeypatch.setenv("M3_MEMORY_ROOT", str(Path("/srv/m3")))
    assert get_m3_models_root().endswith("models")

    monkeypatch.setenv("M3_MODELS_ROOT", str(Path("/srv/models")))
    assert Path(get_m3_models_root()).name == "models"


def test_m3_models_dir_probed_before_third_party(monkeypatch, tmp_path):
    """m3's own dir must be FIRST. Every other location belongs to a tool the
    user may uninstall (LM Studio, Ollama, llama.cpp)."""
    monkeypatch.setenv("M3_MODELS_ROOT", str(tmp_path))
    import importlib

    import memory.embed as embed
    importlib.reload(embed)

    own = tmp_path / "bge-m3-Q4_K_M.gguf"
    own.write_bytes(b"GGUF" + b"\x00" * 64)

    found = embed.discover_bge_m3_gguf(budget_s=5.0)
    assert found is not None
    assert Path(found).parent == tmp_path, f"third-party dir won: {found}"


def test_invalid_gguf_is_rejected(tmp_path, monkeypatch):
    """An HTML error page or truncated download saved as .gguf must not pass
    as a model — validate MAGIC BYTES, not just the extension."""
    from memory.model_fetch import _is_valid_gguf

    monkeypatch.setenv("M3_MODELS_ROOT", str(tmp_path))
    html = tmp_path / "bad.gguf"
    html.write_bytes(b"<!DOCTYPE html><html>404</html>")
    assert _is_valid_gguf(html) is False

    truncated = tmp_path / "short.gguf"
    truncated.write_bytes(b"GGUF" + b"\x00" * 1000)   # right magic, wrong size
    assert _is_valid_gguf(truncated) is False


def test_fetch_is_opt_out_via_env(monkeypatch):
    """M3_NO_MODEL_DOWNLOAD=1 must suppress the download offer entirely —
    air-gapped and metered-connection installs (§1 local-first)."""
    monkeypatch.setenv("M3_NO_MODEL_DOWNLOAD", "1")
    assert ea._offer_model_download() is None


# ── quiesce progress spinner ─────────────────────────────────────────────────

def test_quiesce_spinner_redraws_one_line(monkeypatch, capsys):
    """The 30s quiesce wait must show motion, and must redraw ONE line with \r
    rather than reprinting it — the same complaint as pipx's whole-line rewrite.
    """
    from m3_memory import setup_wizard as sw

    monkeypatch.setattr(sw.sys.stdout, "isatty", lambda: True, raising=False)
    args = SimpleNamespace(gui_child=False)
    tick = sw._quiesce_tick(args)
    assert tick is not None

    live = [SimpleNamespace(role="dashboard", pid=1)]
    for elapsed in (0.0, 0.25, 0.5, 0.75):
        tick(elapsed, 30.0, live)
    out = capsys.readouterr().out

    assert "\n" not in out, "spinner must not emit newlines (one line, redrawn)"
    assert out.count("\r") >= 4, "each tick must carriage-return"
    assert any(f in out for f in "|/-\\"), "no spinner frame rendered"
    assert "dashboard" in out


def test_quiesce_spinner_disabled_without_a_console(monkeypatch):
    """No TTY (piped output) or a GUI child -> no spinner, so logs and windowless
    runs don't accumulate one line per tick."""
    from m3_memory import setup_wizard as sw

    monkeypatch.setattr(sw.sys.stdout, "isatty", lambda: False, raising=False)
    assert sw._quiesce_tick(SimpleNamespace(gui_child=False)) is None

    monkeypatch.setattr(sw.sys.stdout, "isatty", lambda: True, raising=False)
    assert sw._quiesce_tick(SimpleNamespace(gui_child=True)) is None


def _proc(*, name="m3-embed-server", exe=None, create_time=0.0):
    return SimpleNamespace(info={
        "name": name, "exe": exe, "create_time": create_time})


def _fake_psutil(procs):
    return SimpleNamespace(process_iter=lambda attrs=None: procs, Error=Exception)


# ── 5. staleness detection when `exe` is unreadable (Windows service) ────────

def test_stale_detected_when_exe_is_unreadable(tmp_path, monkeypatch):
    """The real 2026-07-27 miss: a LocalSystem service queried unelevated
    reports exe='' (WMI ExecutablePath is blank too). Matching only on the
    resolved path found nothing, so the upgrade left a stale process running
    and the restart never fired. `create_time` IS readable — fall back to name.
    """
    binary = _fake_binary(tmp_path)
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil([
        _proc(name="m3-embed-server.exe", exe=None,
              create_time=binary.stat().st_mtime - 3600)]))
    assert ea._service_binary_is_stale(binary) is True


def test_name_fallback_still_respects_the_grace_window(tmp_path, monkeypatch):
    """A freshly restarted service with an unreadable path is NOT stale —
    the fallback must not turn into 'always restart'."""
    binary = _fake_binary(tmp_path)
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil([
        _proc(name="m3-embed-server.exe", exe=None,
              create_time=binary.stat().st_mtime + 60)]))
    assert ea._service_binary_is_stale(binary) is False


def test_readable_exe_that_does_not_match_is_not_stale(tmp_path, monkeypatch):
    """Name matching is the weaker check, so it is a FALLBACK only: a process
    whose path IS readable and points elsewhere must still be disqualified,
    even when its name collides."""
    binary = _fake_binary(tmp_path)
    other = tmp_path / "other" / "m3-embed-server.exe"
    other.parent.mkdir()
    other.write_bytes(b"\x00")
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil([
        _proc(name="m3-embed-server.exe", exe=str(other),
              create_time=binary.stat().st_mtime - 3600)]))
    assert ea._service_binary_is_stale(binary) is False


def test_unrelated_process_with_blank_exe_is_ignored(tmp_path, monkeypatch):
    """Blank exe must not make every nameless process a match."""
    binary = _fake_binary(tmp_path)
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil([
        _proc(name="chrome.exe", exe=None,
              create_time=binary.stat().st_mtime - 3600),
        _proc(name="", exe=None, create_time=0.0)]))
    assert ea._service_binary_is_stale(binary) is False


# ── 6. sovereignty migration off a third-party configured path ───────────────

def _plausible_gguf(path: Path) -> Path:
    """Write a file that passes _is_valid_gguf: right magic bytes AND a size
    inside the ±2% window around 437,778,496 bytes. Sparse — truncate() does not
    allocate blocks, so this costs no real disk. A 64-byte stub is correctly
    REJECTED by the size gate (see test_invalid_gguf_is_rejected), so tests that
    need a model to be *found* must use this.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"GGUF")
        fh.truncate(437_778_496)
    return path


@pytest.fixture()
def sovereign(tmp_path, monkeypatch):
    """An m3-owned models root + an isolated config root."""
    models, config = tmp_path / "models", tmp_path / "config"
    models.mkdir(), config.mkdir()
    monkeypatch.setenv("M3_MODELS_ROOT", str(models))
    monkeypatch.setenv("M3_CONFIG_ROOT", str(config))
    monkeypatch.delenv("M3_EMBED_GGUF", raising=False)
    monkeypatch.delenv("M3_MEMORY_ROOT", raising=False)
    monkeypatch.delenv("M3_EMBED_GGUF_AUTODETECT", raising=False)
    own = _plausible_gguf(models / "bge-m3-Q4_K_M.gguf")
    return SimpleNamespace(models=models, config=config, own=own,
                           cfg=config / ".embed_config.json")


def _write_cfg(sov, gguf_path):
    import json
    sov.cfg.write_text(json.dumps({
        "disable_inproc_embedder": True,
        "fallback_url": "http://127.0.0.1:8082",
        "gguf_path": str(gguf_path),
    }), encoding="utf-8")


def _read_cfg(sov):
    import json
    return json.loads(sov.cfg.read_text(encoding="utf-8"))


def test_own_copy_beats_third_party_config_and_migrates(sovereign, tmp_path, capsys):
    """(2) The upgrade case: a .8-era config points at LM Studio while m3 owns a
    copy. m3's own must win, and the config must be REWRITTEN so every other
    resolver agrees — otherwise only fresh installs ever become sovereign."""
    lmstudio = tmp_path / "lmstudio" / "bge-m3-GGUF-Q4_K_M.gguf"
    lmstudio.parent.mkdir()
    lmstudio.write_bytes(b"GGUF" + b"\x00" * 64)
    _write_cfg(sovereign, lmstudio)

    found = ea._find_bundled_gguf()
    assert found == sovereign.own, f"third-party path won: {found}"
    assert _read_cfg(sovereign)["gguf_path"] == str(sovereign.own)
    assert "migrated" in capsys.readouterr().out.lower(), "silent repoint"


def test_migration_preserves_other_config_keys(sovereign, tmp_path):
    """Rewriting gguf_path must not clobber shared-mode settings."""
    third = tmp_path / "third" / "m.gguf"
    third.parent.mkdir()
    third.write_bytes(b"GGUF" + b"\x00" * 64)
    _write_cfg(sovereign, third)
    ea._find_bundled_gguf()
    data = _read_cfg(sovereign)
    assert data["disable_inproc_embedder"] is True
    assert data["fallback_url"] == "http://127.0.0.1:8082"


def test_self_heals_when_configured_path_is_gone(sovereign):
    """(1) The ditch-LM-Studio case: the configured file no longer exists, but
    m3 owns a copy. Resolve to it instead of reporting no model."""
    _write_cfg(sovereign, sovereign.models.parent / "deleted" / "gone.gguf")
    assert ea._find_bundled_gguf() == sovereign.own


def test_already_sovereign_config_is_left_alone(sovereign, capsys):
    """No churn when the config already points inside a root we own."""
    _write_cfg(sovereign, sovereign.own)
    assert ea._find_bundled_gguf() == sovereign.own
    assert "migrated" not in capsys.readouterr().out.lower()


def test_third_party_config_wins_when_m3_owns_nothing(sovereign, tmp_path):
    """Sovereignty is a PREFERENCE, not a requirement — with no m3-owned copy,
    honour the configured path rather than failing the install."""
    sovereign.own.unlink()
    third = tmp_path / "third" / "m.gguf"
    third.parent.mkdir()
    third.write_bytes(b"GGUF" + b"\x00" * 64)
    _write_cfg(sovereign, third)
    assert ea._find_bundled_gguf() == third


def test_explicit_env_override_is_never_migrated(sovereign, tmp_path, monkeypatch):
    """$M3_EMBED_GGUF means "use exactly this". Migrating out from under it
    would make the flag stop meaning what it says."""
    explicit = tmp_path / "explicit.gguf"
    explicit.write_bytes(b"GGUF" + b"\x00" * 64)
    monkeypatch.setenv("M3_EMBED_GGUF", str(explicit))
    _write_cfg(sovereign, explicit)
    assert ea._find_bundled_gguf() == explicit
    assert _read_cfg(sovereign)["gguf_path"] == str(explicit), "config rewritten"


def test_autodetect_off_suppresses_migration(sovereign, tmp_path, monkeypatch):
    """M3_EMBED_GGUF_AUTODETECT=0 already means "only what I named"; it must
    keep suppressing the config read, and so the migration too."""
    monkeypatch.setenv("M3_EMBED_GGUF_AUTODETECT", "0")
    third = tmp_path / "third" / "m.gguf"
    third.parent.mkdir()
    third.write_bytes(b"GGUF" + b"\x00" * 64)
    _write_cfg(sovereign, third)
    ea._find_bundled_gguf()
    assert _read_cfg(sovereign)["gguf_path"] == str(third), "config rewritten"


def test_migration_survives_an_unwritable_config(sovereign, tmp_path, monkeypatch):
    """A read-only config must not break resolution — the answer is already in
    hand; persisting it is advisory (§3 fail safe)."""
    third = tmp_path / "third" / "m.gguf"
    third.parent.mkdir()
    third.write_bytes(b"GGUF" + b"\x00" * 64)
    _write_cfg(sovereign, third)
    monkeypatch.setattr(Path, "replace",
                        mock.Mock(side_effect=OSError("read-only")))
    assert ea._find_bundled_gguf() == sovereign.own


def test_wait_for_quiesce_survives_a_raising_tick(monkeypatch):
    """A broken progress renderer must never break the wait itself (§3)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
    import m3_halt

    monkeypatch.setattr(m3_halt, "list_blocking_db_writers",
                        lambda root=None: [])
    # Empty registry returns before any tick; the guard is exercised by the
    # non-empty path below.
    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("renderer exploded")

    seq = [[SimpleNamespace(role="x", pid=1)], []]
    monkeypatch.setattr(m3_halt, "list_blocking_db_writers",
                        lambda root=None: seq.pop(0) if seq else [])
    result = m3_halt.wait_for_quiesce(timeout=5.0, poll=0.01, on_tick=_boom)
    assert result.ok is True
    assert calls["n"] >= 1, "on_tick was never invoked"
