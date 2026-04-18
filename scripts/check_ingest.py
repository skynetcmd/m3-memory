import sqlite3, json

conn = sqlite3.connect('memory/agent_memory.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

print("Searching for 'support group' for conv-26...")
rows = c.execute("SELECT id, title, content, metadata_json FROM memory_items WHERE user_id='conv-26' AND content LIKE '%support group%'").fetchall()
for r in rows:
    print(f"ID: {r['id']} | Title: {r['title']}")
    print(f"Content: {r['content']}")
    print(f"Metadata: {r['metadata_json']}")
    print("-" * 20)

print("\nSearching for Session 1 items...")
rows = c.execute("SELECT id, title, content FROM memory_items WHERE user_id='conv-26' AND title LIKE '%S1:%'").fetchall()
print(f"Found {len(rows)} items for Session 1")
for r in rows[:3]:
    print(f"  {r['title']}: {r['content'][:50]}...")
