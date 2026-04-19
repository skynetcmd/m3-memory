#!/usr/bin/env python3
"""
cli_kb_browse.py — Browse knowledge base entries in rank (importance) order.
Cross-platform: macOS, Windows, Linux.

Usage:
    python bin/cli_kb_browse.py              # all entries, paged
    python bin/cli_kb_browse.py -n 20        # top 20
    python bin/cli_kb_browse.py -t fact      # filter by type
    python bin/cli_kb_browse.py -s proxmox   # search title/content
    python bin/cli_kb_browse.py --no-pager   # dump all, no paging
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import textwrap
from pathlib import Path

# ── Locate DB ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT   = SCRIPT_DIR.parent
DB_PATH     = REPO_ROOT / "memory" / "agent_memory.db"

sys.path.insert(0, str(REPO_ROOT / "bin"))
from m3_sdk import resolve_venv_python


def ensure_venv():
    venv_python = resolve_venv_python()
    if os.path.exists(venv_python) and sys.executable != venv_python:
        # venv_python is an absolute path within the project root, so this is safe.
        os.execl(venv_python, venv_python, *sys.argv)  # nosec B606

ensure_venv()

# ── Terminal width ────────────────────────────────────────────────────────────
try:
    COLS = os.get_terminal_size().columns
except OSError:
    COLS = 100
SEP  = "─" * COLS
SEP2 = "═" * COLS

# ── ANSI colours (disabled on Windows unless ANSICON/WT) ─────────────────────
def _ansi(code: str) -> str:
    if not sys.stdout.isatty() and not os.environ.get("FORCE_COLOR"):
        return ""
    if sys.platform == "win32" and not os.environ.get("WT_SESSION") and not os.environ.get("ANSICON"):
        return ""
    return f"\033[{code}m"

RESET  = _ansi("0")
BOLD   = _ansi("1")
DIM    = _ansi("2")
CYAN   = _ansi("36")
YELLOW = _ansi("33")
GREEN  = _ansi("32")
BLUE   = _ansi("34")
RED    = _ansi("31")
MAGENTA = _ansi("35")

TYPE_COLOURS = {
    "fact":             GREEN,
    "decision":         YELLOW,
    "knowledge":        CYAN,
    "project":          BLUE,
    "note":             DIM,
    "local_device":     MAGENTA,
    "reference":        DIM,
    "network_config":   CYAN,
    "infrastructure":   BLUE,
    "home_automation":  YELLOW,
}

def type_colour(t: str) -> str:
    return TYPE_COLOURS.get(t.lower(), "") if t else ""

# ── Importance bar ────────────────────────────────────────────────────────────
def importance_bar(score: float, width: int = 10) -> str:
    filled = round(score * width)
    bar = "█" * filled + "░" * (width - filled)
    colour = GREEN if score >= 0.9 else YELLOW if score >= 0.7 else RED
    return f"{colour}{bar}{RESET} {score:.2f}"

# ── Fetch entries ─────────────────────────────────────────────────────────────
def fetch(db_path: Path, type_filter: str | None, search: str | None, limit: int | None):
    if not db_path.exists():
        sys.exit(f"DB not found: {db_path}")
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    query = """
        SELECT id, type, title, content, metadata_json, importance,
               origin_device, change_agent, created_at, updated_at
        FROM memory_items
        WHERE is_deleted = 0
    """
    params: list = []

    if type_filter:
        is_exact = (type_filter.startswith('"') and type_filter.endswith('"')) or (type_filter.startswith("'") and type_filter.endswith("'"))
        actual_type = type_filter[1:-1] if is_exact else type_filter
        if is_exact:
            query += " AND type = ?"
            params.append(actual_type)
        else:
            actual_type = actual_type.replace("*", "%")
            if not actual_type.endswith("%"):
                actual_type += "%"
            query += " AND LOWER(type) LIKE LOWER(?)"
            params.append(actual_type)

    if search:
        is_exact = (search.startswith('"') and search.endswith('"')) or (search.startswith("'") and search.endswith("'"))
        actual_search = search[1:-1] if is_exact else search
        if is_exact:
            # GLOB is case-sensitive in SQLite
            query += " AND (title GLOB ? OR content GLOB ?)"
            params += [f"*{actual_search}*", f"*{actual_search}*"]
        else:
            query += " AND (LOWER(title) LIKE LOWER(?) OR LOWER(content) LIKE LOWER(?))"
            params += [f"%{actual_search}%", f"%{actual_search}%"]

    query += " ORDER BY importance DESC, updated_at DESC"

    if limit:
        query += f" LIMIT {int(limit)}"

    cur.execute(query, params)
    rows = cur.fetchall()
    con.close()
    return rows

# ── Render one entry ──────────────────────────────────────────────────────────
def parse_metadata(metadata_json: str) -> tuple[list, dict]:
    """Returns (tags, extras) from metadata_json. extras excludes 'tags'."""
    if not metadata_json:
        return [], {}
    try:
        meta = json.loads(metadata_json)
    except (json.JSONDecodeError, TypeError):
        return [], {}
    tags = meta.get("tags") or []
    extras = {k: v for k, v in meta.items() if k != "tags" and v}
    return tags, extras

def render(idx: int, row, total: int) -> str:
    tc    = type_colour(row["type"])
    row_type = (row["type"] or "unknown")
    lines = []
    lines.append(SEP)
    lines.append(
        f"{BOLD}#{idx:>3}/{total}{RESET}  "
        f"{tc}type:{row_type.lower()}{RESET}  "
        f"{importance_bar(row['importance'] or 0)}  "
        f"{DIM}{row['origin_device'] or '?'} / {row['change_agent'] or '?'}{RESET}"
    )
    lines.append(f"{BOLD}{CYAN}{row['title']}{RESET}")
    lines.append(f"{DIM}id: {row['id']}  created: {(row['created_at'] or '')[:19]}  updated: {(row['updated_at'] or '')[:19]}{RESET}")

    tags, extras = parse_metadata(row["metadata_json"])
    meta_parts = []
    if tags:
        meta_parts.append(f"{YELLOW}tags:{RESET} {' · '.join(tags)}")
    for k, v in extras.items():
        meta_parts.append(f"{DIM}{k}:{RESET} {v}")
    if meta_parts:
        lines.append("  " + "   ".join(meta_parts))

    lines.append("")
    content = (row["content"] or "").strip()
    for para in content.split("\n"):
        lines.append(textwrap.fill(para, width=COLS - 2, subsequent_indent="  ") if para.strip() else "")
    return "\n".join(lines)

# ── Pager ─────────────────────────────────────────────────────────────────────
def page(lines: list[str], page_size: int = 40):
    buf = []
    for line in lines:
        buf.append(line)
        if len(buf) >= page_size:
            print("\n".join(buf))
            buf.clear()
            try:
                ans = input(f"\n{DIM}── Press Enter for more, q to quit ──{RESET} ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if ans == "q":
                return
    if buf:
        print("\n".join(buf))

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Browse knowledge base in rank order")
    ap.add_argument("-n", "--limit",     type=int,  default=None,  help="Max entries to show")
    ap.add_argument("-t", "--type",      type=str,  default=None,  help="Filter by type (fact, decision, knowledge, project…)")
    ap.add_argument("-s", "--search",    type=str,  default=None,  help="Search title/content (case-insensitive)")
    ap.add_argument("--no-pager",        action="store_true",       help="Print all without paging")
    ap.add_argument("--db",              type=str,  default=None,  help="Override DB path")
    args = ap.parse_args()

    db = Path(args.db) if args.db else DB_PATH

    rows = fetch(db, args.type, args.search, args.limit)
    total = len(rows)

    if total == 0:
        print("No entries found.")
        return

    header = (
        f"\n{SEP2}\n"
        f"{BOLD}{CYAN}  Knowledge Base  —  {total} entries"
        + (f"  [type={args.type}]" if args.type else "")
        + (f"  [search={args.search}]" if args.search else "")
        + f"{RESET}\n{SEP2}"
    )

    all_lines = [header]
    for i, row in enumerate(rows, 1):
        all_lines.append(render(i, row, total))
    all_lines.append(SEP)

    if args.no_pager:
        print("\n".join(all_lines))
    else:
        page(all_lines, page_size=50)

if __name__ == "__main__":
    # Ensure UTF-8 output on all platforms (esp. Windows cmd/PowerShell)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    main()
