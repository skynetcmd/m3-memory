"""Touch ID for sudo: offered once, only at the Mac, verified by reading back.

Hermetic: the PAM files are temp files and `sudo tee` is a stub; nothing here
touches /etc/pam.d.
"""
from __future__ import annotations

import pytest

from m3_memory import setup_wizard, touchid


@pytest.fixture
def mac(monkeypatch, tmp_path):
    pam = tmp_path / "pam_tid.so.2"
    pam.write_bytes(b"")
    template = tmp_path / "sudo_local.template"
    template.write_text("# sudo_local\n#auth       sufficient     pam_tid.so\n", encoding="utf-8")
    monkeypatch.setattr(touchid, "_is_mac", lambda: True)
    monkeypatch.setattr(touchid, "PAM_TID", str(pam))
    monkeypatch.setattr(touchid, "TEMPLATE", str(template))
    monkeypatch.setattr(touchid, "SUDO_LOCAL", str(tmp_path / "sudo_local"))
    monkeypatch.setattr(touchid, "_sensor_present", lambda: True)
    for k in ("SSH_CONNECTION", "SSH_TTY"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def _fake_sudo_tee(monkeypatch, write=True, rc=0):
    def run(argv, input=None, **_kw):
        assert argv[:3] == ["sudo", "tee", "-a"]
        if write:
            with open(argv[3], "a", encoding="utf-8") as fh:
                fh.write(input)
        return type("R", (), {"returncode": rc, "stderr": ""})()
    monkeypatch.setattr(touchid.subprocess, "run", run)


def test_status_reads_the_real_states(mac, monkeypatch):
    assert touchid.status() == "available"
    (mac / "sudo_local").write_text("#auth sufficient pam_tid.so\n", encoding="utf-8")
    assert touchid.status() == "available", "a commented line is not enabled"
    (mac / "sudo_local").write_text("auth       sufficient     pam_tid.so\n", encoding="utf-8")
    assert touchid.status() == "enabled"
    (mac / "sudo_local").unlink()
    monkeypatch.setattr(touchid, "_sensor_present", lambda: False)
    assert touchid.status() == "no-sensor"
    monkeypatch.setattr(touchid, "_is_mac", lambda: False)
    assert touchid.status() == "unsupported"


def test_enable_appends_one_line_and_checks_the_file(mac, monkeypatch):
    _fake_sudo_tee(monkeypatch)
    ok, detail = touchid.enable()
    assert ok, detail
    assert (mac / "sudo_local").read_text(encoding="utf-8") == touchid.LINE + "\n"


def test_enable_does_not_claim_success_the_file_does_not_show(mac, monkeypatch):
    _fake_sudo_tee(monkeypatch, write=False)      # sudo "succeeds", nothing written
    ok, detail = touchid.enable()
    assert not ok and "no active pam_tid line" in detail


def test_setup_offers_only_at_the_mac_and_remembers_a_no(mac, monkeypatch):
    monkeypatch.setattr(setup_wizard.sys, "platform", "darwin")
    monkeypatch.setattr(setup_wizard, "_stdin_is_interactive", lambda: True)
    monkeypatch.setenv("M3_CONFIG_ROOT", str(mac / "config"))
    asked = []
    monkeypatch.setattr(setup_wizard, "_ask_yes_no", lambda q, default=True: asked.append(q) or False)

    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.2 5000 10.0.0.1 22")
    setup_wizard._offer_touchid_sudo()
    assert asked == [], "no fingerprint reaches an SSH session"

    monkeypatch.delenv("SSH_CONNECTION")
    setup_wizard._offer_touchid_sudo()
    setup_wizard._offer_touchid_sudo()
    assert len(asked) == 1, "a no is remembered"
    assert not (mac / "sudo_local").exists()
