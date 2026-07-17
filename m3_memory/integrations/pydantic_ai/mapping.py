"""Pure translation between m3 rows and PydanticAI-facing shapes.

All PydanticAI-shape knowledge lives here (Recipe 2, docs/EXTENDING.md) so the
rest of the adapter stays framework-generic. Unlike the CrewAI adapter, PydanticAI
never hands us its own embedding vector — its tools pass *text*, and its message
history is plain ``ModelMessage`` objects — so mapping here is simple dict/text
shaping, with none of CrewAI's dual-vector identity handling.

The two directions:
  * ``recall_hits_to_dicts`` — m3 ``memory_search_scored`` rows
    (``list[(score, item_dict)]``) → a compact ``list[dict]`` the model can read
    as a tool return (content + score + a few useful fields, no internal columns).
  * ``recalled_memories_block`` — the same hits → a single plain-text block a
    history-processor prepends as recalled context.
"""

from __future__ import annotations

import json
from typing import Any

# The item columns we surface to the model. Deliberately small — internal columns
# (embeddings, hashes, decay bookkeeping) never reach the LLM context.
_SURFACED_FIELDS = ("id", "content", "type", "importance", "created_at")


def _coerce_item(item: Any) -> dict:
    """m3 rows may arrive as a dict or a sqlite3.Row-like mapping; normalize."""
    if isinstance(item, dict):
        return item
    try:
        return dict(item)
    except Exception:  # noqa: BLE001 — last-resort: expose nothing structured
        return {}


def recall_hit_to_dict(score: float, item: Any) -> dict:
    """One ``(score, item)`` hit → a compact dict for a tool return."""
    it = _coerce_item(item)
    out: dict[str, Any] = {"score": round(float(score), 4)}
    for f in _SURFACED_FIELDS:
        if f in it and it[f] is not None:
            out[f] = it[f]
    # metadata_json rides as a nested object when present + parseable.
    raw_md = it.get("metadata_json")
    if raw_md:
        try:
            md = json.loads(raw_md) if isinstance(raw_md, str) else raw_md
            if isinstance(md, dict) and md:
                out["metadata"] = md
        except (json.JSONDecodeError, TypeError):
            pass
    return out


def recall_hits_to_dicts(rows: list) -> list[dict]:
    """m3 ``memory_search_scored`` rows → ``list[dict]`` (model-readable)."""
    return [recall_hit_to_dict(score, item) for score, item in (rows or [])]


def recalled_memories_block(rows: list, *, header: str = "Relevant memories") -> str:
    """The same hits → a plain-text block for a history-processor to prepend.

    Empty rows → empty string (the caller skips injection entirely, so no empty
    'Relevant memories:' noise reaches the model).
    """
    hits = recall_hits_to_dicts(rows)
    if not hits:
        return ""
    lines = [f"{header}:"]
    for h in hits:
        content = str(h.get("content", "")).strip()
        if content:
            lines.append(f"- {content}")
    return "\n".join(lines) if len(lines) > 1 else ""
