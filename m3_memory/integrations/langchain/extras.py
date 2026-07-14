"""m3-native extras — the differentiators mem0/LangMem can't do (§0b).

A pure mem0 ``.add()``/``.search()`` shadow HIDES everything that makes m3 worth
switching to. This mixin adds first-class, typed methods for the five unmet
needs — contradiction handling, temporal reasoning, commanded forgetting,
hybrid+graph retrieval, and true extraction — over the SAME canonical dispatch,
so they can't drift from the mem0-compat surface.

These are **m3-native** (typed, discoverable methods we build), NOT the raw
``.call()`` **m3-passthru** escape hatch (§8 terminology). They never change the
mem0-compat signatures — a mem0 migrant's code stays byte-identical; the extras
are additive method names.

The mixin assumes the host class provides ``self._client`` (an ``M3Client``) and
``self._require_user`` (tenancy enforcement, §7).
"""

from __future__ import annotations

from typing import Any, Optional

from . import mapping


class M3ExtrasMixin:
    """m3-native methods folded into ``Memory``/``M3Memory``."""

    # These are provided by the host class (Memory); declared for the type reader.
    _client: Any
    _require_user: Any

    # ── contradiction handling (§0b unmet-need #1, m3's strongest edge) ───────
    def supersede(
        self,
        old_id: str,
        new_content: str,
        *,
        user_id: Optional[str] = None,
        **kwargs: Any,
    ) -> dict:
        """Deterministically supersede ``old_id`` with ``new_content``.

        Unlike ``.add()``'s heuristic contradiction detection (cosine + title),
        this targets a SPECIFIC prior memory — a real supersession edge, not flat
        dedup. Bi-temporal: the old memory is closed, not destroyed (§0b temporal
        reasoning still time-travels to it via ``as_of``).
        """
        uid = self._require_user(user_id)
        raw = self._client._tool(
            "memory_supersede",
            old_id=old_id,
            content=new_content,
            user_id=uid,
            scope="user",
            **kwargs,
        )
        return {"old_id": old_id, "new_id": mapping.parse_written_id(raw)}

    # ── commanded forgetting (§0b unmet-need #3 — mem0 has NO forget verb) ────
    def forget(self, *, user_id: Optional[str] = None, **_ignored: Any) -> dict:
        """GDPR Art. 17 hard-erase EVERY memory for a user (irreversible).

        The first-class ``forget`` verb mem0's surface lacks. Ungated typed
        method (§2.1b) — the user invoked it explicitly. ``user_id`` mandatory.
        """
        uid = self._require_user(user_id)
        result = self._client._tool("gdpr_forget", user_id=uid)
        return {"forgotten_user": uid, "result": result}

    # ── hybrid + graph retrieval (§0b unmet-need #4) ──────────────────────────
    def related(self, memory_id: str, *, depth: int = 1, **_ignored: Any) -> dict:
        """KG traversal from a memory — the graph recall LangMem/mem0 lack.

        Returns m3's neighborhood graph (supersession/entity/link edges) so a
        chain can reason over connected facts, not just vector-nearest ones.
        """
        result = self._client._tool("memory_graph", memory_id=memory_id, depth=depth)
        return {"memory_id": memory_id, "depth": depth, "graph": result}

    def history(self, memory_id: str, *, limit: int = 20, **_ignored: Any) -> Any:
        """Bi-temporal history of a memory (its supersession chain over time)."""
        return self._client._tool("memory_history", memory_id=memory_id, limit=limit)
