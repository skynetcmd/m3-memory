#!/usr/bin/env python3
"""m3_entities_gliner — fast local entity extraction via GLiNER (zero-shot NER).

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
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_BIN = REPO / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

# Bootstrap M3_DATABASE so memory_core's _db() resolves correctly. CLI can
# override via --core-db.
os.environ.setdefault("M3_DATABASE", str(REPO / "memory" / "agent_memory.db"))

import memory_core as mc  # noqa: E402

# GLiNER's published 7-label vocab (person, place, organization, event, concept, object, date).
GLINER_LABELS: list[str] = [
    "person", "place", "organization", "event", "concept", "object", "date",
]
# Map to memory_core's VALID_ENTITY_TYPES vocab so _run_entity_extractor
# doesn't reject our writes for vocab mismatch. concept -> topic; object ->
# legacy_object (the only m3 vocab slot for non-named objects).
GLINER_TYPE_MAP: dict[str, str] = {
    "person": "person",
    "place": "place",
    "organization": "organization",
    "event": "event",
    "date": "date",
    "concept": "topic",
    "object": "legacy_object",
}


@dataclass
class _Cfg:
    model_id: str
    threshold: float
    batch_size: int
    device: str
    dry_run: bool
    limit: int | None
    source_variant: str | None
    force: bool
    min_content_len: int
    recovery_pass: bool


def _resolve_cfg(args: argparse.Namespace) -> _Cfg:
    return _Cfg(
        model_id=os.environ.get("M3_GLINER_MODEL", args.model),
        threshold=float(os.environ.get("M3_GLINER_THRESHOLD", args.threshold)),
        batch_size=int(os.environ.get("M3_GLINER_BATCH_SIZE", args.batch_size)),
        device=os.environ.get("M3_GLINER_DEVICE", args.device),
        dry_run=bool(args.dry_run),
        limit=args.limit,
        source_variant=args.source_variant,
        min_content_len=int(args.min_content_len),
        recovery_pass=bool(args.recovery_pass),
        force=bool(args.force),
    )


def _load_gliner(cfg: _Cfg):
    """Import + load the GLiNER model. Falls back to CPU if CUDA unavailable.

    Heavy imports deferred to call-time so --help / --dry-run don't pay the
    ~3 s torch import cost.
    """
    print(f"[gliner] loading {cfg.model_id} on {cfg.device} ...", flush=True)
    t0 = time.perf_counter()
    try:
        from gliner import GLiNER
    except ImportError as e:
        print(
            f"[gliner] FATAL: the optional GLiNER NER extra is not installed ({e}). "
            f"Install via: pip install 'm3-memory[entity-ner]'  (pulls gliner + torch). "
            f"GLiNER is optional by design — core runs without it; the LLM-based "
            f"bin/m3_entities.py is the no-extra-deps entity extractor.",
            flush=True,
        )
        sys.exit(2)
    model = GLiNER.from_pretrained(cfg.model_id)
    if cfg.device == "cuda":
        try:
            import torch
            if not torch.cuda.is_available():
                print("[gliner] WARN: cuda requested but not available; falling back to cpu", flush=True)
                cfg.device = "cpu"
        except Exception as e:
            print(f"[gliner] WARN: torch import failed ({e}); falling back to cpu", flush=True)
            cfg.device = "cpu"
    if cfg.device != "cpu":
        model = model.to(cfg.device)
    elapsed = time.perf_counter() - t0
    print(
        f"[gliner] loaded in {elapsed:.1f}s; threshold={cfg.threshold} batch_size={cfg.batch_size}",
        flush=True,
    )
    return model


def _select_rows(
    db_path: str,
    source_variant: str | None,
    limit: int | None,
    force: bool,
    conv_allowlist: set[str] | None,
    min_content_len: int = 5,
    recovery_pass: bool = False,
) -> list[tuple[str, str]]:
    """Return [(memory_id, content), ...] eligible for GLiNER extraction.

    Eligibility (mirrors memory_core._select_pending_entity_extraction; we
    re-implement inline because we want a variant-name predicate that scopes
    to a specific obs variant rather than IS NULL):
      - mi.type != 'fact_enriched'         (don't extract from derived rows)
      - COALESCE(mi.is_deleted, 0) = 0
      - mi.variant matches source_variant  ('__none__' = IS NULL)
      - length(mi.content) > min_content_len  (default 5; was 10 historically,
        which excluded short canonical obs like "User runs." — see GLiNER
        empty-cluster analysis 2026-05-10)
      - if not force AND not recovery_pass: mi.id NOT IN
        (SELECT DISTINCT memory_id FROM memory_item_entities)
      - if recovery_pass: ONLY rows that ARE in the NOT-IN subquery (i.e.
        previously-empty rows). Pair with --threshold < 0.5 to backfill
        below-cutoff entities. Mutually exclusive with --force.
      - optional conversation_id allowlist (metadata_json.$.conversation_id OR mi.conversation_id)
    """
    import sqlite3 as _s3
    conn = _s3.connect(db_path, timeout=30)
    conn.row_factory = _s3.Row
    try:
        if source_variant == "__none__":
            variant_clause = "AND mi.variant IS NULL"
            variant_params: list[str] = []
        elif source_variant:
            variant_clause = "AND mi.variant = ?"
            variant_params = [source_variant]
        else:
            variant_clause = ""
            variant_params = []
        if recovery_pass:
            # Recovery pass: only rows currently absent from memory_item_entities.
            extraction_clause = (
                "AND mi.id NOT IN (SELECT DISTINCT memory_id FROM memory_item_entities)"
            )
        elif force:
            # Force: ignore extraction state, redo everything.
            extraction_clause = ""
        else:
            # Normal: skip rows already extracted.
            extraction_clause = (
                "AND mi.id NOT IN (SELECT DISTINCT memory_id FROM memory_item_entities)"
            )
        sql = f"""
            SELECT mi.id, mi.content, mi.metadata_json, mi.conversation_id
            FROM memory_items mi
            WHERE mi.type != 'fact_enriched'
              AND COALESCE(mi.is_deleted, 0) = 0
              AND length(mi.content) > {int(min_content_len)}
              {variant_clause}
              {extraction_clause}
            ORDER BY mi.created_at ASC
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql, variant_params).fetchall()
    finally:
        conn.close()
    out: list[tuple[str, str]] = []
    for r in rows:
        text = (r["content"] or "").strip()
        if not text:
            continue
        if conv_allowlist is not None:
            cid = r["conversation_id"] or ""
            if not cid:
                try:
                    md = json.loads(r["metadata_json"] or "{}")
                    cid = md.get("conversation_id", "") or ""
                except Exception:
                    cid = ""
            if cid not in conv_allowlist:
                continue
        out.append((r["id"], text))
    return out


