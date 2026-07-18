"""The governor gates RAM on ABSOLUTE free memory, not percent.

Percent-of-RAM is the wrong unit: 90% of a 32 GiB host (~3 GiB free) is fine,
90% of an 8 GiB host (~0.8 GiB free) is critical — what matters is absolute free
bytes before the next allocation OOMs. So RAM is gated on ram_available_gb via an
idle-aware ladder (HALT < 1 GiB; THROTTLE < 4 GiB active / < 2 GiB idle-30min),
NOT folded into the cpu/gpu percent scalar.

These tests force the pure-Python path (M3_CORE_RS_DISABLE) and drive
_LAST_USER_INTERACTION directly, so they are hermetic and independent of the
host's real RAM/CPU/GPU.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))


@pytest.fixture()
def gov(monkeypatch):
    """The governor module with the native fast-path disabled (pure-Python ladder)
    and clean RAM-threshold env. Restores globals on teardown."""
    monkeypatch.setenv("M3_CORE_RS_DISABLE", "1")
    for v in ("M3_GOVERNOR_RAM_HALT_GB", "M3_GOVERNOR_RAM_THROTTLE_GB",
              "M3_GOVERNOR_RAM_THROTTLE_IDLE_GB"):
        monkeypatch.delenv(v, raising=False)
    import m3_core.governor as g
    return g


def _pace(g, free_gb, idle_s, cpu=5.0, gpu=0.0):
    g._LAST_USER_INTERACTION = time.time() - idle_s
    tel = {"cpu_total": cpu, "gpu_total": gpu,
           "ram_available_gb": free_gb, "ram_total": 92.0}
    return g.get_governor_pacing(tel)["background"]


# ── the core bug: large-but-full host must NOT halt on percent ────────────────
def test_large_host_high_percent_but_free_ram_does_not_halt(gov):
    """32 GiB host at 92% used (~2.5 GiB free), cpu/gpu idle: the OLD percent gate
    (max(cpu,ram%,gpu) with ram%=92) HALTED. With absolute gating, 2.5 GiB free is
    above the 2 GiB idle buffer, so an idle host runs CONTINUOUS."""
    assert _pace(gov, free_gb=2.5, idle_s=3600) == "CONTINUOUS"


def test_starved_small_host_halts(gov):
    """< 1 GiB free HALTs regardless of idle — the floor is idle-independent."""
    assert _pace(gov, free_gb=0.5, idle_s=3600) == "HALTED"
    assert _pace(gov, free_gb=0.5, idle_s=10) == "HALTED"


# ── idle-aware throttle buffer (4 GiB active / 2 GiB idle) ────────────────────
def test_active_user_uses_generous_4gb_buffer(gov):
    """Active user (past the 30s foreground gate, under 30min): 3 GiB free is below
    the 4 GiB active buffer -> THROTTLED, leaving headroom for foreground apps."""
    assert _pace(gov, free_gb=3.0, idle_s=300) == "THROTTLED"


def test_idle_user_relaxes_to_2gb_buffer(gov):
    """Idle > 30 min: the host is the agent's to use, so the buffer tightens to
    2 GiB — 3 GiB free now runs CONTINUOUS."""
    assert _pace(gov, free_gb=3.0, idle_s=2400) == "CONTINUOUS"


def test_idle_still_throttles_below_idle_buffer(gov):
    """Even idle, below the 2 GiB idle buffer still THROTTLES."""
    assert _pace(gov, free_gb=1.5, idle_s=2400) == "THROTTLED"


def test_plenty_of_free_ram_is_continuous(gov):
    assert _pace(gov, free_gb=8.0, idle_s=300) == "CONTINUOUS"


# ── fail-open + resource independence ─────────────────────────────────────────
def test_unknown_free_ram_does_not_gate(gov):
    """ram_available_gb == 0.0 means the probe couldn't read it — fail open, don't
    gate on RAM (cpu/gpu still protect the host)."""
    assert _pace(gov, free_gb=0.0, idle_s=3600, cpu=5.0) == "CONTINUOUS"


def test_cpu_saturation_still_halts_with_plenty_ram(gov):
    """RAM gating is additive, not a replacement: a saturated CPU still HALTs even
    with abundant free RAM."""
    assert _pace(gov, free_gb=16.0, idle_s=3600, cpu=99.0) == "HALTED"


def test_ram_throttle_wins_over_idle_continuous(gov):
    """Worst-wins: an idle host with a cpu/gpu 'CONTINUOUS' verdict is still
    THROTTLED when free RAM is below the active buffer."""
    assert _pace(gov, free_gb=3.0, idle_s=300, cpu=1.0, gpu=0.0) == "THROTTLED"


# ── env override ──────────────────────────────────────────────────────────────
def test_env_overrides_thresholds(gov, monkeypatch):
    """Thresholds are configurable for smaller/larger hosts."""
    monkeypatch.setenv("M3_GOVERNOR_RAM_HALT_GB", "0.5")
    monkeypatch.setenv("M3_GOVERNOR_RAM_THROTTLE_GB", "8.0")
    # 6 GiB free is now below the raised 8 GiB active buffer -> THROTTLED
    assert _pace(gov, free_gb=6.0, idle_s=300) == "THROTTLED"
    # 0.7 GiB free is above the lowered 0.5 GiB halt floor -> not HALTED
    assert _pace(gov, free_gb=0.7, idle_s=3600) != "HALTED"
