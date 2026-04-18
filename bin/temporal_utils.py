"""
Enhanced temporal resolution utility for m3-memory.
Resolves relative date expressions (yesterday, last Friday, the Sunday before June 1st)
into absolute ISO-8601 dates based on an anchor timestamp.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

DAYS_OF_WEEK = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6
}

MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december"
]

def parse_generic_date(text: str) -> datetime | None:
    """Tries to parse a date from a string like 'May 25, 2023' or '25 May 2023'."""
    text = text.lower().strip()

    # 1. 'May 25, 2023' or 'May 25 2023'
    match = re.search(r"([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,)?\s+(\d{4})", text)
    if match:
        month_name, day, year = match.groups()
        if month_name in MONTHS:
            return datetime(int(year), MONTHS.index(month_name) + 1, int(day))

    # 2. '25 May 2023'
    match = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)\s+(\d{4})", text)
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
        anchor = parse_locomo_date(anchor_date) or parse_longmemeval_date(anchor_date) or datetime.now()
    else:
        anchor = anchor_date

    results = []
    text_lower = text.lower()

    # 1. Complex: "the [weekday] before/after [Date]"
    # e.g. "the Sunday before 25 May 2023"
    complex_pattern = r"the\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+(before|after)\s+([^,.?!]+)"
    for match in re.finditer(complex_pattern, text_lower):
        weekday, direction, date_part = match.groups()
        base_date = parse_generic_date(date_part)
        if base_date:
            resolved = resolve_weekday_relative(weekday, base_date, direction)
            results.append({
                "ref": match.group(0),
                "absolute": resolved.date().isoformat()
            })

    # 2. Simple relative: "yesterday", "today", "tomorrow", "recently"
    simple_rel = [
        (r"\byesterday\b", -1),
        (r"\btoday\b", 0),
        (r"\btomorrow\b", 1),
        (r"\brecently\b", 0), # Map recently to roughly today
    ]
    for pattern, days in simple_rel:
        if re.search(pattern, text_lower):
            resolved = anchor + timedelta(days=days)
            results.append({
                "ref": re.search(pattern, text_lower).group(0),
                "absolute": resolved.date().isoformat()
            })

    # 3. Numeric relative: "N days/weeks/months/years ago"
    numeric_patterns = [
        (r"\b(\d+)\s+days?\s+ago\b", lambda d: timedelta(days=int(d))),
        (r"\b(\d+)\s+weeks?\s+ago\b", lambda w: timedelta(weeks=int(w))),
        (r"\b(\d+)\s+months?\s+ago\b", lambda m: timedelta(days=int(m)*30)),
        (r"\b(\d+)\s+years?\s+ago\b", lambda y: timedelta(days=int(y)*365)),
    ]
    for pattern, delta_fn in numeric_patterns:
        for match in re.finditer(pattern, text_lower):
            resolved = anchor - delta_fn(match.group(1))
            results.append({
                "ref": match.group(0),
                "absolute": resolved.date().isoformat()
            })

    # 4. "last [weekday]"
    weekday_pattern = r"\blast\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
    for match in re.finditer(weekday_pattern, text_lower):
        resolved = resolve_weekday_relative(match.group(1), anchor, "before")
        results.append({
            "ref": match.group(0),
            "absolute": resolved.date().isoformat()
        })

    # 5. "last weekend", "last week", "next month", "last month"
    static_rel = [
        (r"\blast\s+weekend\b", lambda a: a - timedelta(days=a.weekday() + 2)),
        (r"\blast\s+week\b", lambda a: a - timedelta(days=7)),
        (r"\bnext\s+month\b", lambda a: a + timedelta(days=30)),
        (r"\blast\s+month\b", lambda a: a - timedelta(days=30)),
    ]
    for pattern, resolver in static_rel:
        if re.search(pattern, text_lower):
            resolved = resolver(anchor)
            results.append({
                "ref": re.search(pattern, text_lower).group(0),
                "absolute": resolved.date().isoformat()
            })

    return results

def parse_locomo_date(date_str: str) -> datetime | None:
    """Parses LOCOMO date format like '1:56 pm on 8 May, 2023'"""
    try:
        match = re.search(r"(\d+):(\d+)\s+(am|pm)\s+on\s+(\d+)\s+([A-Za-z]+),\s+(\d+)", date_str)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2))
            meridiem = match.group(3).lower()
            day = int(match.group(4))
            month_name = match.group(5).lower()
            year = int(match.group(6))

            if meridiem == "pm" and hour < 12:
                hour += 12
            if meridiem == "am" and hour == 12:
                hour = 0

            month = MONTHS.index(month_name) + 1
            return datetime(year, month, day, hour, minute)
    except Exception:
        pass
    return None

def parse_longmemeval_date(date_str: str) -> datetime | None:
    """Parses LongMemEval date format like '2023/05/20 (Sat) 02:21'"""
    try:
        match = re.search(r"(\d{4})/(\d{2})/(\d{2})\s+\([A-Za-z]+\)\s+(\d{2}):(\d{2})", date_str)
        if match:
            return datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3)),
                int(match.group(4)), int(match.group(5)),
            )
    except Exception:
        pass
    return None

if __name__ == "__main__":
    # Test cases
    anchor = parse_locomo_date("1:14 pm on 25 May, 2023")
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
