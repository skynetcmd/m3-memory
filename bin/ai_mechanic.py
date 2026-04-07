import sqlite3
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "memory", "agent_memory.db")

def repair_database():
    print("🛠️  Checking Database Schema...")
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Force-align project_decisions
        cursor.execute("DROP TABLE IF EXISTS project_decisions")
        cursor.execute("""CREATE TABLE project_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT, decision TEXT, rationale TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        
        # Force-align system_focus
        cursor.execute("DROP TABLE IF EXISTS system_focus")
        cursor.execute("CREATE TABLE system_focus (id INTEGER PRIMARY KEY, summary TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)")
        cursor.execute("INSERT INTO system_focus (id, summary) VALUES (1, 'System Repaired & Calibrated')")
        
        conn.commit()
        conn.close()
        print("✅ Database Schema Aligned.")
    except Exception as e:
        print(f"❌ Database Repair Failed: {e}")

def check_bridges():
    print("🛠️  Checking MCP Bridges...")
    bridges = ["custom_tool_bridge.py", "memory_bridge.py", "web_research_bridge.py", "grok_bridge.py", "debug_agent_bridge.py"]
    for bridge in bridges:
        path = os.path.join(BASE_DIR, "bin", bridge)
        if os.path.exists(path):
            print(f"✅ Bridge Found: {bridge}")
        else:
            print(f"⚠️  Missing Bridge: {bridge}")

if __name__ == "__main__":
    print("🚀 Starting AI-OS Emergency Repair...")
    repair_database()
    check_bridges()
    print("✨ Repair Cycle Finished. Restart your Pulse dashboard and AI CLI.")
