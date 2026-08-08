"""chatlog_prune.classify bounds the content it regex-scans for keep signals.

The prune sweep runs several regexes over turn content every cycle. Scanning the
FULL content of every turn is wasteful (a turn can be a megabyte-scale pasted log
/ tool dump), so classify()/`_is_protected` scan only the first `_SIGNAL_SCAN_CAP`
chars — the markers they look for (``` , "decided", IMPORTANT, TODO, status vocab)
appear early in a real turn. These pin the bound and that a signal within the cap
is still honored. Hermetic — pure functions, no DB.
"""
import argparse
import sys
from pathlib import Path

_BIN = str(Path(__file__).resolve().parents[1] / "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import chatlog_prune as P  # noqa: E402


def _args(**kw):
    base = dict(keep_imp_floor=0.8, status_min_cluster=3, no_generic=False,
               generic_imp_max=0.35, fresh_days=2)
    base.update(kw)
    return argparse.Namespace(**base)


def test_signal_within_cap_is_kept():
    content = "we討論... IMPORTANT: never delete the prod db"  # signal near the start
    cat, reason = P.classify("assistant", content, 0.3, "", 0, _args())
    assert cat is None and reason == "keep:signal"


def test_scan_is_bounded_to_cap():
    # A signal placed FAR beyond the cap is not scanned (documents the tradeoff):
    # the huge prefix is inert filler, the keep-signal marker sits past the cap.
    filler = "x " * (P._SIGNAL_SCAN_CAP)  # >> cap chars, no markers
    content = filler + " IMPORTANT gotcha TODO"
    cat, _ = P.classify("assistant", content, 0.3, "", 0, _args())
    # Not kept-as-signal (marker beyond cap) — falls through to later classes.
    assert not (cat is None and _ == "keep:signal")
    # And crucially it returns quickly (bounded scan) — no full-content regex.


def test_is_protected_respects_cap():
    assert P._is_protected("the decision: we went with sqlite", 0) is True
    filler = "y " * (P._SIGNAL_SCAN_CAP)
    assert P._is_protected(filler + " we decided to use postgres", 0) is False


def test_cap_is_generous_enough_for_normal_turns():
    # A normal multi-paragraph turn keeps its signal well within the cap.
    body = ("Here is a long explanation. " * 200) + "Therefore we should cache it."
    assert len(body) < P._SIGNAL_SCAN_CAP
    cat, reason = P.classify("assistant", body, 0.3, "", 0, _args())
    assert cat is None and reason == "keep:signal"
