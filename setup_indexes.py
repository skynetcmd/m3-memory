import sqlite3
import os

DB_PATH = "memory/agent_memory.db"

def main():
    conn = sqlite3.connect(DB_PATH)
    try:
        print("Adding indexes...")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mi_source ON memory_items(source)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mr_from ON memory_relationships(from_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mr_to ON memory_relationships(to_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mr_rel_type ON memory_relationships(relationship_type)")
        conn.commit()
        print("Done.")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
