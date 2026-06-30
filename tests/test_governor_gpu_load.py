"""Tests for GPU-load-aware governor pacing.

Guards the fix where the Python telemetry path hardcoded gpu_total=0.0, leaving
the governor blind to a GPU-pinned local LLM / embed server. probe_gpu_util now
feeds real GPU utilization into the load metric so the loop throttles/halts.
"""
from __future__ import annotations

import os
import sys
import time

_BIN = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import m3_sdk


def _idle_user():
    # Push the last-interaction stamp back so the "normal mode" idle gate
    # (elapsed < 30s -> HALTED) doesn't mask the load-tier assertions.
    m3_sdk._LAST_USER_INTERACTION = time.time() - 120


def _pin_thresholds(monkeypatch, initial, limit):
    """Force fixed thresholds regardless of any on-disk .governor_config.json or
    env, so load-tier assertions are hermetic."""
    monkeypatch.setattr(m3_sdk, "_governor_thresholds", lambda now=None: (initial, limit))


def test_governor_reacts_to_gpu_load(monkeypatch):
    _idle_user()
    _pin_thresholds(monkeypatch, 85, 95)
    # GPU idle -> full speed.
    assert m3_sdk.get_governor_pacing(
        {"cpu_total": 5, "ram_total": 10, "gpu_total": 10})["background"] == "CONTINUOUS"
    # GPU busy (>= initial 85) -> throttled, even with low CPU/RAM.
    assert m3_sdk.get_governor_pacing(
        {"cpu_total": 5, "ram_total": 10, "gpu_total": 87})["background"] == "THROTTLED"
    # GPU critical (>= limit 95) -> halted.
    assert m3_sdk.get_governor_pacing(
        {"cpu_total": 5, "ram_total": 10, "gpu_total": 96})["background"] == "HALTED"


def _fresh_probe_cache(monkeypatch):
    monkeypatch.setattr(m3_sdk, "_GPU_PROBE_DISABLE", False)
    monkeypatch.setattr(m3_sdk, "_gpu_probe_cache",
                        {"ts": 0.0, "util": 0.0, "backend": None, "misses": 0})


def test_probe_gpu_util_disabled_returns_zero(monkeypatch):
    monkeypatch.setattr(m3_sdk, "_GPU_PROBE_DISABLE", True)
    assert m3_sdk.probe_gpu_util() == 0.0


def test_probe_gpu_util_no_backend_is_cpu_only(monkeypatch):
    # CPU-only host: every backend returns None -> 0.0, and after MAX_MISSES the
    # probe trips off (circuit breaker) so it stops spawning subprocesses.
    _fresh_probe_cache(monkeypatch)
    for name, _fn in m3_sdk._GPU_PROBES:
        monkeypatch.setattr(m3_sdk, f"_gpu_util_{name.replace('-', '_')}", lambda: None, raising=False)
    # Patch the actual functions referenced in the _GPU_PROBES tuple.
    monkeypatch.setattr(m3_sdk, "_GPU_PROBES", tuple((n, lambda: None) for n, _ in m3_sdk._GPU_PROBES))

    now = 0.0
    for _ in range(m3_sdk._GPU_PROBE_MAX_MISSES):
        now += m3_sdk._GPU_PROBE_TTL + 1
        assert m3_sdk.probe_gpu_util(now=now) == 0.0
    assert m3_sdk._gpu_probe_cache["misses"] >= m3_sdk._GPU_PROBE_MAX_MISSES


def test_probe_gpu_util_picks_first_working_backend(monkeypatch):
    # First backend unavailable (None), second returns a value -> that value
    # wins and its backend is pinned.
    _fresh_probe_cache(monkeypatch)
    monkeypatch.setattr(m3_sdk, "_GPU_PROBES", (
        ("nvidia", lambda: None),
        ("windows", lambda: 73.0),
        ("macos", lambda: 5.0),
    ))
    assert m3_sdk.probe_gpu_util(now=100.0) == 73.0
    assert m3_sdk._gpu_probe_cache["backend"] == "windows"


