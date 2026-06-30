---
tool: bin/m3_entities_gliner.py
sha1: 0072125b0ebc
mtime_utc: 2026-06-30T21:32:48.329659+00:00
generated_utc: 2026-06-30T22:19:18.371098+00:00
private: false
---

# bin/m3_entities_gliner.py

## Purpose

m3_entities_gliner — fast local entity extraction via GLiNER (zero-shot NER).

An optional, local alternative to the LLM-based extractor in `bin/m3_entities.py`.
GLiNER (a small DeBERTa-backbone NER model) runs in-process on GPU/CPU — no LLM
endpoint, no API cost — and is dramatically faster than an LLM per turn for the
entity-extraction step (entity spans only; it does not emit relationships).

Recommended config (urchade/gliner_large-v2.1):
    threshold  = 0.5   (precision-leaning; lower, e.g. 0.3, widens recall)
    batch_size = 32
    device     = cuda  (~3-4 GB VRAM; falls back to CPU)

Optional dependency: requires the `entity-ner` extra
    pip install 'm3-memory[entity-ner]'    # pulls gliner + torch
Core runs fully without it; `bin/m3_entities.py` is the no-extra-deps extractor.

Reuses `memory_core._run_entity_extractor` for the write path so:
  - entity-resolve / link-insert semantics stay identical to the LLM path
  - bitemporal valid_from inheritance still works
  - vocabulary validation against `VALID_ENTITY_TYPES` still gates writes
  - re-running is idempotent (already-linked rows skipped)

Usage:
    python bin/m3_entities_gliner.py --core --threshold 0.5 --batch-size 32
    python bin/m3_entities_gliner.py --core --source-variant <variant> --dry-run

Env vars (all optional):
    M3_DATABASE              core DB path (default: memory/agent_memory.db)
    M3_GLINER_MODEL          override model id (default: urchade/gliner_large-v2.1)
    M3_GLINER_THRESHOLD      override threshold (default: 0.5)
    M3_GLINER_BATCH_SIZE     override batch size (default: 32)
    M3_GLINER_DEVICE         override device (default: cuda; falls back to cpu)
    M3_ENTITIES_CONV_LIST    optional path to a conv_id allowlist file

---

## Entry points

- `def main()` (line 484)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--core` | Operate on the core memory DB (the default; kept for CLI symmetry with bin/m3_entities.py). | `False` |  | store_true |  |
| `--core-db` | Explicit path to the core memory DB. Overrides M3_DATABASE. | None |  | str |  |
| `--source-variant` | Filter source rows by variant. '__none__' = variant IS NULL (true core rows). A name scopes to a single variant. Omit to process all variants. | None |  | str |  |
| `--source-conv-list` | Path to a conversation_id allowlist (newline-delimited or JSON array). Filters AFTER --source-variant. | `os.environ.get('M3_ENTITIES_CONV_LIST')` |  | str |  |
| `--limit` | Cap eligible rows (smoke testing). | None |  | int |  |
| `--model` | GLiNER HF model id. Default: urchade/gliner_large-v2.1. | `urchade/gliner_large-v2.1` |  | str |  |
| `--threshold` | Confidence threshold (default: 0.5; lower e.g. 0.3 widens recall). | `0.5` |  | float |  |
| `--batch-size` | Batch size for GLiNER inference (default: 32 — fits in a typical GPU alongside the embedder). | `32` |  | int |  |
| `--device` | Inference device: 'cuda' (default; falls back to cpu) or 'cpu'. | `cuda` |  | str |  |
| `--force` | Re-extract rows already linked in memory_item_entities. Default: skip already-extracted. Mutually exclusive with --recovery-pass. | `False` |  | store_true |  |
| `--min-content-len` | Skip obs with content shorter than this many chars (default: 5). Was 10 historically, which excluded valid short canonical obs like 'User runs.' (10 chars). Lowered to 5 to capture canonical persona-action facts. | `5` |  | int |  |
| `--recovery-pass` | Process ONLY obs that currently have zero entity rows (i.e. previously empty). Pair with --threshold < 0.5 to backfill below-cutoff entities. Mutually exclusive with --force. Idempotent: re-runs only touch new rows. | `False` |  | store_true |  |
| `--dry-run` | Show eligible-row count + first 3 sample texts; don't load the model and don't write. | `False` |  | store_true |  |

---

## Environment variables read

- `M3_DATABASE`
- `M3_ENTITIES_CONV_LIST`
- `M3_GLINER_BATCH_SIZE`
- `M3_GLINER_DEVICE`
- `M3_GLINER_MODEL`
- `M3_GLINER_THRESHOLD`

---

## Calls INTO this repo (intra-repo imports)

- `memory_core`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `gliner (GLiNER)`
- `torch`

---

## File dependencies (repo paths referenced)

- `agent_memory.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
