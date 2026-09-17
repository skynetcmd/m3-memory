---
tool: bin/news_fetcher.py
sha1: de8a471f7d37
mtime_utc: 2026-09-17T16:09:13.613226+00:00
generated_utc: 2026-09-17T16:21:39.870838+00:00
private: false
---

# bin/news_fetcher.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `NEWS_API_KEY`

---

## Calls INTO this repo (intra-repo imports)

- `mcp_compat (FastMCP)`

---

## Calls OUT (external side-channels)

**http**

- `requests.get()  → `NEWS_API_URL`` (line 53)


---

## Notable external imports

- `requests`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
