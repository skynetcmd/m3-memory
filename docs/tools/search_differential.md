---
tool: bin/search_differential.py
sha1: d52ed7216987
mtime_utc: 2026-09-11T04:10:02.438410+00:00
generated_utc: 2026-09-11T04:11:26.046736+00:00
private: false
---

# bin/search_differential.py

## Purpose

Differential search check: does this tree still answer like a known-good m3?

The unit suite can be entirely green while search returns DIFFERENT ROWS. It
happened on 2026-09-10: a bm25 pre-filter optimisation ranked inside a CTE that
could not see ``is_deleted``/tenancy, so filtered rows ate ``LIMIT`` slots and a
20-row request came back with 8. 3,920 tests passed. The regression was only
visible by asking two builds the same questions and diffing the answers --
which is exactly what this test automates.

**This is a comparison, not an assertion about quality.** It says "the working
tree returns what a reference build returns", so it catches silent retrieval
drift from refactors and optimisations.

WHAT IT CANNOT CATCH, stated plainly because it was counter-tested and failed:

* A bug both builds share. The diff is relative; identical wrongness reads as
  identical. The absolute fill-to-k check below covers one important case of
  this, but only that one.
* A backend-layer truncation, now that search fills to k. Restoring the
  2026-09-10 CTE bug (``_FTS_OVERFETCH=1``) makes
  ``keyword_search_with_row_data(limit=20)`` return 0 rows -- and this tool
  still reports a clean run, because the semantic top-up supplies whatever the
  truncated candidate query failed to. The defect is real and invisible HERE by
  construction.

  That class belongs in a unit test against the backend itself, below the fill
  layer: ``tests/test_candidate_sql_builder.py::
  test_keyword_search_returns_the_full_limit_despite_deleted_rows`` fails on it
  (verified). Do not rely on this script for it.

Reference build, in order of preference -- explicit beats auto-detected, so a
caller who names a reference always gets it:
  1. ``M3_DIFF_REF_PYTHON`` + ``M3_DIFF_REF_BIN`` -- any interpreter + bin dir
  2. a git worktree at ``M3_DIFF_REF_REV`` (exact, and always schema-matched)
  3. the pipx-installed m3 (its own venv interpreter, so its deps resolve)

A pinned git rev is the most reliable reference: an INSTALLED m3 can be old
enough that its schema expectations differ from a store the working tree seeded,
in which case it returns nothing and this test skips rather than lying.

Skips rather than fails when no reference exists: a fresh clone or CI box has
no installed m3, and a test that cannot run must not look like a passing one.

    python -m pytest tests/test_search_differential.py -v
    M3_DIFF_REF_REV=v2026.9.8.0 python -m pytest tests/test_search_differential.py

By default it runs against a THROWAWAY store seeded from this repo's own docs,
so it is self-contained and deterministic. Point it at a real store with
``M3_DIFF_STORE=/path/to/engine_root`` for a higher-signal (but read-only) run --
that is how the 2026-09-10 bug was found, against a 5,203-memory store.

---

## Entry points

- `def main()` (line 266)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--store` | existing engine root to query READ-ONLY (default: seed a throwaway one from docs/) | `os.environ.get('M3_DIFF_STORE', '')` |  | str |  |
| `--k` |  | `8` |  | int |  |

---

## Environment variables read

- `M3_CONFIG_ROOT`
- `M3_DIFF_REF_BIN`
- `M3_DIFF_REF_PYTHON`
- `M3_DIFF_REF_REV`
- `M3_DIFF_STORE`
- `M3_ENGINE_ROOT`
- `M3_FIPS_MODE`
- `M3_FIPS_STRICT`

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `['git', 'worktree', 'add', '-q', '--detach', str(wt), rev]`` (line 154)
- `subprocess.run()  → `[python, '-c', code, engine, config]`` (line 126)
- `subprocess.run()  → `[sys.executable, '-c', _SEED, str(engine), str(config), str(items_path)]`` (line 232)

**sqlite**

- `sqlite3.connect()  → `str(engine / 'agent_memory.db')`` (line 250)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `agent_memory.db`
- `seed_items.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
