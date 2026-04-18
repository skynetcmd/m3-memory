#!/usr/bin/env python3
"""
chatlog_embed_sweeper.py — lazy embed chat log rows missing embeddings.

Runs on a schedule (default every 30 min via install_schedules.py). Picks up
rows written with embed=False, embeds in batches using memory_core._embed_many,
and drains any spill-to-disk files from the async write queue.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from glob import glob
from pathlib import Path

# Setup path so we can import bin/ modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chatlog_config

logger = logging.getLogger("chatlog_embed_sweeper")


async def drain_spill(conn: sqlite3.Connection) -> int:
    """
    Drain spill JSONL files back into the chat log DB.
    Returns count of rows inserted.
    """
    spill_dir = chatlog_config.SPILL_DIR
    if not os.path.exists(spill_dir):
        return 0

    spill_files = glob(os.path.join(spill_dir, "*.jsonl"))
    if not spill_files:
        return 0

    total_drained = 0
    for spill_path in spill_files:
        try:
            rows_to_insert = []
            with open(spill_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        doc = json.loads(line)
                    except json.JSONDecodeError as e:
                        logger.warning("Skipping malformed JSON in %s: %s", spill_path, e)
                        continue

                    # Build memory_items row from spill entry
                    memory_id = str(uuid.uuid4())
                    role = doc.get("role", "unknown")
                    content = doc.get("content", "")
                    conversation_id = doc.get("conversation_id", "")
                    host_agent = doc.get("host_agent", "unknown")
                    provider = doc.get("provider", "unknown")
                    model_id = doc.get("model_id", "unknown")
                    timestamp = doc.get("timestamp", datetime.now(timezone.utc).isoformat())

                    # Build metadata JSON with provenance
                    metadata = {
                        "role": role,
                        "provider": provider,
                        "model_id": model_id,
                        "spill_source": True,
                    }
                    metadata_json = json.dumps(metadata)

                    # agent_id marks this as spilled
                    agent_id = f"{host_agent}:spill"

                    rows_to_insert.append((
                        memory_id,
                        content,
                        "",  # title (empty)
                        metadata_json,
                        "chat_log",  # type
                        agent_id,
                        conversation_id,
                        0,  # is_deleted
                        timestamp,
                    ))

            if rows_to_insert:
                try:
                    conn.executemany(
                        """
                        INSERT OR IGNORE INTO memory_items
                        (id, content, title, metadata_json, type, agent_id, conversation_id, is_deleted, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows_to_insert,
                    )
                    conn.commit()
                    total_drained += len(rows_to_insert)
                    logger.info("Drained %d rows from %s", len(rows_to_insert), spill_path)
                except sqlite3.Error as e:
                    logger.error("Failed to insert drained rows from %s: %s", spill_path, e)
                    continue

            # Delete the spill file on success
            try:
                os.remove(spill_path)
                logger.info("Deleted spill file: %s", spill_path)
            except OSError as e:
                logger.warning("Failed to delete spill file %s: %s", spill_path, e)

        except Exception as e:
            logger.error("Error processing spill file %s: %s", spill_path, e)

    return total_drained


async def embed_batch(
    conn: sqlite3.Connection,
    batch: list[tuple[str, str, str, str]],
    dry_run: bool = False,
) -> int:
    """
    Embed a batch of rows. Returns count embedded.
    batch format: [(id, content, title, metadata_json), ...]
    """
    if not batch:
        return 0

    texts = [content for _, content, _, _ in batch]

    # Import embedding function lazily
    from memory_core import _embed_many as embed_many
    from embedding_utils import pack as _pack

    try:
        embeddings = await embed_many(texts)
    except Exception as e:
        logger.error("Failed to embed batch: %s", e)
        return 0

    if not dry_run:
        try:
            rows_to_insert = []
            for (mem_id, _, _, _), (vec, model_str) in zip(batch, embeddings):
                if vec is not None:
                    packed = _pack(vec)
                    embed_id = str(uuid.uuid4())
                    rows_to_insert.append((
                        embed_id,
                        mem_id,
                        packed,
                        model_str,
                        len(vec),  # dim
                        datetime.now(timezone.utc).isoformat(),
                    ))

            if rows_to_insert:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO memory_embeddings
                    (id, memory_id, embedding, embed_model, dim, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    rows_to_insert,
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to insert embeddings: %s", e)
            return 0

    return len(batch)


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """Check if a table exists in the DB."""
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


async def get_unembed_count(conn: sqlite3.Connection) -> int:
    """Count rows needing embeddings."""
    if not table_exists(conn, "memory_items") or not table_exists(conn, "memory_embeddings"):
        return 0

    try:
        row = conn.execute(
            """
            SELECT COUNT(*) as cnt
            FROM memory_items
            WHERE type='chat_log'
              AND is_deleted=0
              AND id NOT IN (SELECT memory_id FROM memory_embeddings)
            """
        ).fetchone()
        return row["cnt"] if row else 0
    except sqlite3.Error as e:
        logger.error("Failed to count unembedded rows: %s", e)
        return 0


def load_state() -> dict:
    """Load state file, or return defaults."""
    state_path = chatlog_config.STATE_FILE
    if not os.path.exists(state_path):
        return {
            "embed_backlog": 0,
            "last_sweeper_run_at": None,
            "last_sweeper_rows_embedded": 0,
            "last_sweeper_spill_drained": 0,
            "queue_depth": 0,
        }
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to load state file: %s", e)
        return {
            "embed_backlog": 0,
            "last_sweeper_run_at": None,
            "last_sweeper_rows_embedded": 0,
            "last_sweeper_spill_drained": 0,
            "queue_depth": 0,
        }


def save_state(state: dict) -> None:
    """Save state atomically with rename."""
    state_dir = os.path.dirname(chatlog_config.STATE_FILE)
    os.makedirs(state_dir, exist_ok=True)
    tmp_path = chatlog_config.STATE_FILE + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, chatlog_config.STATE_FILE)
    except OSError as e:
        logger.error("Failed to save state: %s", e)


