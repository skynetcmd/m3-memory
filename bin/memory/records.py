"""memory.records — the `as_records` return-shape seam.

P1 of the slim-tools work. Twelve catalog tools hold rows, flatten them into a
display string, and return that. The rows are the data; the string is one
rendering of it. A caller who wants to filter, sort, or re-render has to parse
the string back -- or give up and go around the tool (DESIGN_PHILOSOPHIES
section 12a: a shape that sends the caller elsewhere is the defect).

This module owns the CONTRACT, so twelve call sites do not each invent it:

    payload = as_records_payload(rows, summary="Agents (3):")
    return emit(payload, as_records)

WHY A SHARED HELPER AND NOT TWELVE LOCAL `if as_records:` BLOCKS (section 10a):
a copied predicate is the defect independent of correctness. Twelve hand-rolled
serializations drift -- one forgets the count, one renames `items` to `rows`,
one returns a bare list where its siblings return an object. An agent cannot
reason about a param that means something slightly different on each tool. One
owner, twelve call sites.

## The compatibility rule this module exists to enforce

`as_records=False` (the default) MUST return the display string BYTE-IDENTICAL
to what the tool returned before this param existed. Every current caller
predates the param and parses the string. `emit()` therefore does not touch the
string path at all -- it returns the caller's already-built string unchanged,
rather than re-rendering rows into a string that is merely equivalent. Rebuilding
is how a trailing newline or a column width silently changes under 396 callers.

## Row hygiene

Rows arriving here are usually sqlite3.Row / psycopg Row objects, which are NOT
JSON-serializable and whose repr leaks the backend. `_jsonable()` normalizes to
plain dicts with JSON-safe scalars, so the same tool returns the same records on
SQLite and PostgreSQL (section 0.4 -- a green SQLite-only run proves nothing).
"""
from __future__ import annotations

import datetime
import decimal
import json
from typing import Any, Iterable, Mapping

# The one param name, so the twelve ToolSpecs cannot disagree about spelling.
# Verified against the full catalog: no existing tool takes `as_records`.
PARAM_NAME = "as_records"

# The one description, so the twelve ToolSpecs read identically to an agent
# deciding whether to pass it.
PARAM_SPEC: dict[str, Any] = {
    "type": "boolean",
    "description": (
        "Return structured records (JSON) instead of the display string. "
        "Default false keeps the human-readable output unchanged."
    ),
    "default": False,
}


def _jsonable(value: Any) -> Any:
    """Coerce a DB scalar into something json.dumps can render.

    Backend-neutral on purpose: PostgreSQL hands back datetime/Decimal where
    SQLite hands back str/float for the same column. Without this the SAME tool
    returns different record types per backend -- a portability break that a
    SQLite-only test run would never catch.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        # Opaque blobs (embeddings) have no useful JSON form; report the size
        # rather than emitting megabytes of base64 into an agent's context.
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    return str(value)


def to_records(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """Normalize DB rows (sqlite3.Row, psycopg Row, dict, ...) to plain dicts."""
    out: list[dict[str, Any]] = []
    for row in rows or ():
        if isinstance(row, Mapping):
            out.append({str(k): _jsonable(v) for k, v in row.items()})
            continue
        keys = getattr(row, "keys", None)
        if callable(keys):
            out.append({str(k): _jsonable(row[k]) for k in keys()})
            continue
        if isinstance(row, (list, tuple)):
            out.append({str(i): _jsonable(v) for i, v in enumerate(row)})
            continue
        out.append({"value": _jsonable(row)})
    return out


def as_records_payload(rows: Iterable[Any], **extra: Any) -> dict[str, Any]:
    """Build the standard records envelope.

    Always an OBJECT with `count` and `items`, never a bare array: an envelope
    can gain a field later without breaking a caller that indexes into it, and
    `count` lets a caller check for truncation without walking the list.
    """
    items = to_records(rows)
    payload: dict[str, Any] = {"count": len(items), "items": items}
    for key, value in extra.items():
        if value is not None:
            payload[key] = _jsonable(value)
    return payload


def emit(display: str, payload: Any, as_records: bool) -> str:
    """Return the display string, or the JSON records, per the opt-in flag.

    `display` is the string the tool ALREADY built. It is passed through
    untouched when as_records is false -- see the compatibility rule above.
    Both branches return `str` because the MCP tool contract is string-returning;
    as_records changes the CONTENT (parseable JSON), not the type.
    """
    if not as_records:
        return display
    return json.dumps(payload, indent=2, sort_keys=False, default=str)


def error_payload(error: str, **detail: Any) -> dict[str, Any]:
    """Envelope for a FAILED call: `{error, ...}` with NO count/items.

    An error is not an empty result. `{count: 0, items: []}` would tell a
    caller "the query succeeded and matched nothing" when the truth is "the
    thing you named does not exist" -- the conflation section 3 exists to
    prevent, because the caller cannot then tell a bad id from a valid id with
    no rows.

    The MISSING keys are the point. A caller that lazily reaches for
    `data["items"]` without checking for `error` gets a KeyError, which is the
    correct fail-loud outcome; a superset envelope carrying both would let
    `if not data["items"]` silently swallow a hard error as an empty set.
    """
    payload: dict[str, Any] = {"error": error}
    for key, value in detail.items():
        if value is not None:
            payload[key] = _jsonable(value)
    return payload


def scalar_payload(**fields: Any) -> dict[str, Any]:
    """Envelope for a tool whose result is a summary, not a list.

    memory_cost_report is the one such tool in the P1 set: a dict of totals, not
    rows. It still gets `as_records` so the param means the same thing on every
    tool an agent might reach for -- a flag present on 11 of 12 siblings is a
    surface that cannot be reasoned about without special-casing.
    """
    return {k: _jsonable(v) for k, v in fields.items()}
