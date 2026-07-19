---
tool: bin/gen_download_badges.py
sha1: dcb7dc374c1e
mtime_utc: 2026-07-19T06:31:23.169257+00:00
generated_utc: 2026-07-19T19:29:22.243790+00:00
private: false
---

# bin/gen_download_badges.py

## Purpose

Generate download-count badge data (PyPI + GitHub) for the README.

Writes two shields.io *endpoint* JSON files that the README badges point at:

  * docs/badges/pypi-downloads.json  — estimated TOTAL PyPI downloads
        (pypistats "overall", WITHOUT mirrors — excludes bandersnatch/CI mirror
        bots, so it approximates real installs). pypistats keeps ~180 days; for a
        package younger than that this is effectively all-time.
  * docs/badges/github-clones.json   — TOTAL unique GitHub clones
        (the repo traffic API's rolling 14-day `uniques`). GitHub only retains a
        14-day window, so a running total is accumulated in
        docs/badges/clone-history.json across scheduled runs (dedup by day).

These are ESTIMATES by design — download/clone counts include automation and
can't be deduplicated to people; the numbers are labelled accordingly and the
mirror-excluded / unique variants are chosen to be the least-noisy signal.

The README references the JSON via a shields endpoint URL, e.g.:
    https://img.shields.io/endpoint?url=<raw json url>&style=flat-square
so shields renders the current committed number with no third-party number source.

Run in CI on a schedule; commit the result back. Locally:
    GITHUB_TOKEN=$(gh auth token) python bin/gen_download_badges.py         --repo skynetcmd/m3-memory --package m3-memory

Exit codes: 0 = badges written (or unchanged); 2 = hard error (network/auth).
Standard library only.

---

## Entry points

- `def main()` (line 192)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--repo` | owner/name | `skynetcmd/m3-memory` |  | str |  |
| `--package` | PyPI package name | `m3-memory` |  | str |  |
| `--badges-dir` | output directory | `docs/badges` |  | str |  |

---

## Environment variables read

- `GITHUB_TOKEN`
- `M3_STARGAZER`

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `clone-history.json`
- `github-clones.json`
- `pypi-downloads.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
