import sqlite3
import os

DB_PATH = "memory/agent_memory.db"

def main():
    if not os.path.exists(DB_PATH):
        print(f"DB not found at {DB_PATH}")
        return
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        print("\nMiniLM entry count:")
        count = conn.execute("SELECT COUNT(*) FROM memory_embeddings WHERE embed_model='minilm-dml'").fetchone()[0]
        print(f"minilm-dml: {count}")

        print("\nEmbeddings schema:")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_embeddings'").fetchone()
        print(row['sql'])

        print("\nTop 10 sources:")
        rows = conn.execute("SELECT source, COUNT(*) as cnt FROM memory_items GROUP BY source ORDER BY cnt DESC LIMIT 10").fetchall()
        for row in rows:
            print(dict(row))

        print("\nLongMemEval data remaining:")
        rows = conn.execute("SELECT type, COUNT(*) as cnt FROM memory_items WHERE source LIKE 'longmemeval%' GROUP BY type").fetchall()
        for row in rows:
            print(dict(row))

        print("\nIndexes on memory_items:")
        rows = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='memory_items'").fetchall()
        for row in rows:
            print(f"Index: {row['name']}\n{row['sql']}\n")

        print("\nQueue counts:")
        count = conn.execute("SELECT COUNT(*) FROM chroma_sync_queue").fetchone()[0]
        print(f"chroma_sync_queue: {count}")
        
    except sqlite3.Error as e:
        print(f"Error: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
