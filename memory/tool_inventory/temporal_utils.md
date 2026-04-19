---
tool: bin/temporal_utils.py
sha1: a5d46393391d
mtime_utc: 2026-04-18T03:45:31.264360+00:00
generated_utc: 2026-04-18T05:16:53.226111+00:00
private: false
---

# bin/temporal_utils.py

## Purpose

Resolves temporal anchors ("yesterday", "last week" → YYYY-MM-DD). Used as library module for temporal-anchor prefix resolution in embed_text. Converts relative date expressions into absolute ISO-8601 dates.

## Entry points / Public API

- `resolve_temporal_expressions(text, anchor_date)` (line 63) — Extracts and resolves temporal expressions; returns list of {ref, absolute} dicts. Anchor can be datetime or string.
- `parse_generic_date(text)` (line 21) — Parses "May 25, 2023", "25 May 2023", or ISO format.
- `resolve_weekday_relative(weekday_name, base_date, direction)` (line 47) — Finds nearest weekday before/after base_date.
- `parse_locomo_date(date_str)` (line 145) — Parses LOCOMO format "1:56 pm on 8 May, 2023".
- `parse_longmemeval_date(date_str)` (line 168) — Parses LongMemEval format "2023/05/20 (Sat) 02:21".

## CLI flags / arguments

_(no CLI surface — invoked as library module)_

## Environment variables read

_(none)_

## Calls INTO this repo (intra-repo imports)

_(none)_

## Calls OUT (external side-channels)

_(none — pure stdlib, no subprocess/HTTP/file I/O)_

## File dependencies

_(none — self-contained)_

## Re-validation

If `sha1` differs from current file, re-read and regenerate via `python bin/gen_tool_inventory.py`.