def _load_conv_allowlist(path: str | None) -> set[str] | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        print(f"[gliner] WARN: conv allowlist {p} not found; ignoring", flush=True)
        return None
    raw = p.read_text(encoding="utf-8").strip()
    if raw.startswith("["):
        items = json.loads(raw)
    else:
        items = [
            ln.split("#", 1)[0].strip()
            for ln in raw.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
    out = {s for s in items if s}
    print(f"[gliner] conv allowlist: {len(out)} ids", flush=True)
    return out


def _gliner_predict_batch(model, texts: list[str], cfg: _Cfg) -> list[list[dict]]:
    """Run GLiNER on a batch of texts. Returns one list-of-spans per input.

    Falls back to per-text calls if the installed gliner version doesn't
    expose a batch API (older releases only ship `predict_entities`).
    """
    if hasattr(model, "batch_predict_entities"):
        return model.batch_predict_entities(texts, GLINER_LABELS, threshold=cfg.threshold)
    if hasattr(model, "predict_entities_batch"):
        return model.predict_entities_batch(texts, GLINER_LABELS, threshold=cfg.threshold)
    return [model.predict_entities(t, GLINER_LABELS, threshold=cfg.threshold) for t in texts]


def _spans_to_extractor_dict(spans: list[dict]) -> dict:
    """Convert GLiNER's [{text, label, score, start, end}] into the dict shape
    that memory_core._run_entity_extractor expects.

    De-dupes by (canonical_name, mapped_type). The same person mentioned three
    times in one obs becomes one entity row + one mention row (the underlying
    _link_memory_to_entity is INSERT OR IGNORE so duplicates would be no-ops
    anyway, but de-duping here keeps the intermediate list small).

    Threads `start` (GLiNER's character offset) through as `mention_offset`
    so memory_core._run_entity_extractor can persist it. Required by the
    relations module's surface-text rules (located_in, etc.); without an
    offset every mention claims to start at position 0 and between-mention
    text checks become impossible. When de-duping, the highest-confidence
    occurrence wins on mention_text AND mention_offset together (keeping
    them paired so the offset always corresponds to the chosen surface
    form, not a different occurrence's position).
    """
    seen: dict[tuple[str, str], dict] = {}
    for sp in spans:
        cname_raw = (sp.get("text") or "").strip()
        if not cname_raw:
            continue
        gliner_type = (sp.get("label") or "").strip().lower()
        m3_type = GLINER_TYPE_MAP.get(gliner_type)
        if not m3_type:
            # Unknown label — should not happen since we pin the label list,
            # but skip defensively.
            continue
        cname = cname_raw.lower()
        key = (cname, m3_type)
        if key in seen:
            if float(sp.get("score", 0.0)) > seen[key].get("confidence", 0.0):
                seen[key]["confidence"] = float(sp.get("score", 0.0))
                seen[key]["mention_text"] = cname_raw
                seen[key]["mention_offset"] = int(sp.get("start") or 0)
            continue
        seen[key] = {
            "canonical_name": cname,
            "entity_type": m3_type,
            "mention_text": cname_raw,
            "mention_offset": int(sp.get("start") or 0),
            "confidence": float(sp.get("score", 0.85)),
        }
    return {"entities": list(seen.values()), "relationships": []}


async def _process_batch(
    rows: list[tuple[str, str]],
    spans_per_row: list[list[dict]],
    counters: dict,
) -> None:
    """Hand each row to memory_core's writer with a passthrough extractor."""
    for (mid, text), spans in zip(rows, spans_per_row):
        extractor_out = _spans_to_extractor_dict(spans)
        n_ents = len(extractor_out["entities"])
        counters["processed"] += 1
        if not n_ents:
            counters["empty"] += 1
            continue
        counters["entities_emitted"] += n_ents

        async def _passthrough(_text: str, _out=extractor_out) -> dict:
            return _out

        try:
            await mc._run_entity_extractor(mid, text, _passthrough)
        except Exception as e:
            counters["write_failed"] += 1
            print(f"[gliner] WRITE-FAIL {mid[:8]}: {type(e).__name__}: {e}", flush=True)


async def _run_async(args: argparse.Namespace) -> int:
    cfg = _resolve_cfg(args)

    db_path = args.core_db or os.environ.get("M3_DATABASE")
    if not db_path or not Path(db_path).exists():
        print(f"[gliner] FATAL: DB not found at {db_path}; pass --core-db or set M3_DATABASE", flush=True)
        return 2
    # memory_core._run_entity_extractor reads M3_DATABASE via the active
    # context; pin it now so an explicit --core-db wins over the bootstrap.
    os.environ["M3_DATABASE"] = db_path

    conv_allowlist = _load_conv_allowlist(
        args.source_conv_list or os.environ.get("M3_ENTITIES_CONV_LIST")
    )

    print(
        f"[gliner] db={db_path} variant={cfg.source_variant!r} "
        f"limit={cfg.limit} force={cfg.force}",
        flush=True,
    )
    if cfg.recovery_pass and cfg.force:
        print("[gliner] FATAL: --recovery-pass and --force are mutually exclusive", flush=True)
        return 2
    rows = _select_rows(
        db_path, cfg.source_variant, cfg.limit, cfg.force, conv_allowlist,
        min_content_len=cfg.min_content_len,
        recovery_pass=cfg.recovery_pass,
    )
    pass_label = "recovery" if cfg.recovery_pass else ("force" if cfg.force else "normal")
    print(
        f"[gliner] {len(rows)} eligible rows "
        f"(pass={pass_label}, threshold={cfg.threshold}, min_content_len={cfg.min_content_len})",
        flush=True,
    )
    if not rows:
        return 0

    if cfg.dry_run:
        print(
            "[gliner] --dry-run: would extract entities from "
            f"{len(rows)} rows in batches of {cfg.batch_size}",
            flush=True,
        )
        for mid, txt in rows[:3]:
            preview = txt[:120] + ("..." if len(txt) > 120 else "")
            print(f"  {mid[:8]}: {preview!r}", flush=True)
        return 0

    model = _load_gliner(cfg)

    counters = {
        "processed": 0,
        "entities_emitted": 0,
        "empty": 0,
        "write_failed": 0,
    }
    started = time.monotonic()
    for batch_start in range(0, len(rows), cfg.batch_size):
        batch = rows[batch_start:batch_start + cfg.batch_size]
        texts = [text for _, text in batch]
        try:
            spans_per_row = _gliner_predict_batch(model, texts, cfg)
        except Exception as e:
            print(
                f"[gliner] batch starting at {batch_start} failed: "
                f"{type(e).__name__}: {e}; falling back to per-row inference",
                flush=True,
            )
            spans_per_row = [
                _gliner_predict_batch(model, [t], cfg)[0] for t in texts
            ]
        await _process_batch(batch, spans_per_row, counters)

        # Progress log every 10 batches + at end-of-corpus.
        if (batch_start // cfg.batch_size) % 10 == 0 or (batch_start + cfg.batch_size) >= len(rows):
            done = min(batch_start + cfg.batch_size, len(rows))
            elapsed = time.monotonic() - started
            rate = done / elapsed if elapsed > 0 else 0
            eta_min = ((len(rows) - done) / rate / 60) if rate > 0 else 0
            print(
                f"[gliner] {done}/{len(rows)} processed "
                f"({rate:.1f} rows/s, ETA {eta_min:.1f} min) "
                f"entities={counters['entities_emitted']} "
                f"empty={counters['empty']} write_fail={counters['write_failed']}",
                flush=True,
            )

    elapsed = time.monotonic() - started
    print(
        f"[gliner] done in {elapsed/60:.1f}m: "
        f"{counters['processed']} processed, "
        f"{counters['entities_emitted']} entities, "
        f"{counters['empty']} empty, "
        f"{counters['write_failed']} write-fail",
        flush=True,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--core", action="store_true",
                    help="Operate on the core memory DB (the default; kept for "
                         "CLI symmetry with bin/m3_entities.py).")
    ap.add_argument("--core-db", default=None,
                    help="Explicit path to the core memory DB. Overrides M3_DATABASE.")
    ap.add_argument("--source-variant", default=None,
                    help="Filter source rows by variant. '__none__' = variant IS NULL "
                         "(true core rows). A name scopes to a single variant. "
                         "Omit to process all variants.")
    ap.add_argument("--source-conv-list",
                    default=os.environ.get("M3_ENTITIES_CONV_LIST"),
                    help="Path to a conversation_id allowlist (newline-delimited "
                         "or JSON array). Filters AFTER --source-variant.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap eligible rows (smoke testing).")
    ap.add_argument("--model", default="urchade/gliner_large-v2.1",
                    help="GLiNER HF model id. Default: urchade/gliner_large-v2.1.")
    ap.add_argument("--threshold", type=float, default=0.5,
                    help="Confidence threshold (default: 0.5; lower e.g. 0.3 widens recall).")
    ap.add_argument("--batch-size", type=int, default=32,
                    help="Batch size for GLiNER inference (default: 32 — fits in "
                         "a typical GPU alongside the embedder).")
    ap.add_argument("--device", default="cuda",
                    help="Inference device: 'cuda' (default; falls back to cpu) or 'cpu'.")
    ap.add_argument("--force", action="store_true",
                    help="Re-extract rows already linked in memory_item_entities. "
                         "Default: skip already-extracted. Mutually exclusive "
                         "with --recovery-pass.")
    ap.add_argument("--min-content-len", type=int, default=5,
                    help="Skip obs with content shorter than this many chars "
                         "(default: 5). Was 10 historically, which excluded "
                         "valid short canonical obs like 'User runs.' (10 chars). "
                         "Lowered to 5 to capture canonical persona-action facts.")
    ap.add_argument("--recovery-pass", action="store_true",
                    help="Process ONLY obs that currently have zero entity rows "
                         "(i.e. previously empty). Pair with --threshold < 0.5 "
                         "to backfill below-cutoff entities. Mutually exclusive "
                         "with --force. Idempotent: re-runs only touch new rows.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show eligible-row count + first 3 sample texts; don't load "
                         "the model and don't write.")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(_run_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
