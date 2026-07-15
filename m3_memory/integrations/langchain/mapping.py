"""The single source of mapping truth: m3 row ⇄ LangChain / mem0 objects.

DESIGN_PHILOSOPHIES §3 (structured returns): all row↔object conversion lives
HERE — rows in, typed objects out. Nothing else in the package formats or parses
m3's wire shapes. This is also where three verified m3 realities are absorbed so
no caller has to know them:

  1. **Mixed return shapes.** ``memory_search_scored`` returns native
     ``list[(score, item_dict)]``; but ``memory_get`` returns a *formatted JSON
     string* (or the sentinel ``"Error: not found"``), and ``chatlog_search``
     returns a JSON string ``{"results":[...]}``. Mapping normalizes all three.
  2. **Field-name trap.** mem0 calls the text field ``memory``; m3 calls it
     ``content``. A migrated program reading ``entry["memory"]`` KeyErrors if we
     emit ``content``. :func:`to_mem0_result` renames it — this is the fragile
     seam of the whole drop-in, so it lives in one tested place.
  3. **metadata_json split.** Arbitrary LangChain/mem0 metadata rides m3's
     free-form ``metadata_json`` column; ``content`` rides the ``content``
     column. Mapping owns the split-on-write and merge-on-read so ``value`` dicts
     round-trip losslessly.

Temporal/confidence fields (``confidence``/``valid_from``/``valid_to``) are
present in search results because the search caller passes ``extra_columns``
(§2.4); mapping still uses ``.get()`` defensively.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

# §7 tenancy resolution, shared by the mem0-compat and M3Store surfaces so both
# resolve user_id identically. Order: explicit arg > constructor default >
# M3_DEFAULT_USER_ID env > (caller raises). The env default exists ONLY to remove
# onboarding friction for single-user LangChain apps — it never weakens isolation:
# when nothing resolves, the caller still raises (there is no anonymous/global
# mode). A single-user prototyper sets M3_DEFAULT_USER_ID once and stops passing
# user_id= everywhere; a multi-tenant app leaves it unset and the raise stands.
_ENV_DEFAULT_USER_ID = "M3_DEFAULT_USER_ID"


def resolve_user_id(explicit: Optional[str], default: Optional[str] = None) -> Optional[str]:
    """Resolve a user_id from (explicit > constructor default > env), or None.

    Returns None when nothing resolves — the CALLER decides how to fail (the mem0
    surface and M3Store raise their own tenancy-specific messages). This helper
    never raises, so the env default can't silently change a caller's error text.
    """
    return explicit or default or os.environ.get(_ENV_DEFAULT_USER_ID) or None

# The temporal/confidence columns the adapter asks search to surface, and which
# mapping lifts into result metadata. Kept here so the search caller and the
# mapper agree on ONE list.
EXTRA_COLUMNS = ["confidence", "valid_from", "valid_to", "metadata_json"]

# Cap on rows fetched when clearing a conversation (§2.2b). A conversation with
# more turns than this is paged by repeated clear() calls; the cap defends the
# boundary against an unbounded fetch (§4/§6), mirroring m3's own MAX_SEARCH_K.
MAX_CLEAR_ROWS = 1000

# Sentinel strings m3's sync string-returning impls use for "not found".
_NOT_FOUND_MARKERS = ("Error: not found", "Error: item")


# ── metadata_json split / merge ───────────────────────────────────────────────

def split_value(value: dict) -> tuple[str, dict]:
    """Split a LangChain/mem0 ``value`` dict into (content, extra-metadata).

    ``content`` (or ``text``/``memory`` as fallbacks) goes to m3's content
    column; every OTHER key becomes ``metadata_json``. Round-trips with
    :func:`merge_value`.
    """
    v = dict(value or {})
    content = v.pop("content", None)
    if content is None:
        content = v.pop("text", None)
    if content is None:
        content = v.pop("memory", None)  # mem0's field name, tolerated on input
    if content is None:
        content = ""
    return str(content), v


def _loads_metadata(raw: Any) -> dict:
    """metadata_json may arrive as a dict (already parsed) or a JSON string."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def dumps_metadata(md: dict) -> str:
    """Serialize a metadata dict to a JSON string (for impls whose ``metadata``
    arg wants a string, e.g. ``memory_supersede_impl``)."""
    try:
        return json.dumps(md, default=str)
    except (TypeError, ValueError):
        return "{}"


