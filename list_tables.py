import sqlite3
def main():
    conn = sqlite3.connect('memory/agent_memory.db')
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name='synchronized_secrets'").fetchone()
    print(row[0])
if __name__ == "__main__":
    main()
