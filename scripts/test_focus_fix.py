import sys
import os
import sqlite3
from datetime import datetime

# Set up paths
sys.path.insert(0, os.path.abspath('bin'))

from m3_sdk import M3Context

def test_update_focus():
    ctx = M3Context()
    summary = "Verification: All bridges fixed and operational."
    
    try:
        with ctx.get_sqlite_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM system_focus WHERE id != 1")
            cursor.execute(
                "INSERT OR REPLACE INTO system_focus (id, summary, timestamp) VALUES (1, ?, ?)",
                (summary, datetime.now().isoformat()),
            )
            conn.commit()
            print(f"Success: System focus updated to: {summary}")
            
            # Verify
            cursor.execute("SELECT * FROM system_focus")
            row = cursor.fetchone()
            print(f"Verified row: {dict(row)}")
            
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    test_update_focus()
