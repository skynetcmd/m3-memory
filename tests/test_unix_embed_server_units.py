"""Tests for the Unix embed-server keep-alive units (launchd + systemd).

Closes the gap tracked since 2026-07 and made concrete on 2026-09-13: only
Windows had an embed-server supervisor, so a Linux/macOS host running shared
mode had nothing restarting :8082 after a crash or reboot.

Hermetic (§3): renders templates and drives the installer with every
subprocess and service manager mocked. No launchctl, no systemctl, no HTTP.
"""
from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_BIN = _ROOT / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import install_schedules as sched  # noqa: E402

PLIST = _BIN / "com.m3memory.embedserver.plist"
UNIT = _BIN / "m3-embed-server.service"


# ── templates exist and are well-formed ───────────────────────────────────────
def test_templates_ship():
    """The watchdog's _restart_backend() names these units; a missing template
    would make its start command fail with 'unit not found'."""
    assert PLIST.is_file(), f"missing launchd template: {PLIST}"
    assert UNIT.is_file(), f"missing systemd template: {UNIT}"


def test_plist_renders_to_valid_xml(tmp_path):
    rendered = sched._render_template(str(PLIST), "/opt/m3", "/opt/m3/.venv/bin/python")
    data = plistlib.loads(rendered.encode("utf-8"))
    assert data["Label"] == "com.m3memory.embedserver"
    assert data["ProgramArguments"][0] == "/opt/m3/.venv/bin/python"
    assert data["ProgramArguments"][1].endswith("bin/embed_server_inproc.py")
    assert "8082" in data["ProgramArguments"]


def test_plist_has_no_unsubstituted_placeholders():
    rendered = sched._render_template(str(PLIST), "/opt/m3", "/py")
    assert "[M3_MEMORY_ROOT]" not in rendered
    assert "[M3_PYTHON]" not in rendered


def test_systemd_unit_has_no_unsubstituted_placeholders():
    rendered = sched._render_template(str(UNIT), "/opt/m3", "/py")
    assert "[M3_MEMORY_ROOT]" not in rendered
    assert "[M3_PYTHON]" not in rendered
    assert "ExecStart=/py /opt/m3/bin/embed_server_inproc.py --port 8082" in rendered


# ── the properties that matter for recovery ───────────────────────────────────
def test_launchd_keepalive_is_unconditional():
    """KeepAlive={Crashed:true} ignores a CLEAN exit — the bug that left the
    cognitive loop dead for 4h on 2026-08-09. In shared mode the whole fleet
    defers to this process, so any exit must be restarted."""
    data = plistlib.loads(
        sched._render_template(str(PLIST), "/opt/m3", "/py").encode("utf-8"))
    assert data["KeepAlive"] is True, "KeepAlive must be unconditional, not Crashed-only"
    assert data.get("RunAtLoad") is True
    assert int(data.get("ThrottleInterval", 0)) > 0, "no crash-loop throttle"


def test_systemd_restart_is_always():
    rendered = sched._render_template(str(UNIT), "/opt/m3", "/py")
    assert "Restart=always" in rendered
    assert "RestartSec=" in rendered