def merge_value(item: dict) -> dict:
    """Reconstruct a full ``value`` dict from an m3 item: content column +
    parsed metadata_json, merged. Inverse of :func:`split_value`."""
    md = _loads_metadata(item.get("metadata_json"))
    out = dict(md)
    out["content"] = item.get("content", "")
    return out


# ── string-returning sync impls → native ──────────────────────────────────────

def parse_get(raw: Any) -> Optional[dict]:
    """Normalize ``memory_get`` output → native item dict, or None if not found.

    ``memory_get`` (sync) returns a formatted JSON string, or a ``"Error: ..."``
    sentinel when the id is absent. LangChain's ``GetOp`` contract wants None on
    miss (§3: LangChain-facing empties follow ITS contract).
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if any(s.startswith(m) for m in _NOT_FOUND_MARKERS):
            return None
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, dict) else None
        except (ValueError, TypeError):
            return None
    return None


_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def parse_written_id(raw: Any) -> Optional[str]:
    """Extract the new memory id from a write/supersede return string.

    ``memory_write`` returns ``"Created: <uuid>"`` — but on a fresh write the
    tail carries a suffix: ``"Created: <uuid> (embedding deferred — …)"``.
    ``memory_supersede`` returns ``"Superseded <old> -> Created: <new>"`` (the
    ``<old>`` is also a uuid). Both start ``"Error: ..."`` on failure. So we
    extract the uuid that follows the LAST ``"Created: "`` marker (robust to the
    suffix AND to a leading old-id), else the last uuid anywhere, else None.
    A bare uuid (the bulk path returns ``list[str]``) passes through.
    """
    if not raw:
        return None
    s = str(raw).strip()
    if s.startswith("Error"):
        return None
    marker = "Created: "
    idx = s.rfind(marker)
    tail = s[idx + len(marker):] if idx != -1 else s
    m = _UUID_RE.search(tail)
    if m:
        return m.group(0)
    # Fallback: any uuid in the whole string (e.g. a bare id).
    m = _UUID_RE.search(s)
    return m.group(0) if m else None


def parse_chatlog_search(raw: Any) -> list[dict]:
    """Normalize ``chatlog_search`` output → list of native row dicts.

    ``chatlog_search`` returns a JSON string ``{"results":[...], "count":N}``.
    Rows come back newest-first (``created_at`` DESC); callers that want
    message-history order must reverse (see history.py).
    """
    if isinstance(raw, dict):
        results = raw.get("results", [])
    elif isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            results = parsed.get("results", []) if isinstance(parsed, dict) else []
        except (ValueError, TypeError):
            results = []
    else:
        results = []
    return [r for r in results if isinstance(r, dict)]


# ── mem0 result shape ─────────────────────────────────────────────────────────

def to_mem0_result(score: float, item: dict) -> dict:
    """One m3 ``(score, item)`` search tuple → one mem0-shaped result dict.

    THE fragile seam of the drop-in: mem0 names the text field ``memory``, m3
    names it ``content``. We emit ``memory`` (never ``content``) so migrated code
    reading ``r["memory"]`` keeps working. Temporal/confidence fields ride
    ``metadata`` (present because search passed ``extra_columns``; ``.get()``
    stays defensive). The §5 field-shape test asserts ``"memory" in r`` AND
    ``"content" not in r``.
    """
    md: dict = {}
    # User-supplied metadata_json rides through first...
    md.update(_loads_metadata(item.get("metadata_json")))
    # ...then m3's first-class signal (never let it be clobbered by user keys).
    for k in ("confidence", "valid_from", "valid_to"):
        val = item.get(k)
        if val is not None and val != "":
            md[k] = val
    return {
        "id": item.get("id"),
        "memory": item.get("content", ""),  # note: content -> "memory"
        "score": score,
        "metadata": md,
    }


def to_mem0_results(rows: list[tuple[float, dict]]) -> dict:
    """``memory_search_scored`` rows → mem0 ``{"results": [...]}`` envelope."""
    return {"results": [to_mem0_result(s, it) for s, it in rows]}
