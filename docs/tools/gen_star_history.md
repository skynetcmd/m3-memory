---
tool: bin/gen_star_history.py
sha1: d64c4704e9f6
mtime_utc: 2026-07-15T18:30:53.086967+00:00
generated_utc: 2026-07-17T02:18:40.574864+00:00
private: false
---

# bin/gen_star_history.py

## Purpose

Generate docs/star-history.svg from the GitHub stargazers API.

GitHub now requires an authenticated token to read star data, so the public
star-history.com embed no longer renders anonymously in the README. This script
fetches the repo's stargazer timestamps with a token (the CI GITHUB_TOKEN is
enough — only `Metadata: read` / `contents: read` is needed) and renders a
self-contained cumulative-stars line chart as an SVG committed into the repo.
The README embeds that committed file, so no token is ever exposed publicly and
no third-party service is involved.

Run in CI on a schedule; commit the result back to main. Locally:

    GITHUB_TOKEN=$(gh auth token) python bin/gen_star_history.py         --repo skynetcmd/m3-memory --out docs/star-history.svg

Exit codes: 0 = SVG written (or unchanged), 2 = hard error (auth/network).
Standard library only.

---

## Entry points

- `def main()` (line 144)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--repo` |  | `skynetcmd/m3-memory` |  | str |  |
| `--out` |  | `docs/star-history.svg` |  | str |  |

---

## Environment variables read

- `GH_TOKEN`
- `GITHUB_TOKEN`

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

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
