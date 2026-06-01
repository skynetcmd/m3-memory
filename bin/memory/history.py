"""Polars-accelerated bitemporal timeline audits and delta grouping.

Provides:
1. compute_bitemporal_diffs_impl: high-speed columnar timeline grouping using Polars if available, falling back gracefully to pure-Python dictionary grouping.
2. get_bitemporal_timeline_impl: constructs a consolidated chronological delta timeline of all field mutations for a specific memory item.
"""
from __future__ import annotations

import json
import logging

from memory.db import _db

logger = logging.getLogger("memory.history")


def compute_bitemporal_diffs_impl(
    history_rows: list[tuple[int, str, str, str, str]]
) -> str:
    """Compute consolidated current values per memory_id and field from raw history rows.

    Parameters:
        history_rows: list of tuples (id, memory_id, field, old_value, new_value)

    Returns:
        JSON string containing the grouped current values per field.
    """
    if not history_rows:
        return json.dumps([])

    # Try high-speed Polars path first
    try:
        import polars as pl

        # 1. Load data directly into columnar Polars Arrow chunks
        df = pl.DataFrame(
            history_rows,
            schema=["id", "memory_id", "field", "old_value", "new_value"],
            orient="row"
        )

        # 2. Perform parallel time-series grouping and delta calculation
        # Group by memory_id and field, select the last new_value chronologically (last id)
        grouped = (
            df.lazy()
            .sort("id")
            .group_by(["memory_id", "field"])
            .agg(pl.col("new_value").last().alias("current_value"))
            .collect()
        )

        # 3. Serialize back via fast JSON writer
        return json.dumps(grouped.to_dicts())
    except ImportError:
        pass

    # Fallback to pure-Python dictionary grouping
    # Replicate the sorting by ID and grouping by (memory_id, field)
    sorted_rows = sorted(history_rows, key=lambda x: x[0])
    grouped_dict = {}
    for row in sorted_rows:
        _, memory_id, field, _, new_val = row
        grouped_dict[(memory_id, field)] = new_val

    results = []
    for (mem_id, field), current_val in grouped_dict.items():
        results.append({
            "memory_id": mem_id,
            "field": field,
            "current_value": current_val
        })
    return json.dumps(results)


def get_bitemporal_timeline_impl(memory_id: str, limit: int = 100) -> str:
    """Constructs a consolidated chronological delta timeline of all mutations for a memory."""
    with _db() as db:
        rows = db.execute(
            "SELECT id, memory_id, field, prev_value, new_value, created_at "
            "FROM memory_history WHERE memory_id = ? ORDER BY created_at ASC LIMIT ?",
            (memory_id, limit),
        ).fetchall()

    if not rows:
        return f"No bitemporal history found for memory: {memory_id}"

    history_tuples = []
    for r in rows:
        history_tuples.append((
            r["id"],
            r["memory_id"],
            r["field"],
            r["prev_value"] or "",
            r["new_value"] or ""
        ))

    # Run the fast Polars or fallback grouping
    diffs_json = compute_bitemporal_diffs_impl(history_tuples)
    diffs = json.loads(diffs_json)

    lines = [f"Bitemporal Change Timeline for {memory_id}:"]
    lines.append("-" * 60)
    for r in rows:
        prev = (r["prev_value"] or "")[:40]
        new = (r["new_value"] or "")[:40]
        lines.append(
            f"  [{r['created_at']}] mutated '{r['field']}': {prev!r} -> {new!r}"
        )
    lines.append("-" * 60)
    lines.append("Consolidated Current State (Active Delta Grouping):")
    for diff in diffs:
        lines.append(f"  • '{diff['field']}': {diff['current_value']!r}")

    return "\n".join(lines)