def _write_cfg(path, payload, mtime):
    """Write the config and pin a distinct mtime so the mtime-based reload
    detector fires deterministically (two writes in one OS tick can collide)."""
    import json
    import os
    path.write_text(json.dumps(payload))
    os.utime(str(path), (mtime, mtime))


def test_governor_thresholds_config_file_overrides(monkeypatch, tmp_path):
    # Config file is authoritative over env and default, re-parsed only on mtime
    # change.
    cfg_root = tmp_path
    cfg = cfg_root / ".governor_config.json"
    monkeypatch.setattr(m3_sdk, "get_m3_config_root", lambda: str(cfg_root))
    monkeypatch.setattr(m3_sdk, "_GOV_CFG_TTL", 0.0)  # always re-stat
    monkeypatch.setattr(m3_sdk, "_gov_cfg_cache",
                        {"ts": 0.0, "mtime": None, "initial": None, "limit": None})
    monkeypatch.delenv("M3_GOVERNOR_INITIAL_THRESHOLD", raising=False)
    monkeypatch.delenv("M3_GOVERNOR_LIMIT_THRESHOLD", raising=False)

    # No file -> defaults.
    assert m3_sdk._governor_thresholds(now=1.0) == (85, 95)

    # File present -> its values win (the user's 40/75 ask).
    _write_cfg(cfg, {"initial_threshold": 40, "limit_threshold": 75}, mtime=1000.0)
    assert m3_sdk._governor_thresholds(now=2.0) == (40, 75)

    # initial >= limit is sanitized to limit-5 (new mtime forces a re-parse).
    _write_cfg(cfg, {"initial_threshold": 90, "limit_threshold": 80}, mtime=2000.0)
    assert m3_sdk._governor_thresholds(now=3.0) == (75, 80)


def test_governor_thresholds_skips_reparse_when_unchanged(monkeypatch, tmp_path):
    # Same mtime -> the file is NOT re-opened/parsed; the cached value is reused.
    cfg_root = tmp_path
    cfg = cfg_root / ".governor_config.json"
    monkeypatch.setattr(m3_sdk, "get_m3_config_root", lambda: str(cfg_root))
    monkeypatch.setattr(m3_sdk, "_GOV_CFG_TTL", 0.0)
    monkeypatch.setattr(m3_sdk, "_gov_cfg_cache",
                        {"ts": 0.0, "mtime": None, "initial": None, "limit": None})
    monkeypatch.delenv("M3_GOVERNOR_INITIAL_THRESHOLD", raising=False)
    monkeypatch.delenv("M3_GOVERNOR_LIMIT_THRESHOLD", raising=False)

    _write_cfg(cfg, {"initial_threshold": 40, "limit_threshold": 75}, mtime=1000.0)
    assert m3_sdk._governor_thresholds(now=1.0) == (40, 75)

    # Spy on open() to prove no re-parse happens when mtime is unchanged.
    import builtins
    opens = []
    real_open = builtins.open

    def _counting_open(*a, **k):
        if a and ".governor_config.json" in str(a[0]):
            opens.append(1)
        return real_open(*a, **k)
    monkeypatch.setattr(builtins, "open", _counting_open)

    # mtime is still 1000.0 -> stat sees no change -> no open of the config.
    assert m3_sdk._governor_thresholds(now=2.0) == (40, 75)
    assert opens == []


def test_ensure_governor_config_creates_and_is_idempotent(monkeypatch, tmp_path):
    import json
    cfg_root = tmp_path
    cfg = cfg_root / ".governor_config.json"
    monkeypatch.setattr(m3_sdk, "get_m3_config_root", lambda: str(cfg_root))
    monkeypatch.setattr(m3_sdk, "_GOV_CFG_TTL", 0.0)
    monkeypatch.setattr(m3_sdk, "_gov_cfg_cache",
                        {"ts": 0.0, "mtime": None, "initial": None, "limit": None})
    monkeypatch.setenv("M3_GOVERNOR_INITIAL_THRESHOLD", "40")
    monkeypatch.setenv("M3_GOVERNOR_LIMIT_THRESHOLD", "75")

    assert not cfg.exists()
    path = m3_sdk.ensure_governor_config()
    assert cfg.exists()
    data = json.loads(cfg.read_text())
    # Seeded with the current EFFECTIVE thresholds (here from env).
    assert data["initial_threshold"] == 40
    assert data["limit_threshold"] == 75
    assert "_comment" in data

    # Idempotent: an existing file is never overwritten (user edits survive).
    cfg.write_text(json.dumps({"initial_threshold": 10, "limit_threshold": 20}))
    m3_sdk.ensure_governor_config()
    assert json.loads(cfg.read_text())["initial_threshold"] == 10  # untouched
    assert path == str(cfg)


