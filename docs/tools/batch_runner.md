---
tool: bin/batch_runner.py
sha1: dd9a5afc9276
mtime_utc: 2026-05-07T03:32:14.554106+00:00
generated_utc: 2026-05-09T13:54:33.807915+00:00
private: false
---

# bin/batch_runner.py

## Purpose

Provider-neutral batch-API runner protocol with Anthropic implementation.

Use when you have a pile of independent LLM calls and want a 50% cost
discount in exchange for async wallclock (typically minutes-to-hours
for the batch to complete).

Currently implements:
  - AnthropicBatchRunner: /v1/messages/batches, 50% off list pricing.

Stub points for future:
  - OpenAIBatchRunner: /v1/batches with JSONL Files API (50% off).
  - VertexBatchRunner: GCS-backed batch (50% off, but needs Cloud Storage
    bucket provisioning).

Calling code uses BatchRequest/BatchResult dataclasses; runners translate
to/from native API formats. See bin/m3_enrich_batch.py for usage.

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

- `unified_ai (_is_gemini_endpoint)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `httpx`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
