import sqlite3
import uuid

conn = sqlite3.connect("memory/agent_memory.db")
c = conn.cursor()
c.execute("SELECT rowid FROM memory_relationships WHERE id IS NULL OR id = ''")
rows = c.fetchall()
for r in rows:
    c.execute("UPDATE memory_relationships SET id = ? WHERE rowid = ?", (str(uuid.uuid4()), r[0]))
conn.commit()
print(f"Fixed {len(rows)} relationships in SQLite.")
