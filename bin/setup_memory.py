#!/usr/bin/env python3
"""
setup_memory.py — Bootstrap the m3-memory memory system on any OS.
Usage: python bin/setup_memory.py
All progress logged to stderr; final config JSON printed to stdout.
"""
import json
import os
import pathlib
import sqlite3
import subprocess
import sys

BASE   = pathlib.Path(__file__).parent.parent.resolve()
IS_WIN = sys.platform == "win32"


# bin/ on sys.path so this bootstrap script can reach its siblings. Done once,
# at import, rather than inside the helper: a function that mutates sys.path on
# every call grows it without bound the day it gains a second caller.
_BIN = str(pathlib.Path(__file__).resolve().parent)
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

# generate_configs is the SINGLE owner of "is this an installed layout?" and is
# stdlib-only, so it is importable even this early in bootstrap. Import the
# predicate rather than copying it: a local copy is a §10a drift hazard, and
# this module's whole purpose is to agree with generate_configs about which
# interpreter is canonical — disagreeing is the bug being fixed here.
from generate_configs import _is_installed_layout  # noqa: E402

# Interpreter resolution. A repo-local .venv is correct for a SOURCE checkout,
# but when this script ships inside an installed wheel BASE is
# <site-packages>/m3_memory — and building a venv THERE nests one environment
# inside another, then runs every step below with an interpreter that has no
# m3_memory importable. That is what produced
# "WARN: could not seed shared embedder config: No module named 'm3_memory'"
# and a spurious "requirements.txt not found": both are symptoms of the same
# wrong BASE. On an installed layout the interpreter already running this code
# IS the payload's interpreter, so use it and create nothing.
_INSTALLED = _is_installed_layout(str(BASE))  # delegate takes a str, not a Path
VENV   = None if _INSTALLED else BASE / ".venv"
# (PIP was defined here and never used; dropped rather than set to None, which
# would hand a later caller a NoneType error instead of a NameError naming it.)
PY = (pathlib.Path(sys.executable) if _INSTALLED
      else VENV / ("Scripts/python.exe" if IS_WIN else "bin/python"))
# Prefer a Windows-specific requirements file if one exists, else fall back to the
# common requirements.txt (the windows variant is optional and may be absent).
_req_win = BASE / "requirements-windows.txt"
REQS   = _req_win if (IS_WIN and _req_win.exists()) else BASE / "requirements.txt"
# Bootstrap honors --database (positional for simplicity) and M3_DATABASE env.
# Called before m3_sdk is importable in a fresh checkout, so resolution is
# kept self-contained rather than delegated to resolve_db_path.
_override = None
if len(sys.argv) > 1 and sys.argv[1].startswith("--database="):
    _override = sys.argv[1].split("=", 1)[1]
elif "--database" in sys.argv:
    i = sys.argv.index("--database")
    if i + 1 < len(sys.argv):
        _override = sys.argv[i + 1]
DB     = pathlib.Path(_override or os.environ.get("M3_DATABASE") or (BASE / "memory" / "agent_memory.db"))
MIGS   = BASE / "memory" / "migrations"

def log(msg): print(f"[setup] {msg}", file=sys.stderr)

def run(*args, **kw):
    subprocess.run(args, check=True, **kw)

# 1. Create venv — only for a source checkout; an installed payload reuses the
#    interpreter that launched us (see the resolution note above).
if _INSTALLED:
    log(f"Installed layout detected; using the running interpreter {PY}")
elif not PY.exists():
    log(f"Creating virtual environment at {VENV} ...")
    run(sys.executable, "-m", "venv", str(VENV))
else:
    log(f"Venv already exists at {VENV}")

# 2-3. Dependencies. An installed payload got its dependencies from the wheel
#      that installed it; re-resolving them here would either no-op or fight the
#      installing tool (pipx owns that environment). Only a source checkout,
#      whose freshly created venv is empty, needs this.
if _INSTALLED:
    log("Installed layout; dependencies come from the wheel — skipping pip.")
elif not REQS.exists():
    log(f"WARNING: {REQS} not found; skipping dependency install.")
else:
    log("Upgrading pip ...")
    run(str(PY), "-m", "pip", "install", "--upgrade", "pip", "--quiet")
    log(f"Installing dependencies from {REQS.name} ...")
    run(str(PY), "-m", "pip", "install", "-r", str(REQS), "--quiet")

# 4. Run migrations — forward only, in numeric order.
#    Apply .up.sql and bare NNN_*.sql; NEVER .down.sql (those are rollbacks and
#    would undo a migration that hasn't been applied yet). Sort by the leading
#    integer prefix so ordering is correct regardless of zero-padding, and so a
#    migration's .up never sorts after the next migration's files.
log(f"Running migrations against {DB} ...")
os.makedirs(str(DB.parent), exist_ok=True)


def _mig_key(p):
    stem = p.name.split("_", 1)[0]
    try:
        return (int(stem), p.name)
    except ValueError:
        return (1 << 30, p.name)  # non-numeric prefixes last, stable by name


conn = sqlite3.connect(str(DB))
if MIGS.exists():
    forward = [
        p for p in MIGS.glob("*.sql")
        if not p.name.endswith(".down.sql")
    ]
    for sql_file in sorted(forward, key=_mig_key):
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
            "LM_STUDIO_EMBED_URL": "http://127.0.0.1:1234/v1/embeddings"
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

# 6. Detect a Claude Code install and offer the recommended hook install.
#    This is the SAFE, re-runnable path: it merges m3's SessionStart capture-check
#    hook + PreCompact/Stop hooks + statusLine + mcpServers into the live
#    settings.json idempotently (an upgrade replaces m3's own entries in place —
#    no duplicate or conflicting lines), backing up first and prompting before
#    writing. Prefer this over the manual paste below.
claude_dir = pathlib.Path(os.path.expanduser("~")) / ".claude"
if claude_dir.is_dir():
    log("")
    log("Detected a Claude Code install (~/.claude).")
    log("RECOMMENDED (safe, re-runnable): auto-install m3 hooks + statusLine + MCP")
    log("servers into ~/.claude/settings.json. Re-running upgrades in place without")
    log("duplicate or conflicting lines, and backs up your current settings first:")
    log("")
    log(f'    "{PY}" "{BASE / "bin" / "generate_configs.py"}" --install-claude')
    log("")
    log("Add --yes to skip the confirmation prompt, or --dry-run to preview only.")
else:
    log("")
    log("No ~/.claude install detected. To wire m3 into Claude Code later, run:")
    log(f'    "{PY}" "{BASE / "bin" / "generate_configs.py"}" --install-claude')

log("\n=== Manual fallback — paste this into ~/.claude/settings.json mcpServers ===")
print(json.dumps({"mcpServers": config}, indent=2))
log("Setup complete.")