def test_systemd_unit_does_not_narrow_the_start_limit():
    """A restart BUDGET is the exact failure mode this whole change exists to
    fix (SCM spent RESTART x3 on a sick GPU driver and never retried). The
    unit must not add its own StartLimitBurst on top."""
    rendered = sched._render_template(str(UNIT), "/opt/m3", "/py")
    live = [ln.strip() for ln in rendered.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    assert not any(ln.startswith("StartLimitBurst") for ln in live)
    assert not any(ln.startswith("StartLimitIntervalSec") for ln in live)


def test_neither_unit_hardcodes_a_gguf_path():
    """§3 headless rule: a baked-in M3_EMBED_GGUF would drift out of sync with
    .embed_config.json. The server auto-detects via discover_bge_m3_gguf()."""
    for tpl in (PLIST, UNIT):
        rendered = sched._render_template(str(tpl), "/opt/m3", "/py")
        live = [ln for ln in rendered.splitlines()
                if not ln.strip().startswith("#")]
        assert not any("M3_EMBED_GGUF=" in ln for ln in live), f"{tpl.name} pins a GGUF"


# ── installer behaviour ───────────────────────────────────────────────────────
@pytest.fixture
def fake_run(monkeypatch):
    calls: list[list[str]] = []

    class R:
        returncode = 0
        stdout = ""
        stderr = ""

    def _run(cmd, **kw):
        calls.append(list(cmd))
        return R()
    monkeypatch.setattr(sched.subprocess, "run", _run)
    return calls


def test_refuses_when_rust_service_registered(monkeypatch, fake_run, tmp_path):
    """Mutually exclusive on :8082 — installing both gives one port two
    supervisors. REFUSE, don't skip: see test_indeterminate_refuses."""
    monkeypatch.setattr(sched, "_rust_embed_service_loaded", lambda: True)
    monkeypatch.setattr(sched, "_os_name", lambda: "Darwin")
    with pytest.raises(sched.EmbedServerConflict):
        sched.install_unix_embed_server(str(tmp_path), "/py")
    assert not any("launchctl" in c[0] for c in fake_run), \
        "installed a competing unit while the Rust service owns :8082"


def test_indeterminate_refuses(monkeypatch, fake_run, tmp_path):
    """§3: the guard must refuse when it CANNOT TELL.

    A guard that returns False on error installs a second supervisor on every
    error path — the exact collision it exists to prevent."""
    monkeypatch.setattr(sched, "_rust_embed_service_loaded", lambda: None)
    monkeypatch.setattr(sched, "_os_name", lambda: "Darwin")
    with pytest.raises(sched.EmbedServerConflict):
        sched.install_unix_embed_server(str(tmp_path), "/py")
    assert fake_run == []


# ── the guard's own logic (previously mocked away entirely) ───────────────────
def test_loaded_detects_the_real_live_mac_state(monkeypatch):
    """Ground truth from brs-macbook-pro, 2026-09-14:

        launchctl list  ->  -  0  com.skynetcmd.m3-embed-server   (LOADED)
        lsof -iTCP:8082 ->  nothing listening

    The label is registered while the port is FREE and state = not running.
    A binary check and a /health probe both read 'clear' in this state, which
    is why the check must be `launchctl list`."""
    real = ("-\t0\tcom.skynetcmd.m3-embed-server\n"
            "-\t0\tcom.m3memory.sync_all\n"
            "77535\t0\tcom.m3memory.cognitiveloop\n")

    class R:
        returncode = 0
        stdout = real
        stderr = ""
    monkeypatch.setattr(sched, "_os_name", lambda: "Darwin")
    monkeypatch.setattr(sched.subprocess, "run", lambda *a, **k: R())
    assert sched._rust_embed_service_loaded() is True


def test_loaded_is_none_when_launchctl_cannot_run(monkeypatch):
    """Indeterminate, NOT absent — the caller turns this into a refusal."""
    monkeypatch.setattr(sched, "_os_name", lambda: "Darwin")

    def _boom(*a, **k):
        raise OSError("launchctl: not found")
    monkeypatch.setattr(sched.subprocess, "run", _boom)
    assert sched._rust_embed_service_loaded() is None


def test_loaded_is_none_on_nonzero_rc(monkeypatch):
    class R:
        returncode = 1
        stdout = ""
        stderr = "Could not connect to server"
    monkeypatch.setattr(sched, "_os_name", lambda: "Darwin")
    monkeypatch.setattr(sched.subprocess, "run", lambda *a, **k: R())
    assert sched._rust_embed_service_loaded() is None


def test_loaded_false_when_label_absent(monkeypatch):
    class R:
        returncode = 0
        stdout = "-\t0\tcom.m3memory.cognitiveloop\n"
        stderr = ""
    monkeypatch.setattr(sched, "_os_name", lambda: "Darwin")
    monkeypatch.setattr(sched.subprocess, "run", lambda *a, **k: R())
    assert sched._rust_embed_service_loaded() is False


def test_linux_is_false_because_the_map_says_no_unit_exists(monkeypatch):
    """Measured, not guessed: _ROLE_TO_SERVICE maps embed-server to None on
    linux — the Rust binary manages its own service and ships no systemd unit,
    so no registration can exist to collide with."""
    assert sched._ROLE_TO_SERVICE["embed-server"]["linux"] is None
    monkeypatch.setattr(sched, "_os_name", lambda: "Linux")
    assert sched._rust_embed_service_loaded() is False


def test_guard_uses_the_one_owner_label(monkeypatch):
    """§10a: the label comes from _ROLE_TO_SERVICE, not a second hardcoded copy.
    Repoint the map and the guard must follow."""
    monkeypatch.setitem(sched._ROLE_TO_SERVICE["embed-server"],
                        "darwin", "com.example.relabeled")

    class R:
        returncode = 0
        stdout = "-\t0\tcom.example.relabeled\n"
        stderr = ""
    monkeypatch.setattr(sched, "_os_name", lambda: "Darwin")
    monkeypatch.setattr(sched.subprocess, "run", lambda *a, **k: R())
    assert sched._rust_embed_service_loaded() is True


def test_conflict_does_not_abort_the_whole_installer(monkeypatch, capsys):
    """install_unix_embed_server is the LAST call in install_unix_cognitive_loop,
    so a propagating raise would fail the installer AFTER the loop installed.
    The conflict is fatal to the embed-server unit only — loudly."""
    def _conflict(*a, **k):
        raise sched.EmbedServerConflict("two supervisors, one port")
    monkeypatch.setattr(sched, "install_unix_embed_server", _conflict)
    sched._install_embed_server_or_report("/root", "/py")  # must not raise
    out = capsys.readouterr().out
    assert "two supervisors, one port" in out
    assert sched.FAIL in out, "a refusal must not read as a routine skip"


def test_linux_installs_and_enables(monkeypatch, fake_run, tmp_path):
    monkeypatch.setattr(sched, "_rust_embed_service_loaded", lambda: False)
    monkeypatch.setattr(sched, "_os_name", lambda: "Linux")
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "home"))
    sched.install_unix_embed_server(str(_ROOT), "/py")
    flat = [" ".join(c) for c in fake_run]
    assert any("daemon-reload" in f for f in flat)
    assert any("enable --now m3-embed-server.service" in f for f in flat)


