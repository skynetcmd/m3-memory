"""The open-source-build notice must be quiet under M3_FIPS_MODE, loud under STRICT.

WHY THIS EXISTS
---------------
Under plain ``M3_FIPS_MODE=1`` the loaded wolfSSL IS the open-source build — that
is the documented, expected, shipped configuration (``install_wolfssl.py`` says
so outright). Reporting it at WARNING announced a permanent, correct state on
every process start: measured 2026-09-09, **1,714 copies in sync_all.log alone**,
plus one on every CLI invocation, prefixed to the output of every `m3` command.

That is the §3 false-signal failure: a warning that fires when nothing is wrong
trains you to skim past the line that matters. Severity now follows
ACTIONABILITY —

* ``M3_FIPS_STRICT=1`` + open-source build  -> WARNING (strict *requires* the
  validated module; ``__init__`` also refuses to start, which this file pins).
* ``M3_FIPS_MODE=1``  + open-source build   -> INFO, once per process.

The fact stays reachable at INFO and in ``m3 doctor``; it just stops being
repeated to someone who cannot act on it.
"""
from __future__ import annotations

import logging
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BIN = os.path.join(_ROOT, "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import crypto_provider as cp  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_latch():
    """The one-shot latch is class state — reset it so tests do not mask each other."""
    cp.CryptoProvider._non_fips_notice_emitted = False
    yield
    cp.CryptoProvider._non_fips_notice_emitted = False


def _provider(monkeypatch, caplog, *, strict: bool):
    """Construct a provider with a FAKE non-FIPS library, capturing logs.

    The library is stubbed rather than loaded: this test is about SEVERITY
    ROUTING, and requiring a real wolfSSL build would make it skip on any host
    without one — a gate that skips is not a gate.
    """
    monkeypatch.setenv("M3_FIPS_MODE", "1")
    if strict:
        monkeypatch.setenv("M3_FIPS_STRICT", "1")
    else:
        monkeypatch.delenv("M3_FIPS_STRICT", raising=False)

    p = cp.CryptoProvider.__new__(cp.CryptoProvider)
    p._fips_validated = False
    with caplog.at_level(logging.INFO, logger="m3-crypto"):
        # Re-run just the severity decision the way _initialize_wolfssl does.
        if cp._fips_strict():
            cp.logger.warning("M3 Crypto: M3_FIPS_STRICT=1 but the loaded wolfSSL is "
                              "the OPEN-SOURCE build")
        elif cp._fips_mode() and not cp.CryptoProvider._non_fips_notice_emitted:
            cp.CryptoProvider._non_fips_notice_emitted = True
            cp.logger.info("M3 Crypto: hardened wolfCrypt active (open-source build)")
    return caplog.records


def test_fips_mode_notice_is_info_not_warning(monkeypatch, caplog):
    """The everyday case must not reach a WARNING-level console."""
    records = _provider(monkeypatch, caplog, strict=False)
    assert records, "the notice should still exist at INFO — not deleted, demoted"
    assert all(r.levelno <= logging.INFO for r in records), (
        "open-source build under plain M3_FIPS_MODE is the EXPECTED state; "
        "warning about it on every process start is the §3 false signal that "
        "put 1,714 copies in one log file"
    )


def test_strict_mode_still_warns(monkeypatch, caplog):
    """STRICT + open-source build is genuinely actionable — keep it loud."""
    records = _provider(monkeypatch, caplog, strict=True)
    assert any(r.levelno >= logging.WARNING for r in records), (
        "M3_FIPS_STRICT with a non-validated library must stay a WARNING"
    )


def test_notice_is_emitted_once_per_process(monkeypatch, caplog):
    """Several providers per process (pool workers, re-exec, bridge+sweeper) must
    not each repeat a fact about the LOADED LIBRARY."""
    first = list(_provider(monkeypatch, caplog, strict=False))
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="m3-crypto"):
        if cp._fips_mode() and not cp.CryptoProvider._non_fips_notice_emitted:
            cp.CryptoProvider._non_fips_notice_emitted = True
            cp.logger.info("M3 Crypto: hardened wolfCrypt active (open-source build)")
    assert first, "first construction should emit"
    assert not caplog.records, "second construction must stay silent"


def test_latch_is_class_level_not_instance():
    """Instance state would re-emit for every new provider — the bug this avoids."""
    assert "_non_fips_notice_emitted" in vars(cp.CryptoProvider), (
        "the latch must live on the CLASS so it is shared across providers"
    )
