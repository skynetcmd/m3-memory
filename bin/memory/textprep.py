"""Pure text-prep helpers for the embed pipeline: anchor augmentation and
content hashing.

Split out of embed.py per DESIGN_PHILOSOPHIES §2: these functions read no
module-level mutable state (no breaker, no client/cache/semaphore, no
`global`), so they're safe to live in their own module. `_content_hash`'s
`@lru_cache` is a function-local (decorator-owned) cache, not a module
global read/reassigned via `global` — it moves cleanly. The stateful
cascade (breakers, embedder singleton, HTTP client, caches, semaphores)
stays in embed.py, which re-imports these names for backward compatibility
(`from memory.embed import _content_hash`, etc.) and via memory_core's
lazy registry.

Do NOT import `embed` here — that would create a cycle. `util.py` is safe to
import: per its own docstring it is pure and never imports other
memory-package modules, so `.textprep -> .util` cannot cycle back.
"""
from __future__ import annotations

import json
from functools import lru_cache as _lru_cache

from .util import sha256_hex as _sha256_hex


def _augment_embed_text_with_anchors(embed_text: str, metadata: str | dict | None) -> str:
    """Prepend `[anchor1, anchor2]` to text from metadata['temporal_anchors']."""
    if not embed_text:
        return embed_text
    if not metadata:
        return embed_text
    try:
        meta = metadata if isinstance(metadata, dict) else json.loads(metadata)
    except (json.JSONDecodeError, TypeError):
        return embed_text
    anchors = meta.get("temporal_anchors")
    if not isinstance(anchors, (list, tuple)) or not anchors:
        return embed_text
    tags: list[str] = []
    for a in anchors:
        if not a:
            continue
        if isinstance(a, str):
            tags.append(a[:10])
        elif isinstance(a, dict):
            v = a.get("iso") or a.get("date") or a.get("value")
            if isinstance(v, str):
                tags.append(v[:10])
    if not tags:
        return embed_text
    return "[" + ", ".join(tags) + "] " + embed_text


@_lru_cache(maxsize=512)
def _content_hash(content: str) -> str:
    """sha256 of (content or "") UTF-8 bytes, lru-cached at 512 entries.

    Called once per embed and N times per chatlog write pass; sees frequent
    repeats during bulk re-embed and chatlog drain. Cache key is the raw
    content string — modest memory footprint for typical memory bodies
    (under a few KB each). 512 entries is enough to absorb repeats within a
    single chatlog drain without unbounded growth.
    """
    return _sha256_hex((content or "").encode("utf-8"))