def test_governor_thresholds_env_fallback(monkeypatch, tmp_path):
    # No config file -> env var is used over the default.
    monkeypatch.setattr(m3_sdk, "get_m3_config_root", lambda: str(tmp_path))
    monkeypatch.setattr(m3_sdk, "_GOV_CFG_TTL", 0.0)
    monkeypatch.setattr(m3_sdk, "_gov_cfg_cache",
                        {"ts": 0.0, "mtime": None, "initial": None, "limit": None})
    monkeypatch.setenv("M3_GOVERNOR_INITIAL_THRESHOLD", "30")
    monkeypatch.setenv("M3_GOVERNOR_LIMIT_THRESHOLD", "60")
    assert m3_sdk._governor_thresholds(now=1.0) == (30, 60)


def test_local_llm_url_detection():
    import m3_cognitive_loop as c
    # Loopback / localhost = on THIS GPU.
    assert c._is_local_llm_url("http://127.0.0.1:1234/v1/messages") is True
    assert c._is_local_llm_url("http://localhost:11434") is True
    assert c._is_local_llm_url("http://[::1]:8080") is True
    # Cloud / frontier = GPU load here is irrelevant.
    assert c._is_local_llm_url("https://api.anthropic.com/v1/messages") is False
    assert c._is_local_llm_url("https://generativelanguage.googleapis.com") is False
    # A LAN box is remote (not on THIS GPU).
    assert c._is_local_llm_url("http://gpu-box.local:8000") is False
    # Unknown / empty -> assume local (safe: prefer over-throttle).
    assert c._is_local_llm_url(None) is True
    assert c._is_local_llm_url("") is True


def test_pace_for_pass_resource_scoping():
    import m3_cognitive_loop as c
    cpu_ram = {"background": "CONTINUOUS"}
    full = {"background": "THROTTLED"}  # GPU pushed it to THROTTLED
    # Local-GPU pass obeys the GPU-inclusive (stricter) verdict.
    assert c._pace_for_pass(cpu_ram, full, uses_local_gpu=True)["background"] == "THROTTLED"
    # Cloud / SQL pass ignores GPU -> obeys CPU/RAM-only verdict.
    assert c._pace_for_pass(cpu_ram, full, uses_local_gpu=False)["background"] == "CONTINUOUS"
    # When CPU/RAM itself is high, BOTH kinds of pass are gated.
    cpu_ram_hot = {"background": "THROTTLED"}
    assert c._pace_for_pass(cpu_ram_hot, full, uses_local_gpu=False)["background"] == "THROTTLED"


def test_probe_gpu_util_nvidia_parses_busiest_and_caches(monkeypatch):
    # Drive the real nvidia backend: multi-GPU output, busiest card wins, then
    # the TTL cache prevents a second spawn.
    _fresh_probe_cache(monkeypatch)
    import subprocess

    class _R:
        returncode = 0
        stdout = "23\n91\n"  # two GPUs; busiest is 91

    calls = []

    def _fake_run(cmd, *a, **k):
        # Only nvidia-smi should be invoked (it answers first).
        assert cmd[0] == "nvidia-smi"
        calls.append(1)
        return _R()
    monkeypatch.setattr(subprocess, "run", _fake_run)

    assert m3_sdk.probe_gpu_util(now=1000.0) == 91.0
    assert m3_sdk._gpu_probe_cache["backend"] == "nvidia"
    # Within TTL -> cached, no second spawn.
    assert m3_sdk.probe_gpu_util(now=1000.5) == 91.0
    assert len(calls) == 1