def test_darwin_installs_and_loads(monkeypatch, fake_run, tmp_path):
    monkeypatch.setattr(sched, "_rust_embed_service_loaded", lambda: False)
    monkeypatch.setattr(sched, "_os_name", lambda: "Darwin")
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "home"))
    sched.install_unix_embed_server(str(_ROOT), "/py")
    flat = [" ".join(c) for c in fake_run]
    assert any("launchctl load" in f for f in flat)
    # unload-before-load, so a re-run is idempotent
    assert any("launchctl unload" in f for f in flat)


def test_windows_is_a_noop(monkeypatch, fake_run, tmp_path):
    """Windows uses the Rust SCM service; there is no Python unit to install."""
    monkeypatch.setattr(sched, "_rust_embed_service_loaded", lambda: False)
    monkeypatch.setattr(sched, "_os_name", lambda: "Windows")
    sched.install_unix_embed_server(str(tmp_path), "/py")
    assert fake_run == []


def test_installer_never_raises(monkeypatch, tmp_path):
    """A keep-alive that breaks the installer is worse than no keep-alive."""
    monkeypatch.setattr(sched, "_rust_embed_service_loaded", lambda: False)
    monkeypatch.setattr(sched, "_os_name", lambda: "Linux")

    def _boom(*a, **k):
        raise OSError("no systemctl")
    monkeypatch.setattr(sched.subprocess, "run", _boom)
    sched.install_unix_embed_server(str(_ROOT), "/py")  # must not raise


def test_missing_template_warns_not_crashes(monkeypatch, fake_run, tmp_path, capsys):
    monkeypatch.setattr(sched, "_rust_embed_service_loaded", lambda: False)
    monkeypatch.setattr(sched, "_os_name", lambda: "Linux")
    sched.install_unix_embed_server(str(tmp_path), "/py")  # tmp_path has no bin/
    assert "missing" in capsys.readouterr().out.lower()


def test_watchdog_unit_names_match_the_installed_units():
    """§10a one-owner check: the watchdog starts what the installer registers.
    A rename on either side silently breaks recovery on that OS."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_ew", _BIN / "m3_embed_watchdog.py")
    ew = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ew)
    assert ew._SYSTEMD_UNIT == UNIT.name
    plist_label = plistlib.loads(
        sched._render_template(str(PLIST), "/o", "/p").encode("utf-8"))["Label"]
    assert ew._LAUNCHD_LABEL == plist_label