async def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Lazy embed chat log rows missing embeddings."
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=None,
        help="Batch size (default from config.embed_sweeper.batch_size)",
    )
    parser.add_argument(
        "--max-per-run",
        type=int,
        default=None,
        help="Max rows per run (default from CHATLOG_EMBED_MAX_PER_RUN env or 10000)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Query and log but don't embed",
    )
    parser.add_argument(
        "--drain-spill",
        action="store_true",
        help="Process spill files before embedding",
    )
    args = parser.parse_args()

    # Resolve config
    cfg = chatlog_config.resolve_config()
    batch_size = args.batch or cfg.embed_sweeper.batch_size
    max_per_run = (
        args.max_per_run
        or int(os.environ.get("CHATLOG_EMBED_MAX_PER_RUN", "10000"))
    )

    # Open DB connection
    db_path = chatlog_config.chatlog_db_path()
    if not os.path.exists(db_path):
        logger.info("Chat log DB does not exist yet: %s", db_path)
        return 0

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        logger.error("Failed to open chat log DB: %s", e)
        return 1

    start_time = asyncio.get_event_loop().time()

    try:
        spill_drained = 0
        rows_embedded = 0
        batches_processed = 0

        # Check if schema exists
        if not table_exists(conn, "memory_items"):
            logger.info("Chat log schema not initialized yet; nothing to do")
            return 0

        # Drain spill if requested or if files exist
        if args.drain_spill or os.path.exists(chatlog_config.SPILL_DIR):
            spill_drained = await drain_spill(conn)
            if spill_drained > 0:
                logger.info("Drained %d rows from spill", spill_drained)

        # Query and embed batches
        total_to_embed = max_per_run
        while total_to_embed > 0:
            batch_limit = min(batch_size, total_to_embed)
            try:
                rows = conn.execute(
                    """
                    SELECT id, content, title, metadata_json
                    FROM memory_items
                    WHERE type='chat_log'
                      AND is_deleted=0
                      AND id NOT IN (SELECT memory_id FROM memory_embeddings)
                    ORDER BY created_at
                    LIMIT ?
                    """,
                    (batch_limit,),
                ).fetchall()
            except sqlite3.Error as e:
                logger.error("Failed to query unembedded rows: %s", e)
                return 1

            if not rows:
                break

            if args.dry_run:
                logger.info(
                    "DRY RUN: would embed %d rows in batch %d",
                    len(rows),
                    batches_processed + 1,
                )
                rows_embedded += len(rows)
            else:
                embedded = await embed_batch(conn, rows, dry_run=False)
                rows_embedded += embedded
                logger.info("Embedded %d rows in batch %d", embedded, batches_processed + 1)

            batches_processed += 1
            total_to_embed -= len(rows)

        # Get remaining backlog count
        backlog = await get_unembed_count(conn)

        # Update state
        state = load_state()
        state["embed_backlog"] = backlog
        state["last_sweeper_run_at"] = datetime.now(timezone.utc).isoformat()
        state["last_sweeper_rows_embedded"] = rows_embedded
        state["last_sweeper_spill_drained"] = spill_drained
        save_state(state)

        elapsed = asyncio.get_event_loop().time() - start_time

        if rows_embedded > 0 or spill_drained > 0:
            logger.info(
                "Embedded %d rows in %d batches (%.1fs), spill drained: %d, backlog remaining: %d",
                rows_embedded,
                batches_processed,
                elapsed,
                spill_drained,
                backlog,
            )
        else:
            logger.info("Nothing to do (backlog: %d)", backlog)

        return 0

    except Exception as e:
        logger.exception("Sweeper failed: %s", e)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(name)s: [%(levelname)s] %(message)s",
    )
    sys.exit(asyncio.run(main()))
