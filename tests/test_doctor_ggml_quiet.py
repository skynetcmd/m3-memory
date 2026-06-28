"""m3 doctor quiets the llama.cpp/GGML backend process-wide.

Both doctor probes load the bge-m3 GGUF — cascade_probe IN-PROCESS (native
EmbeddedEmbedder) and embed_server_probe in a SUBPROCESS. llama.cpp otherwise
dumps its full per-tensor load trace + Metal teardown to stderr, burying the
readable summary. memory_doctor.main() sets GGML_LOG_LEVEL=4 (error-only) before
any probe runs, respecting an operator override.
"""
import importlib.util
import os
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"


def _load_main():
    spec = importlib.util.spec_from_file_location("memory_doctor_t", BIN / "memory_doctor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main


def _run_main_help(monkeypatch):
    """Invoke main() with --help (exits early after the env setup line)."""
    monkeypatch.setattr(sys, "argv", ["memory_doctor.py", "--help"])
    main = _load_main()
    try:
        main()
    except SystemExit:
        pass  # --help raises SystemExit after argparse prints usage


def test_sets_ggml_quiet_by_default(monkeypatch):
    monkeypatch.delenv("GGML_LOG_LEVEL", raising=False)
    _run_main_help(monkeypatch)
    assert os.environ.get("GGML_LOG_LEVEL") == "4"


def test_respects_operator_override(monkeypatch):
    monkeypatch.setenv("GGML_LOG_LEVEL", "2")
    _run_main_help(monkeypatch)
    # setdefault must NOT clobber an explicit operator value.
    assert os.environ.get("GGML_LOG_LEVEL") == "2"
