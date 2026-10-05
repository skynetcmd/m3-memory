"""LLM endpoint switches reach daemons that never read the shell rc."""
import argparse
import json
import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

from m3_core import llm_config  # noqa: E402

from m3_memory import setup_wizard  # noqa: E402


@pytest.fixture
def config_root(monkeypatch, tmp_path):
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    monkeypatch.delenv("M3_ENABLE_OLLAMA_FAILOVER", raising=False)
    monkeypatch.delenv("M3_ENABLE_LMSTUDIO_FAILOVER", raising=False)
    return tmp_path


def test_file_is_used_when_the_environment_is_bare(config_root):
    llm_config.write_llm_setting("M3_ENABLE_OLLAMA_FAILOVER", "1")
    assert llm_config.llm_setting("M3_ENABLE_OLLAMA_FAILOVER") == "1"

    import llm_failover
    assert llm_failover._flag("M3_ENABLE_OLLAMA_FAILOVER", False) is True


def test_environment_outranks_the_file(config_root, monkeypatch):
    llm_config.write_llm_setting("M3_ENABLE_OLLAMA_FAILOVER", "1")
    monkeypatch.setenv("M3_ENABLE_OLLAMA_FAILOVER", "0")

    import llm_failover
    assert llm_failover._flag("M3_ENABLE_OLLAMA_FAILOVER", True) is False


def test_malformed_file_is_reported(config_root, caplog):
    (config_root / llm_config.LLM_CONFIG_NAME).write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="M3_SDK"):
        assert llm_config.llm_setting("M3_ENABLE_OLLAMA_FAILOVER") is None
    assert "unreadable" in caplog.text


def test_absent_file_is_silent(config_root, caplog):
    with caplog.at_level(logging.WARNING, logger="M3_SDK"):
        assert llm_config.llm_setting("M3_ENABLE_OLLAMA_FAILOVER") is None
    assert caplog.text == ""


def _probe(monkeypatch, reachable_port):
    monkeypatch.delenv("LLM_ENDPOINTS_CSV", raising=False)
    monkeypatch.delenv("M3_LLM_ENDPOINTS_CSV", raising=False)
    monkeypatch.delenv("M3_LLM_URL", raising=False)
    monkeypatch.setattr(setup_wizard, "_endpoint_reachable",
                        lambda url, **k: reachable_port in url)
    monkeypatch.setattr(setup_wizard, "_persist_env_var", lambda *a, **k: None)
    setup_wizard._probe_llm_endpoints(object(), argparse.Namespace(non_interactive=True))


def _recorded(config_root):
    return json.loads((config_root / llm_config.LLM_CONFIG_NAME).read_text(encoding="utf-8"))


def test_setup_records_ollama_even_when_the_shell_already_has_it(config_root, monkeypatch):
    # The repair path for an existing install: the shell already exports the
    # switch, and setup must still write the copy a daemon can see.
    monkeypatch.setenv("M3_ENABLE_OLLAMA_FAILOVER", "1")
    _probe(monkeypatch, "11434")
    assert _recorded(config_root) == {
        "M3_ENABLE_LMSTUDIO_FAILOVER": "0",
        "M3_ENABLE_OLLAMA_FAILOVER": "1",
    }


def test_setup_records_a_fresh_ollama_enable(config_root, monkeypatch):
    _probe(monkeypatch, "11434")
    assert _recorded(config_root)["M3_ENABLE_OLLAMA_FAILOVER"] == "1"


def test_setup_records_a_custom_server_url(config_root, monkeypatch):
    monkeypatch.delenv("LLM_ENDPOINTS_CSV", raising=False)
    monkeypatch.delenv("M3_LLM_ENDPOINTS_CSV", raising=False)
    monkeypatch.setenv("M3_LLM_URL", "http://localhost:8080/v1")
    monkeypatch.setattr(setup_wizard, "_endpoint_reachable", lambda url, **k: True)
    setup_wizard._probe_llm_endpoints(object(), argparse.Namespace(non_interactive=True))
    assert _recorded(config_root) == {"M3_LLM_URL": "http://localhost:8080/v1"}

    monkeypatch.delenv("M3_LLM_URL")
    import llm_failover
    assert llm_failover._setting("M3_LLM_URL") == "http://localhost:8080/v1"


def test_setup_writes_nothing_when_only_the_default_is_reachable(config_root, monkeypatch):
    _probe(monkeypatch, "1234")
    assert not (config_root / llm_config.LLM_CONFIG_NAME).exists()


@pytest.mark.skipif(os.name == "nt", reason="shell rc files are POSIX")
@pytest.mark.parametrize("text, expected", [
    ('export M3_ENABLE_OLLAMA_FAILOVER="1"\n', True),
    ("export M3_ENABLE_OLLAMA_FAILOVER=1  # m3\n", True),
    ('# export M3_ENABLE_OLLAMA_FAILOVER="1"\n', False),
    ('export M3_ENABLE_OLLAMA_FAILOVER="1"\nexport M3_ENABLE_OLLAMA_FAILOVER=0\n', False),
    ("export M3_ENABLE_OLLAMA_FAILOVER=10\n", False),
])
def test_shell_rc_has_reads_the_effective_assignment(monkeypatch, tmp_path, text, expected):
    from m3_memory.wizard import persist

    rc = tmp_path / ".zshrc"
    rc.write_text(text, encoding="utf-8")
    monkeypatch.setattr(persist, "_pick_unix_shell_rc", lambda: rc)
    assert persist.shell_rc_has("M3_ENABLE_OLLAMA_FAILOVER", "1") is expected
