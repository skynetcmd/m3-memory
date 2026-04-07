import sys
import os
import sqlite3
import logging

# Dynamically resolve project root
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "bin"))

from m3_sdk import M3Context
ctx = M3Context()
DB_PATH = ctx.db_path

logger = logging.getLogger("session_handoff")

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    logger.error("FastMCP not found. Ensure dependencies are installed in the venv.")
    sys.exit(1)

mcp = FastMCP("Session Manager")

from datetime import datetime, timezone

# Ensure table exists at module load (M13)
def _ensure_table():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS session_handoff (id INTEGER PRIMARY KEY, project TEXT, summary TEXT, next_steps TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)")

_ensure_table()

@mcp.tool()
def save_handoff(project: str, summary: str, next_steps: str):
    """Saves the current AI session state for another agent to resume."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute("INSERT INTO session_handoff (project, summary, next_steps, timestamp) VALUES (?, ?, ?, ?)", (project, summary, next_steps, now))
        return f"State saved for {project}."
    except Exception as exc:
        return f"Error saving handoff: {type(exc).__name__}"

if __name__ == "__main__":
    mcp.run()
