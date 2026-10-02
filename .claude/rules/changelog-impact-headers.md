---
paths:
  - "CHANGELOG.md"
---

# Changelog entries carry an impact header

Every bullet in a release section **above** the `impact-headers` marker ends with
one footer line:

```
  **Affected:** <platforms / configurations that can reach this> ·
  **Action:** <what an operator must do> · **Data status:** <none | ...>
```

`Data status` is mandatory and `none` must be stated explicitly — an explicit
"none" distinguishes *verified no data impact* from *nobody looked*, and for a
memory system that is the question an operator actually needs answered.

Entries **below** the marker predate the convention and are deliberately not
backfilled: writing a scope or data-status claim for a historical entry means
asserting something nobody re-verified, and a confident header that is wrong is
worse than no header.

`tests/test_changelog_impact_headers.py` enforces this above the marker only.
Do not move the marker to silence it, and do not delete it — a marker at the top
of the file makes every one of those tests pass vacuously.

Keep forensic detail OUT of the public changelog: the public entry states impact
and action, the causal chain goes in the private expanded changelog. Root cause
is often provisional; user impact usually is not.
