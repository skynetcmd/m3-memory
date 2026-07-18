import logging
import os
import sys

# Dynamically resolve project root
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "bin"))

from m3_sdk import M3Context

ctx = M3Context.for_db(None)
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
# session_handoff is a CORE-store table, so it is created/read/written through
# the backend-aware seam (mc._db()). On a PG-primary deployment the old
# sqlite3.connect(DB_PATH) created/wrote a stale SQLite file that the live store
# never sees. The DDL diverges per backend (SQLite INTEGER PRIMARY KEY +
# DATETIME vs PG SERIAL + TIMESTAMPTZ), so the id/timestamp column types are
# chosen from the active backend.
def _ensure_table():
    import memory_core as mc
    from memory.backends import active_backend
    is_pg = active_backend().name != "sqlite"
    id_col = "id SERIAL PRIMARY KEY" if is_pg else "id INTEGER PRIMARY KEY"
    ts_col = (
        "timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP"
        if is_pg
        else "timestamp DATETIME DEFAULT CURRENT_TIMESTAMP"
    )
    with mc._db() as conn:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS session_handoff ({id_col}, project TEXT, "
            f"summary TEXT, next_steps TEXT, {ts_col})"
        )

_ensure_table()

@mcp.tool()
def save_handoff(project: str, summary: str, next_steps: str):
    """Saves the current AI session state for another agent to resume."""
    try:
        import memory_core as mc
        from memory.backends import dialect
        _p = dialect().param()
        with mc._db() as conn:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                f"INSERT INTO session_handoff (project, summary, next_steps, timestamp) "
                f"VALUES ({_p}, {_p}, {_p}, {_p})",
                (project, summary, next_steps, now),
            )
        return f"State saved for {project}."
    except Exception as exc:
        return f"Error saving handoff: {type(exc).__name__}"

if __name__ == "__main__":
    mcp.run()
