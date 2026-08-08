---
tool: bin/promote_pipeline.py
sha1: d729811b79ce
mtime_utc: 2026-08-08T03:47:37.162304+00:00
generated_utc: 2026-08-08T14:40:50.070202+00:00
private: false
---

# bin/promote_pipeline.py

## Purpose

LLM-judged promotion pipeline: tightened candidate selection + SLM judge.

Stage 1 (--select): high-precision candidate selection (~1-2k) from chatlog.
Stage 2 (--smoke N / --run): batched judge via LM Studio; distill PROMOTE
         items to crisp facts. Writes accepted to --out jsonl.

---

## Entry points

- `def main()` (line 133)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--db` |  | — |  | str |  |
| `--select` |  | `False` |  | store_true |  |
| `--smoke` |  | `0` |  | int |  |
| `--run` |  | `False` |  | store_true |  |
| `--batch` |  | `8` |  | int |  |
| `--out` |  | `os.path.join(tempfile.gettempdir(), 'promote_accepted.jsonl')` |  | str |  |
| `--samples` |  | `8` |  | int |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (get_secret, getenv_compat)`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `db`` (line 54)


---

## Notable external imports

- `importlib.util`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
