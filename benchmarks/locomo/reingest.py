"""Re-ingest LOCOMO samples with explicit variant tags.

Supports multiple variants in a single invocation so they share the in-process
LLM content-hash cache (memory_core._AUTO_TITLE_CACHE / _AUTO_ENTITIES_CACHE).
Running heuristic_c1c4 + llm_v1 + llm_only together means every unique turn's
LLM title / entities are computed once and reused.

Per-variant config is keyed by variant name in VARIANT_PRESETS. A variant
expressed on the CLI that isn't in presets is treated as heuristic-only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / "bin"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("LLM_ENDPOINTS_CSV", "http://localhost:1234/v1")

import bench_locomo  # noqa: E402


VARIANT_PRESETS: dict[str, dict] = {
    "baseline":            {"use_heuristics": False, "use_llm": False},
    "heuristic_c1c4":      {"use_heuristics": True,  "use_llm": False},
    "llm_v1":              {"use_heuristics": True,  "use_llm": True},
    "llm_only":            {"use_heuristics": False, "use_llm": True},
    # llm_v1 + entity-preserving prompt (proposal 1) + concat heuristic/LLM
    # title in embed_text (proposal 2) + per-session gist+entity context fed
    # to the LLM (proposal 3). Same flag config as llm_v1; the behavior
    # differences are in memory_core prompts and bench_locomo embed_text.
    "llm_v1_title_ctx":    {"use_heuristics": True,  "use_llm": True},
}


async def main(args: argparse.Namespace):
    with open(BASE / "data" / "locomo" / "locomo10.json", "r", encoding="utf-8") as f:
        dataset = json.load(f)
    by_id = {s["sample_id"]: s for s in dataset}

    features_override = (
        [f.strip() for f in args.features.split(",") if f.strip()]
        if args.features else None
    )

    for variant in args.variants:
        cfg = VARIANT_PRESETS.get(variant, {"use_heuristics": True, "use_llm": False})
        print(
            f"\n=== variant={variant!r} "
            f"use_heuristics={cfg['use_heuristics']} use_llm={cfg['use_llm']} ===",
            flush=True,
        )
        for sid in args.samples:
            if sid not in by_id:
                print(f"[{sid}] not in dataset — skipping", flush=True)
                continue
            print(f"[{sid}] ingesting variant={variant!r}...", flush=True)
            n, elapsed = await bench_locomo.ingest_sample_with_graph(
                by_id[sid],
                variant=variant,
                features=features_override,
                use_heuristics=cfg["use_heuristics"],
                use_llm=cfg["use_llm"],
            )
            print(f"[{sid}] ingested {n} items in {elapsed:.1f}s", flush=True)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--samples", nargs="+",
        default=["conv-26", "conv-30", "conv-41", "conv-42"],
        help="Sample IDs to ingest (default: the four Phase 1 convs).",
    )
    p.add_argument(
        "--variants", nargs="+",
        default=["heuristic_c1c4"],
        help="Variant names to produce in one process. Known presets: "
             + ", ".join(VARIANT_PRESETS.keys())
             + ". Unknown names default to heuristic-only.",
    )
    p.add_argument(
        "--features", type=str, default="",
        help="Comma-separated metadata.features override. Empty uses variant defaults.",
    )
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
