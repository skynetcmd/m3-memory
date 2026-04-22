"""
Enhanced temporal resolution utility for m3-memory.
Resolves relative date expressions (yesterday, last Friday, the Sunday before June 1st)
into absolute ISO-8601 dates based on an anchor timestamp.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Callable

# Dataset-specific anchor-date parsers register themselves here (see
# bin/bench_locomo.py and bin/bench_longmemeval.py). Keeps benchmark-format
# knowledge out of this shared module.
_ANCHOR_PARSERS: list[Callable[[str], "datetime | None"]] = []


def register_anchor_parser(parser: Callable[[str], "datetime | None"]) -> None:
    """Register a dataset-specific anchor-date parser."""
    if parser not in _ANCHOR_PARSERS:
        _ANCHOR_PARSERS.append(parser)


def parse_anchor_date(date_str: str) -> "datetime | None":
    """Try each registered dataset parser, return first non-None result."""
    for parser in _ANCHOR_PARSERS:
        result = parser(date_str)
        if result is not None:
            return result
    return None


DAYS_OF_WEEK = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6
}

MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december"
]

# Precompiled patterns used by parse_generic_date and resolve_temporal_expressions.
# Lifted to module scope because these run per-turn at ingest.
_GENERIC_DATE_MONTH_FIRST_RE = re.compile(
    r"([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,)?\s+(\d{4})"
)
_GENERIC_DATE_DAY_FIRST_RE = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)\s+(\d{4})"
)
_COMPLEX_WEEKDAY_REL_RE = re.compile(
    r"the\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+(before|after)\s+([^,.?!]+)"
)
_LAST_WEEKDAY_RE = re.compile(
    r"\blast\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
)
_SIMPLE_REL_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"\byesterday\b"), -1),
    (re.compile(r"\btoday\b"), 0),
    (re.compile(r"\btomorrow\b"), 1),
    (re.compile(r"\brecently\b"), 0),
]
_NUMERIC_REL_PATTERNS: list[tuple[re.Pattern[str], Callable[[str], timedelta]]] = [
    (re.compile(r"\b(\d+)\s+days?\s+ago\b"), lambda d: timedelta(days=int(d))),
    (re.compile(r"\b(\d+)\s+weeks?\s+ago\b"), lambda w: timedelta(weeks=int(w))),
    (re.compile(r"\b(\d+)\s+months?\s+ago\b"), lambda m: timedelta(days=int(m) * 30)),
    (re.compile(r"\b(\d+)\s+years?\s+ago\b"), lambda y: timedelta(days=int(y) * 365)),
]
_STATIC_REL_PATTERNS: list[tuple[re.Pattern[str], Callable[[datetime], datetime]]] = [
    (re.compile(r"\blast\s+weekend\b"), lambda a: a - timedelta(days=a.weekday() + 2)),
    (re.compile(r"\blast\s+week\b"), lambda a: a - timedelta(days=7)),
    (re.compile(r"\bnext\s+month\b"), lambda a: a + timedelta(days=30)),
    (re.compile(r"\blast\s+month\b"), lambda a: a - timedelta(days=30)),
]

# Cap on "N <unit> ago" deltas. timedelta itself raises OverflowError for
# days > ~2.7 million; 100 years is a sane ceiling for memory-item references.
_MAX_RELATIVE_DAYS = 365 * 100

# Temporal-cue detection used by retrieval to decide whether a query needs
# date-aware ranking. Kept narrow — just the common surface forms.
_TEMPORAL_CUE_RES = [
    re.compile(r"\b(when|before|after|how long|timeline|date)\b", re.IGNORECASE),
    re.compile(r"\b(yesterday|today|tomorrow|recently|ago)\b", re.IGNORECASE),
    re.compile(r"\blast\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|week|weekend|month)\b", re.IGNORECASE),
]
_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

def parse_generic_date(text: str) -> datetime | None:
    """Tries to parse a date from a string like 'May 25, 2023' or '25 May 2023'."""
    text = text.lower().strip()

    # 1. 'May 25, 2023' or 'May 25 2023'
    match = _GENERIC_DATE_MONTH_FIRST_RE.search(text)
    if match:
        month_name, day, year = match.groups()
        if month_name in MONTHS:
            return datetime(int(year), MONTHS.index(month_name) + 1, int(day))

    # 2. '25 May 2023'
    match = _GENERIC_DATE_DAY_FIRST_RE.search(text)
    if match:
        day, month_name, year = match.groups()
        if month_name in MONTHS:
            return datetime(int(year), MONTHS.index(month_name) + 1, int(day))

    # 3. '2023-05-25' (ISO)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass

    return None

def resolve_weekday_relative(weekday_name: str, base_date: datetime, direction: str = "before") -> datetime:
    """Finds the nearest [weekday] before or after the base_date."""
    target_weekday = DAYS_OF_WEEK[weekday_name.lower()]
    current_weekday = base_date.weekday()

    if direction == "before":
        days_diff = (current_weekday - target_weekday) % 7
        if days_diff == 0:
            days_diff = 7
        return base_date - timedelta(days=days_diff)
    else: # after
        days_diff = (target_weekday - current_weekday) % 7
        if days_diff == 0:
            days_diff = 7
        return base_date + timedelta(days=days_diff)

def resolve_temporal_expressions(text: str, anchor_date: datetime | str) -> list[dict[str, Any]]:
    """
    Extracts and resolves temporal expressions from text.
    Returns a list of {ref: str, absolute: str}.
    """
    if isinstance(anchor_date, str):
        anchor = parse_anchor_date(anchor_date) or datetime.now()
    else:
        anchor = anchor_date

    results = []
    text_lower = text.lower()

    # 1. Complex: "the [weekday] before/after [Date]"
    for match in _COMPLEX_WEEKDAY_REL_RE.finditer(text_lower):
        weekday, direction, date_part = match.groups()
        base_date = parse_generic_date(date_part)
        if base_date:
            resolved = resolve_weekday_relative(weekday, base_date, direction)
            results.append({
                "ref": match.group(0),
                "absolute": resolved.date().isoformat()
            })

    # 2. Simple relative: "yesterday", "today", "tomorrow", "recently"
    # finditer instead of search so every occurrence contributes a result —
    # "yesterday I did X and yesterday I did Y" was previously collapsing.
    for pattern, days in _SIMPLE_REL_PATTERNS:
        for match in pattern.finditer(text_lower):
            resolved = anchor + timedelta(days=days)
            results.append({
                "ref": match.group(0),
                "absolute": resolved.date().isoformat()
            })

    # 3. Numeric relative: "N days/weeks/months/years ago"
    # Guard against overflow: "99999999999 months ago" would raise
    # OverflowError on timedelta construction. Skip deltas beyond 100 years
    # (pathological input; legitimate memory items don't reference that far
    # back) and swallow OverflowError/ValueError defensively.
    for pattern, delta_fn in _NUMERIC_REL_PATTERNS:
        for match in pattern.finditer(text_lower):
            try:
                delta = delta_fn(match.group(1))
                if abs(delta.days) > _MAX_RELATIVE_DAYS:
                    continue
                resolved = anchor - delta
                results.append({
                    "ref": match.group(0),
                    "absolute": resolved.date().isoformat()
                })
            except (OverflowError, ValueError):
                continue

    # 4. "last [weekday]"
    for match in _LAST_WEEKDAY_RE.finditer(text_lower):
        resolved = resolve_weekday_relative(match.group(1), anchor, "before")
        results.append({
            "ref": match.group(0),
            "absolute": resolved.date().isoformat()
        })

    # 5. "last weekend", "last week", "next month", "last month"
    # Same finditer fix as _SIMPLE_REL_PATTERNS above.
    for pattern, resolver in _STATIC_REL_PATTERNS:
        for match in pattern.finditer(text_lower):
            resolved = resolver(anchor)
            results.append({
                "ref": match.group(0),
                "absolute": resolved.date().isoformat()
            })

    return results


def has_temporal_cues(text: str) -> bool:
    """Fast check: does ``text`` contain any common temporal expression?

    Used by intent-routing and retrieval-side date boosts to decide whether
    a query needs date-aware handling at all. Returns True on the first
    match; no full enumeration.
    """
    return any(cue.search(text) for cue in _TEMPORAL_CUE_RES)


def extract_referenced_dates(text: str) -> list[str]:
    """Return ISO-8601 date substrings (YYYY-MM-DD) referenced in ``text``."""
    return _ISO_DATE_RE.findall(text)


# ── Time-aware retrieval helpers ─────────────────────────────────────────────
# Used by memory_search_scored_impl(smart_time_boost=...) and bench ingest to
# annotate turns with calendar dates they reference, so queries that ask about
# a specific date can match content without oracle metadata.

_MONTH_NAMES = (
    "january|february|march|april|may|june|july|august|september|october|november|december"
    "|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
_DATE_PATTERNS: list[re.Pattern[str]] = [
    # Month-first: "May 7", "May 7th", "May 7 2023", "May 7, 2023".
    # Day must not be immediately followed by more digits (prevents "May 20"
    # from eating the "20" of "May 2023"). Year is optional.
    re.compile(
        rf"({_MONTH_NAMES})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?!\d)(?:\s*,?\s*(\d{{4}}))?",
        re.IGNORECASE,
    ),
    # Day-first: "7 May 2023", "7th May 2023". Year required.
    re.compile(
        rf"(?<!\d)(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_NAMES})\s+(\d{{4}})",
        re.IGNORECASE,
    ),
    re.compile(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})"),
    re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})"),
]

_RELATIVE_TIME_RE = re.compile(
    r"(\d+)\s+(day|week|month|year)s?\s+ago"
    r"|last\s+(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|week|month|year)"
    r"|(\d+)\s+(day|week|month)s?\s+(?:later|after|before|from\s+now)"
    r"|how\s+many\s+(day|week|month|year)s?\s+(?:passed|between|since|ago|have\s+passed)",
    re.IGNORECASE,
)

_TEMPORAL_KEYWORDS = (
    "how many days", "how many weeks", "how many months", "how many years",
    "how long", "when did", "what date", "which came first", "in what order",
    "before or after", "earlier", "later", "first to last", "last to first",
    "chronological", "timeline", "sequence",
)

_MONTH_INDEX: dict[str, int] = {}
for _idx, _name in enumerate(MONTHS, start=1):
    _MONTH_INDEX[_name] = _idx
    _MONTH_INDEX[_name[:3]] = _idx
_MONTH_INDEX["sept"] = 9


def extract_referenced_dates(text: str, default_year: int = 2023) -> list[str]:
    """Pull explicit YYYY-MM-DD dates mentioned in text.

    Used at ingest time to annotate turns with the calendar dates they reference,
    so time-aware retrieval can match query dates against content dates without
    needing oracle metadata. `default_year` is applied only when a month+day
    appears without a year.
    """
    if not text:
        return []
    dates: list[str] = []
    seen: set[str] = set()

    def _emit(y: int, mo: int, d: int) -> None:
        try:
            iso = f"{y:04d}-{mo:02d}-{d:02d}"
            datetime.strptime(iso, "%Y-%m-%d")
        except ValueError:
            return
        if iso not in seen:
            seen.add(iso)
            dates.append(iso)

    for m in _DATE_PATTERNS[0].finditer(text):
        mo = _MONTH_INDEX.get(m.group(1).lower(), 0)
        if not mo:
            continue
        try:
            day = int(m.group(2))
        except (TypeError, ValueError):
            continue
        year = int(m.group(3)) if m.group(3) else default_year
        _emit(year, mo, day)

    for m in _DATE_PATTERNS[1].finditer(text):
        try:
            day = int(m.group(1))
            mo = _MONTH_INDEX.get(m.group(2).lower(), 0)
            if not mo:
                continue
            year = int(m.group(3))
        except (TypeError, ValueError):
            continue
        _emit(year, mo, day)

    for m in _DATE_PATTERNS[2].finditer(text):
        try:
            _emit(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except (TypeError, ValueError):
            continue

    for m in _DATE_PATTERNS[3].finditer(text):
        try:
            _emit(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except (TypeError, ValueError):
            continue

    return dates


def has_temporal_cues(text: str) -> bool:
    """Return True if text likely asks for time-aware reasoning.

    Detects explicit date strings, relative-time expressions, and a
    keyword list covering ordering/duration/when-style phrasing.
    """
    if not text:
        return False
    if _RELATIVE_TIME_RE.search(text):
        return True
    if any(p.search(text) for p in _DATE_PATTERNS):
        return True
    text_lower = text.lower()
    return any(kw in text_lower for kw in _TEMPORAL_KEYWORDS)


if __name__ == "__main__":
    # Test cases
    anchor = datetime(2023, 5, 25, 13, 14)
    print(f"Anchor: {anchor}")

    # Test complex pattern
    test_text = "Melanie ran a charity race on the Sunday before 25 May 2023."
    print(f"Test: {test_text}")
    print(f"Resolved: {resolve_temporal_expressions(test_text, anchor)}")

    # Test simple relative
    print(f"Yesterday: {resolve_temporal_expressions('I went there yesterday', anchor)}")

    # Test last weekday
    print(f"Last Tuesday: {resolve_temporal_expressions('We met last Tuesday', anchor)}")

    # Test numeric
    print(f"3 years ago: {resolve_temporal_expressions('That was 3 years ago', anchor)}")
