"""Cascade-health probe phase of memory_doctor.

Thin wrapper around `memory.doctor.memory_doctor_impl` — the canonical
MCP-tool diagnostic that probes tier-1 (in-proc GGUF), tier-2
(m3-embed-server :8082), DB integrity, and end-to-end embed roundtrip.

Why a wrapper at all: the impl returns a structured dict shaped for
MCP responses; the CLI wants human-readable lines. This module owns
that formatting + maps the impl's summary to an exit code.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

logger = logging.getLogger("memory.doctor.cascade_probe")


def run(brief: bool = False) -> int:
    """Run the embedding-cascade probe and print a human report.

    Returns 0 if summary is 'healthy' or 'degraded'; 1 if 'broken'.
    Returns 0 with a warning if the cascade module itself isn't
    importable — that's a deployment issue separate from cascade health
    and is best left to the operator to address via reinstall.

    brief=True prints a single-line summary (for `m3 doctor --brief`)
    from the SAME structured dict — no prose-parsing.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
        from memory.doctor import memory_doctor_impl
    except Exception as e:
        if not brief:
            logger.warning(
                f"cascade doctor unavailable (memory.doctor not importable): "
                f"{type(e).__name__}: {e}"
            )
        else:
            print("embedding-cascade: unavailable (module not importable)")
        return 0

    try:
        out = asyncio.run(memory_doctor_impl())
    except Exception as e:
        if brief:
            print("embedding-cascade: ❌ probe crashed")
        else:
            logger.error(f"cascade doctor crashed: {type(e).__name__}: {e}")
        return 1

    if brief:
        summary = out.get("summary", "unknown")
        t1 = out.get("tier_1", {}).get("status", "?")
        t2 = out.get("tier_2", {}).get("status", "?")
        rt = out.get("roundtrip", {})
        glyph = "✅" if summary == "healthy" else "⚠️" if summary == "degraded" else "❌"
        lat = f", {rt.get('latency_ms')}ms" if rt.get("latency_ms") is not None else ""
        if out.get("tier_1", {}).get("shared_mode"):
            # Reassuring, accurate phrasing for the shipped default: the shared
            # server is the fast path; per-process tier-1 is off by design.
            print(f"{glyph} embedding-cascade: {summary} — shared tier-2 embedder "
                  f"online (tier-1 appropriately offline{lat})")
        else:
            print(f"{glyph} embedding-cascade: {summary} (tier1 {t1}, tier2 {t2}{lat})")
        return 0 if summary != "broken" else 1

    print()
    print("=== embedding-cascade health (memory_doctor) ===")
    print(f"  summary  : {out.get('summary')}")
    _t1 = out.get("tier_1", {})
    if _t1.get("shared_mode"):
        print("  tier_1   : shared-mode (intentionally off — the shared tier-2 "
              "server owns the single GPU context)")
    else:
        print(f"  tier_1   : {_t1.get('status')}")
    print(
        f"  tier_2   : {out.get('tier_2', {}).get('status')}"
        f"  ({out.get('tier_2', {}).get('url')})"
    )
    print(f"  db       : {out.get('db', {}).get('status')}")
    print(
        f"  roundtrip: {out.get('roundtrip', {}).get('status')}"
        f"  latency={out.get('roundtrip', {}).get('latency_ms')}ms"
    )
    for issue in out.get("issues", []):
        print(f"  ISSUE: {issue}")
    for rec in out.get("recommendations", []):
        print(f"  TIP:   {rec}")
    return 0 if out.get("summary") != "broken" else 1
