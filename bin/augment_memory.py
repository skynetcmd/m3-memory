#!/usr/bin/env python3
"""Offline post-ingest augmentation utilities for memory_items.

Two independent operations that improve retrieval quality on an already-
ingested DB without re-running the full ingest pipeline:

  link-adjacent
      Create ``related`` relationship edges between consecutive turns
      (turn N -> turn N+1) within each conversation. Graph expansion then
      bridges the gap between an assistant echo and the user statement
      that prompted it, which helps user-fact retrieval even without the
      intent-routing predecessor-pull being enabled.

  enrich-titles
      Use the SLM (``slm_intent.extract_entities``) to prefix user-turn
      titles with 1-3 pithy entities. "Sparky, Golden Retriever | ..."
      makes BM25 hit on the proper noun even when the body text uses a
      pronoun. Requires ``M3_SLM_CLASSIFIER=1`` and the entity_extract
      profile — off otherwise.

Both operations are idempotent-ish: link-adjacent uses memory_link_impl
which dedupes on (from_id, to_id, relationship_type); enrich-titles only
rewrites a title if the extracted prefix isn't already present.

Usage:
    python bin/augment_memory.py link-adjacent --database memory/x.db
    python bin/augment_memory.py enrich-titles --database memory/x.db --limit 500
    python bin/augment_memory.py all --database memory/x.db  # both in sequence
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "bin"))

from m3_sdk import add_database_arg, resolve_db_path  # noqa: E402
from memory_core import _db, memory_link_impl  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(name)s: [%(levelname)s] %(message)s")
logger = logging.getLogger("augment_memory")


async def link_adjacent_turns(user_id: str = "", limit: int = 10000) -> int:
    """Create 'related' edges between consecutive turns in each conversation.

    Returns the count of link calls issued. memory_link_impl is responsible
    for any FK / dedup enforcement — this function does not pre-check for
    existing edges, so a re-run against an already-linked DB is cheap but
    not zero-cost (it hits the link impl once per adjacent pair).
    """
    logger.info("Scanning memory_items for adjacency candidates...")
    clauses = [
        "is_deleted = 0",
        "conversation_id IS NOT NULL",
        "json_extract(metadata_json, '$.turn_index') IS NOT NULL",
    ]
    params: list = []
    if user_id:
        clauses.append("user_id = ?")
        params.append(user_id)
    where = " AND ".join(clauses)
    sql = (
        f"SELECT id, conversation_id, "
        f"       CAST(json_extract(metadata_json, '$.turn_index') AS INTEGER) AS turn_index "
        f"FROM memory_items WHERE {where} "
        f"ORDER BY conversation_id, turn_index "
        f"LIMIT ?"
    )
    params.append(limit)

    with _db() as db:
        rows = db.execute(sql, params).fetchall()
    logger.info(f"Scanned {len(rows)} candidate turns.")

    # Group by conversation, then chain N -> N+1.
    by_conv: dict[str, list] = {}
    for r in rows:
        by_conv.setdefault(r["conversation_id"], []).append(r)

    links = 0
    for cid, turns in by_conv.items():
        turns.sort(key=lambda x: x["turn_index"])
        for i in range(len(turns) - 1):
            memory_link_impl(turns[i]["id"], turns[i + 1]["id"], "related")
            links += 1
    logger.info(f"Issued {links} adjacency links across {len(by_conv)} conversations.")
    return links


async def enrich_user_titles(user_id: str = "", limit: int = 200) -> int:
    """Prefix user-turn titles with SLM-extracted entities.

    Updates ``UPDATE memory_items SET title = '<entities> | <orig title>'``
    for each processed row. Skips rows whose title already starts with a
    '|' separator (indicator of a prior augment run). Only touches rows
    where metadata_json.role == 'user' — assistant echoes don't need the
    bump and are often long-form.

    Returns the count of rows updated. Returns 0 silently when
    M3_SLM_CLASSIFIER is off (extract_entities returns None every call).
    """
    from slm_intent import extract_entities

    clauses = [
        "is_deleted = 0",
        "json_extract(metadata_json, '$.role') = 'user'",
    ]
    params: list = []
    if user_id:
        clauses.append("user_id = ?")
        params.append(user_id)
    where = " AND ".join(clauses)
    sql = f"SELECT id, content, title FROM memory_items WHERE {where} LIMIT ?"
    params.append(limit)

    with _db() as db:
        rows = db.execute(sql, params).fetchall()
    logger.info(f"Candidate user turns: {len(rows)}")

    updated = 0
    skipped_already = 0
    skipped_empty = 0
    for r in rows:
        title = r["title"] or ""
        # Our marker: prefix ends with ' | '. If the title already starts
        # with an entity prefix, don't re-stack on re-run.
        if " | " in title and not title.startswith("[") and not title.startswith("<"):
            head = title.split(" | ", 1)[0]
            if head and len(head) < 80:
                skipped_already += 1
                continue

        entities = await extract_entities(r["content"] or "")
        if entities is None:
            # Gate off or call failed — stop the loop, don't thrash the DB.
            logger.warning("extract_entities returned None; stopping (gate off or SLM down).")
            break
        # Filter the "no entity found" sentinel the profile emits.
        entities = [e for e in entities if e and e != "-"]
        if not entities:
            skipped_empty += 1
            continue

        prefix = ", ".join(entities[:3])
        new_title = f"{prefix} | {title}" if title else prefix
        if new_title == title:
            continue
        with _db() as db:
            db.execute("UPDATE memory_items SET title = ? WHERE id = ?", (new_title, r["id"]))
        updated += 1

    logger.info(
        f"Enriched {updated}; skipped {skipped_already} (already prefixed), "
        f"{skipped_empty} (no entities extracted)."
    )
    return updated


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("link-adjacent", help="Create adjacency edges between consecutive turns")
    sp.add_argument("--user-id", default="", help="Restrict to a single user_id")
    sp.add_argument("--limit", type=int, default=10000, help="Max candidate turns to scan")
    add_database_arg(sp)

    sp = sub.add_parser("enrich-titles", help="Prefix user-turn titles with SLM-extracted entities")
    sp.add_argument("--user-id", default="", help="Restrict to a single user_id")
    sp.add_argument("--limit", type=int, default=200, help="Max rows to enrich in this run")
    add_database_arg(sp)

    sp = sub.add_parser("all", help="Run link-adjacent then enrich-titles")
    sp.add_argument("--user-id", default="", help="Restrict to a single user_id")
    sp.add_argument("--limit", type=int, default=200, help="Limit for the enrich-titles phase")
    add_database_arg(sp)

    return p


async def _main() -> int:
    args = _build_parser().parse_args()
    if getattr(args, "database", None):
        os.environ["M3_DATABASE"] = args.database
    resolved = resolve_db_path(getattr(args, "database", None))
    logger.info(f"Target DB: {resolved}")

    if args.command == "link-adjacent":
        await link_adjacent_turns(user_id=args.user_id, limit=args.limit)
    elif args.command == "enrich-titles":
        await enrich_user_titles(user_id=args.user_id, limit=args.limit)
    elif args.command == "all":
        await link_adjacent_turns(user_id=args.user_id, limit=max(args.limit * 50, 10000))
        await enrich_user_titles(user_id=args.user_id, limit=args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
