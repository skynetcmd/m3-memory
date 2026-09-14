"""Tests for the `as_records` return-shape seam (P1).

The contract this file pins:

  1. as_records=False returns the display string UNTOUCHED. Every caller that
     predates the param parses that string; emit() must not re-render it.
  2. as_records=True returns parseable JSON in a stable envelope.
  3. The empty case honours the flag -- opting into records and getting a
     display string back because the result set was empty is the branch a
     caller's parser forgets.
  4. Records are backend-neutral (section 0.4): PostgreSQL hands back
     datetime/Decimal where SQLite hands back str/float, and the SAME tool must
     not emit different record types per backend.

Hermetic: no DB, no network. Row objects are faked to the shapes the backends
actually return.
"""
from __future__ import annotations

import datetime
import decimal
import json
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from memory import records  # noqa: E402


class _Row:
    """Stand-in for sqlite3.Row / psycopg Row: keys() + __getitem__, not a dict."""

    def __init__(self, **kw):
        self._d = kw

    def keys(self):
        return self._d.keys()

    def __getitem__(self, k):
        return self._d[k]


# ── rule 1: the default path is byte-identical ───────────────────────────────
def test_default_returns_the_display_string_untouched():
    display = "Agents (2):\n  [a] role=x\n  [b] role=y"
    out = records.emit(display, records.as_records_payload([]), False)
    assert out == display, "emit() must not re-render the string path"


def test_default_ignores_the_payload_entirely():
    """Even a payload that would raise on serialization must not affect the
    string path -- the default branch may not depend on record building."""
    class Boom:
        def __repr__(self):
            raise AssertionError("payload was touched on the default path")

    assert records.emit("hi", {"items": [Boom()]}, False) == "hi"


# ── rule 2: the records path is parseable and stably shaped ──────────────────
def test_as_records_returns_parseable_json_envelope():
    rows = [_Row(agent_id="a", role="r"), _Row(agent_id="b", role="s")]
    out = records.emit("display", records.as_records_payload(rows), True)
    data = json.loads(out)
    assert data["count"] == 2
    assert data["items"][0] == {"agent_id": "a", "role": "r"}


def test_envelope_is_an_object_not_a_bare_array():
    """An envelope can gain a field later without breaking a caller that
    indexes into it; a bare array cannot."""
    data = json.loads(records.emit("d", records.as_records_payload([_Row(x=1)]), True))
    assert isinstance(data, dict)
    assert set(data) >= {"count", "items"}


def test_extra_fields_ride_the_envelope():
    payload = records.as_records_payload([_Row(x=1)], conversation_id="c1", truncated=True)
    data = json.loads(records.emit("d", payload, True))
    assert data["conversation_id"] == "c1"
    assert data["truncated"] is True


def test_none_extras_are_dropped_not_serialized_as_null():
    payload = records.as_records_payload([], cursor=None)
    assert "cursor" not in payload


# ── rule 3: the empty case honours the flag ──────────────────────────────────
def test_empty_result_still_returns_records_when_opted_in():
    out = records.emit("(no agents)", records.as_records_payload(()), True)
    data = json.loads(out)
    assert data == {"count": 0, "items": []}


def test_empty_result_returns_the_display_string_by_default():
    assert records.emit("(no agents)", records.as_records_payload(()), False) == "(no agents)"


# ── rule 4: backend neutrality (section 0.4) ─────────────────────────────────
def test_datetime_normalizes_to_iso_string():
    """PG returns datetime; SQLite returns an ISO string for the same column."""
    dt = datetime.datetime(2026, 9, 14, 3, 8, 23, tzinfo=datetime.timezone.utc)
    got = records.to_records([_Row(created_at=dt)])[0]["created_at"]
    assert got == dt.isoformat()
    assert isinstance(got, str)


def test_decimal_normalizes_to_float():
    """PG NUMERIC arrives as Decimal; SQLite gives a float."""
    got = records.to_records([_Row(cost=decimal.Decimal("1.25"))])[0]["cost"]
    assert got == 1.25
    assert isinstance(got, float)


def test_blobs_report_size_rather_than_dumping_base64():
    """An embedding blob has no useful JSON form and would flood an agent's
    context."""
    got = records.to_records([_Row(vec=b"\x00" * 4096)])[0]["vec"]
    assert got == "<4096 bytes>"


def test_records_are_json_serializable_for_every_supported_scalar():
    row = _Row(
        s="text", i=1, f=1.5, b=True, n=None,
        dt=datetime.datetime(2026, 1, 1),
        d=decimal.Decimal("2.5"),
        blob=b"xx",
        nested={"k": [1, decimal.Decimal("3")]},
    )
    json.dumps(records.as_records_payload([row]))  # must not raise


def test_plain_dict_rows_work():
    assert records.to_records([{"a": 1}]) == [{"a": 1}]


def test_tuple_rows_are_positionally_keyed():
    assert records.to_records([("x", "y")]) == [{"0": "x", "1": "y"}]


# ── the one spelling, shared by all 12 ToolSpecs (section 10a) ───────────────
def test_param_name_and_spec_are_single_owner():
    assert records.PARAM_NAME == "as_records"
    assert records.PARAM_SPEC["type"] == "boolean"
    assert records.PARAM_SPEC["default"] is False


def test_scalar_payload_normalizes_like_rows():
    out = records.scalar_payload(total=decimal.Decimal("7"), when=datetime.date(2026, 9, 14))
    assert out == {"total": 7.0, "when": "2026-09-14"}


# ── errors are NOT empty results ────────────────────────────────────────────
def test_error_envelope_has_no_count_or_items():
    """The missing keys are the contract. A caller that lazily reaches for
    data['items'] without checking for 'error' must get a KeyError -- that is
    the correct fail-loud outcome, not a silent empty set."""
    data = json.loads(records.emit("Error: nope",
                                   records.error_payload("not_found", memory_id="x"), True))
    assert data["error"] == "not_found"
    assert "count" not in data
    assert "items" not in data
    with pytest.raises(KeyError):
        _ = data["items"]


def test_error_is_distinguishable_from_an_empty_result():
    """{count:0,items:[]} means 'matched nothing'; {error:...} means 'the thing
    you named does not exist'. Conflating them is the section 3 silent failure."""
    empty = json.loads(records.emit("(none)", records.as_records_payload([]), True))
    err = json.loads(records.emit("Error", records.error_payload("not_found"), True))
    assert empty["count"] == 0 and "error" not in empty
    assert "error" in err and "count" not in err


def test_error_path_still_returns_the_string_by_default():
    assert records.emit("Error: nope", records.error_payload("not_found"), False) == "Error: nope"


def test_error_detail_fields_are_normalized():
    data = records.error_payload("bad", memory_id="m1", when=datetime.date(2026, 9, 14), skip=None)
    assert data == {"error": "bad", "memory_id": "m1", "when": "2026-09-14"}


@pytest.mark.parametrize("flag", [False, True])
def test_emit_always_returns_str(flag):
    """as_records changes the CONTENT, not the return type -- the MCP tool
    contract is string-returning on both paths."""
    assert isinstance(records.emit("d", records.as_records_payload([]), flag), str)
