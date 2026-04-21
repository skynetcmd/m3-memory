import argparse
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from m3_sdk import add_database_arg, resolve_db_path

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# This tool DROPs and recreates tables — it must never run against the live
# memory store by accident. The argparse `--database` flag has no default,
# so callers must pass a path explicitly.

def repair_database(db_path: str):
    print(f"🛠️  Checking Database Schema at {db_path}...")
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute("DROP TABLE IF EXISTS project_decisions")
        cursor.execute("""CREATE TABLE project_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT, decision TEXT, rationale TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")

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
    parser = argparse.ArgumentParser(
        description="Emergency schema repair (DESTRUCTIVE: drops project_decisions and system_focus)."
    )
    add_database_arg(parser)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Required. Confirms you understand this drops tables.",
    )
    args = parser.parse_args()

    if not args.database:
        parser.error("--database is required (this tool will not run against the default DB without an explicit path).")
    if not args.force:
        parser.error("--force is required to acknowledge the destructive DROP TABLE operations.")

    db_path = resolve_db_path(args.database)
    print("🚀 Starting AI-OS Emergency Repair...")
    repair_database(db_path)
    check_bridges()
    print("✨ Repair Cycle Finished. Restart your Pulse dashboard and AI CLI.")
