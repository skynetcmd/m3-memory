"""Tests for bin/temporal_utils.resolve_temporal_expressions.

Covers the quantifier and "this <period>" handling added 2026-06:
  - article quantifier:  "a month ago", "an hour"-class
  - spelled-out numbers: "two months ago", "a couple of weeks ago", "few"
  - "this week/month/year/weekend" -> anchor date
  - no regression on bare-digit and last/next forms
The anchor is supplied as a datetime so tests don't depend on a registered
dataset parser or on datetime.now().
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))
import temporal_utils as tu  # noqa: E402

# A Sunday, so weekend/weekday math is deterministic.
ANCHOR = datetime(2023, 5, 28, 9, 2)


def _resolve(text):
    """Return {ref: absolute} for easy assertions."""
    return {r["ref"]: r["absolute"] for r in tu.resolve_temporal_expressions(text, ANCHOR)}


def test_article_quantifier():
    assert _resolve("I got it a month ago")["a month ago"] == "2023-04-28"
    assert _resolve("a week ago")["a week ago"] == "2023-05-21"
    assert _resolve("a year ago")["a year ago"] == "2022-05-28"


def test_spelled_out_numbers():
    assert _resolve("two months ago")["two months ago"] == "2023-03-29"
    assert _resolve("three years ago")["three years ago"] == "2020-05-28"


def test_couple_and_few():
    # couple -> 2, few/several -> 3; "of" is absorbed. The matched ref begins at
    # the quantifier word (a leading article "a" is outside the match); we assert
    # on the resolved absolute date, which is what downstream consumers use.
    assert _resolve("a couple of weeks ago")["couple of weeks ago"] == "2023-05-14"
    assert _resolve("a few days ago")["few days ago"] == "2023-05-25"
    assert _resolve("several months ago")["several months ago"] == "2023-02-27"


def test_this_period_resolves_to_anchor():
    iso = ANCHOR.date().isoformat()
    for phrase in ("this week", "this month", "this year", "this weekend"):
        assert _resolve(phrase)[phrase] == iso


def test_last_and_next_units():
    r = _resolve("last week and next week and last month and next month")
    assert r["last week"] == "2023-05-21"
    assert r["next week"] == "2023-06-04"
    assert r["last month"] == "2023-04-28"
    assert r["next month"] == "2023-06-27"


def test_bare_digit_no_regression():
    r = _resolve("it was 3 months ago and 10 days ago")
    assert r["3 months ago"] == "2023-02-27"
    assert r["10 days ago"] == "2023-05-18"


def test_simple_relative_no_regression():
    r = _resolve("yesterday and today and tomorrow")
    assert r["yesterday"] == "2023-05-27"
    assert r["today"] == "2023-05-28"
    assert r["tomorrow"] == "2023-05-29"


def test_last_weekday_no_regression():
    assert _resolve("we met last Friday")["last friday"] == "2023-05-26"


def test_vague_expressions_do_not_resolve():
    # These have no precise date; better to miss than fabricate.
    assert _resolve("the other day") == {}
    assert _resolve("over the weekend") == {}
