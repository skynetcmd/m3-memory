---
tool: bin/embed_sweep_lib.py
sha1: 22720e6d9dc1
mtime_utc: 2026-05-04T23:42:45.306781+00:00
generated_utc: 2026-05-05T01:49:21.670849+00:00
private: false
---

# bin/embed_sweep_lib.py

## Purpose

embed_sweep_lib — shared embed-loop helper for sweeper-style backfill tools.

Two callers consume this:

  bin/embed_backfill.py       — general-purpose, any DB, any variant
  bin/chatlog_embed_sweeper.py — chatlog-specific (type='chat_log'), with
                                 spill-drain + state-file bookkeeping

Both have the same inner shape:

  while not done:
      candidates = fetch_some_unembedded_rows()
      vectors    = await _embed_many(...)
      write_embeddings(...)
      advance the resume cursor

The differences live in *what* they fetch, *what* they write alongside the
embedding row, and *how* they pre-process text before embedding. We expose
those as callbacks so each caller stays in control of its own semantics
while the loop machinery (concurrency, batching, cursor advance, timeouts,
hardening) lives in one place.

History: extracted from bin/embed_backfill.py 2026-05-03 when the chatlog
sweeper migrated to share this helper. See commit message for rationale.

---

## Entry points

_(no conventional entry point detected)_

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

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
