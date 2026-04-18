#!/usr/bin/env python3
"""
setup_memory.py — Bootstrap the m3-memory memory system on any OS.
Usage: python bin/setup_memory.py
All progress logged to stderr; final config JSON printed to stdout.
"""
import json
import os
import pathlib
import platform
import sqlite3
import subprocess
import sys

BASE   = pathlib.Path(__file__).parent.parent.resolve()
IS_WIN = platform.system() == "Windows"
VENV   = BASE / ".venv"
PY     = VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")
PIP    = VENV / ("Scripts/pip.exe"    if IS_WIN else "bin/pip")
REQS   = BASE / ("requirements-windows.txt" if IS_WIN else "requirements.txt")
DB     = BASE / "memory" / "agent_memory.db"
MIGS   = BASE / "memory" / "migrations"

def log(msg): print(f"[setup] {msg}", file=sys.stderr)

def run(*args, **kw):
    subprocess.run(args, check=True, **kw)

# 1. Create venv
if not PY.exists():
    log(f"Creating virtual environment at {VENV} ...")
    run(sys.executable, "-m", "venv", str(VENV))
else:
    log(f"Venv already exists at {VENV}")

# 2. Upgrade pip
log("Upgrading pip ...")
run(str(PY), "-m", "pip", "install", "--upgrade", "pip", "--quiet")

# 3. Install dependencies
log(f"Installing dependencies from {REQS.name} ...")
run(str(PY), "-m", "pip", "install", "-r", str(REQS), "--quiet")

# 4. Run migrations
log(f"Running migrations against {DB} ...")
os.makedirs(str(DB.parent), exist_ok=True)
conn = sqlite3.connect(str(DB))
if MIGS.exists():
    for sql_file in sorted(MIGS.glob("*.sql")):
        log(f"  Applying {sql_file.name} ...")
        conn.executescript(sql_file.read_text(encoding="utf-8"))
conn.commit()
conn.close()
log("Migrations complete.")

# 5. Print MCP config
py_path   = str(PY).replace("\\", "\\\\")
base_path = str(BASE).replace("\\", "\\\\")

config = {
    "memory": {
        "command": str(PY),
        "args": [str(BASE / "bin" / "memory_bridge.py")],
        "env": {
            "LM_STUDIO_EMBED_URL": "http://127.0.0.1:1234/v1/embeddings",
            "CHROMA_BASE_URL": os.environ.get("CHROMA_BASE_URL", "")
        }
    },
    "custom_pc_tool": {
        "command": str(PY),
        "args": [str(BASE / "bin" / "custom_tool_bridge.py")]
    },
    "grok_intel": {
        "command": str(PY),
        "args": [str(BASE / "bin" / "grok_bridge.py")]
    },
    "web_research": {
        "command": str(PY),
        "args": [str(BASE / "bin" / "web_research_bridge.py")]
    }
}

log("\n=== Paste this into ~/.claude/settings.json mcpServers ===")
print(json.dumps({"mcpServers": config}, indent=2))
log("Setup complete.")
