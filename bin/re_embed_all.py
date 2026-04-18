import asyncio
import os
import sqlite3
import sys

# Add bin to path for imports
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(BASE_DIR, "bin"))

from memory_core import DB_PATH, _embed, _pack


async def re_embed_all():
    print(f"Connecting to {DB_PATH}...")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    items = conn.execute("SELECT id, content, title FROM memory_items WHERE is_deleted = 0").fetchall()
    print(f"Found {len(items)} active items to re-embed.")

    updated = 0
    for item in items:
        rid = item['id']
        text = item['content'] or item['title'] or ""

        if not text.strip():
            print(f"[{updated+1}/{len(items)}] Skipping {rid} (no embeddable text)")
            continue

        print(f"[{updated+1}/{len(items)}] Re-embedding {rid}...")
        vec, model = await _embed(text)

        if vec:
            blob = _pack(vec)
            existing = conn.execute(
                "SELECT id FROM memory_embeddings WHERE memory_id = ?", (rid,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE memory_embeddings SET embedding = ?, embed_model = ?, dim = ? WHERE memory_id = ?",
                    (blob, model, len(vec), rid)
                )
            else:
                import uuid
                conn.execute(
                    "INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim) VALUES (?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), rid, blob, model, len(vec))
                )
            updated += 1
        else:
            print(f"FAILED to embed {rid}")

    conn.commit()
    conn.close()
    print(f"Successfully re-embedded {updated} items.")

if __name__ == "__main__":
    asyncio.run(re_embed_all())
