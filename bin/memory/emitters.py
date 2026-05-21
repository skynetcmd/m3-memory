from __future__ import annotations

import json
import logging
import re
from typing import Any

from .config import (
    INGEST_GIST_MIN_TURNS,
    INGEST_GIST_STRIDE,
    INGEST_WINDOW_SIZE,
)
from .db import _db
from .fts import _EVENT_DATE_HINT, _EVENT_PROPER_NOUN, _EVENT_SENT_SPLIT, _EVENT_VERB_RE

logger = logging.getLogger("memory.emitters")


def _extract_event_sentences(content: str) -> list[tuple[str, str]]:
    """Extract event-like sentences from message content.
    Returns list of (sentence, primary_verb) tuples.
    """
    out = []
    # Split by sentence boundaries
    for sent in _EVENT_SENT_SPLIT.split(content):
        s = sent.strip()
        if not s:
            continue
        # Heuristic 1: Must contain at least one capitalized proper noun
        if not _EVENT_PROPER_NOUN.search(s):
            continue
        # Heuristic 2: Must contain one of our event verbs
        m = _EVENT_VERB_RE.search(s)
        if not m:
            continue
        # Heuristic 3: Must contain a temporal hint (date/time/ago)
        if not _EVENT_DATE_HINT.search(s):
            continue
        out.append((s, m.group(1).lower()))
    return out


async def _maybe_emit_event_rows(
    content: str,
    metadata: str | dict | None,
    conversation_id: str,
    user_id: str,
    parent_id: str,
) -> None:
    """Extract event-like sentences from a message and emit one
    type='event_extraction' row per match, linked back to the parent via
    `references`. Embed_text includes resolved temporal anchors so date
    queries can hit these rows directly. Idempotent: skipped if the caller
    did not provide a conversation_id."""
    if not conversation_id:
        return
    events = _extract_event_sentences(content)
    if not events:
        return
    meta_dict: dict[str, Any] = {}
    if metadata:
        try:
            meta_dict = metadata if isinstance(metadata, dict) else json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            meta_dict = {}
    session_id = meta_dict.get("session_id", "")

    # Lazy import to avoid circularity
    from .write import memory_link_impl, memory_write_impl

    for sent, verb in events:
        ev_meta = {
            "source_message_id": parent_id,
            "verb": verb,
            "session_id": session_id,
            "temporal_anchors": meta_dict.get("temporal_anchors") or [],
        }
        try:
            created = await memory_write_impl(
                type="event_extraction",
                content=sent,
                title=f"event:{verb}",
                metadata=json.dumps(ev_meta),
                user_id=user_id,
                source="event_extraction",
                conversation_id=conversation_id,
                embed=True,
            )
            # created is "Created: <uuid> (...)"
            m = re.search(r"Created:\s*([a-f0-9-]+)", created or "")
            if m:
                try:
                    memory_link_impl(m.group(1), parent_id, "references")
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"event_extraction emit failed: {e}")


async def _maybe_emit_window_chunk(conversation_id: str, user_id: str) -> None:
    """Emit a sliding 3-turn (INGEST_WINDOW_SIZE) summary row that embeds the
    concatenated text of the most recent N message rows in a conversation.
    Fires only on turns whose count is a multiple of the window size, so a
    conversation of 9 turns emits 3 window rows rather than 9 overlapping
    ones. Does not fire until at least INGEST_WINDOW_SIZE turns exist."""
    if not conversation_id:
        return
    try:
        with _db() as db:
            rows = db.execute(
                "SELECT id, content, title FROM memory_items "
                "WHERE conversation_id = ? AND type = 'message' "
                "AND is_deleted = 0 ORDER BY created_at ASC",
                (conversation_id,),
            ).fetchall()
    except Exception as e:
        logger.debug(f"window chunk query failed: {e}")
        return
    n = len(rows)
    if n < INGEST_WINDOW_SIZE or (n % INGEST_WINDOW_SIZE) != 0:
        return
    window_rows = rows[-INGEST_WINDOW_SIZE:]
    joined = "\n".join((r["content"] or "") for r in window_rows if r["content"])
    if not joined.strip():
        return

    # Lazy import
    from .write import memory_write_impl

    try:
        await memory_write_impl(
            type="summary",
            content=joined,
            title=f"window:{conversation_id}:{n}",
            metadata=json.dumps({
                "kind": "window_chunk",
                "window_end_turn": n,
                "window_size": INGEST_WINDOW_SIZE,
                "source_message_ids": [r["id"] for r in window_rows],
            }),
            user_id=user_id,
            source="window_chunk",
            conversation_id=conversation_id,
            embed=True,
        )
    except Exception as e:
        logger.debug(f"window chunk emit failed: {e}")


async def _maybe_emit_gist_row(conversation_id: str, user_id: str) -> None:
    """Emit a heuristic gist row for a conversation once it has passed
    INGEST_GIST_MIN_TURNS turns, and every INGEST_GIST_STRIDE additional
    turns thereafter. The gist concatenates the first sentence of each
    message and a deduped list of capitalized tokens seen across the
    conversation — cheap, deterministic, no LLM."""
    if not conversation_id:
        return
    try:
        with _db() as db:
            rows = db.execute(
                "SELECT id, content FROM memory_items "
                "WHERE conversation_id = ? AND type = 'message' "
                "AND is_deleted = 0 ORDER BY created_at ASC",
                (conversation_id,),
            ).fetchall()
    except Exception as e:
        logger.debug(f"gist query failed: {e}")
        return
    n = len(rows)
    if n < INGEST_GIST_MIN_TURNS:
        return
    if ((n - INGEST_GIST_MIN_TURNS) % INGEST_GIST_STRIDE) != 0:
        return
    sentences: list[str] = []
    entities: list[str] = []
    seen_ent: set[str] = set()
    for r in rows:
        c = (r["content"] or "").strip()
        if not c:
            continue
        first = _EVENT_SENT_SPLIT.split(c, maxsplit=1)[0]
        if first:
            sentences.append(first[:200])
        for m in _EVENT_PROPER_NOUN.findall(c):
            if m not in seen_ent:
                seen_ent.add(m)
                entities.append(m)
            if len(entities) >= 16:
                break
    if not sentences:
        return
    gist = " | ".join(sentences[:12])
    if entities:
        gist = f"[{', '.join(entities[:16])}] {gist}"

    # Lazy import
    from .write import memory_write_impl

    try:
        await memory_write_impl(
            type="summary",
            content=gist,
            title=f"gist:{conversation_id}:{n}",
            metadata=json.dumps({
                "kind": "conversation_gist",
                "turn_count": n,
                "entities": entities[:16],
            }),
            user_id=user_id,
            source="conversation_gist",
            conversation_id=conversation_id,
            embed=True,
        )
    except Exception as e:
        logger.debug(f"gist emit failed: {e}")
