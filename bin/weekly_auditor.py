#!/usr/bin/env python3
"""Weekly Audit Report -- M3 Memory

Generates a PDF covering:
  1. Memory System Health (memory_items + embeddings)
  2. Project Decisions (last 7 days)
  3. Activity Timeline (legacy activity_logs)
  4. ChromaDB Sync Status
  5. Git Activity (~/m3-memory)

Optionally writes a consolidated summary into memory_items + ChromaDB.
Use --no-memory to skip the memory write step.
"""

import argparse
import asyncio
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from fpdf import FPDF
from fpdf.enums import XPos, YPos

# -- Constants ----------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_PATH = BASE_DIR
REPORTS_DIR = os.path.join(BASE_DIR, "reports")

# DB path is resolved at main() time so --database / M3_DATABASE overrides
# work. Module-level default points at the active resolver's default so
# top-level imports still work for tools that import helpers from this file.
sys.path.insert(0, os.path.join(BASE_DIR, "bin"))
from m3_sdk import add_database_arg, resolve_db_path
DB_PATH = resolve_db_path(None)


# -- PDF class ----------------------------------------------------------------
class AI_Report(FPDF):
    def header(self):
        self.set_font("helvetica", "B", 16)
        self.cell(0, 10, "M3 MAX - WEEKLY AI ACTIVITY REPORT",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
        self.set_font("helvetica", "I", 10)
        self.cell(0, 10, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
        self.ln(10)

    def mc(self, h, text):
        """multi_cell wrapper that resets X to left margin first."""
        self.set_x(self.l_margin)
        self.multi_cell(w=0, h=h, text=text)


# -- Helpers ------------------------------------------------------------------
def sanitize(text, max_len=200):
    """Remove non-latin1 chars and truncate for PDF safety."""
    if not text:
        return "(empty)"
    text = str(text)[:max_len]
    return text.encode("latin-1", errors="replace").decode("latin-1")


def _db():
    # Re-resolve per call so a late --database flag in main() is honored by
    # section helpers invoked afterwards.
    conn = sqlite3.connect(resolve_db_path(None), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _week_label():
    """ISO week label like '2026-W10'."""
    now = datetime.now()
    return f"{now.year}-W{now.isocalendar()[1]:02d}"


# -- Section 1: Memory System Health -----------------------------------------
def section_memory_health(pdf, summary):
    pdf.set_font("helvetica", "B", 14)
    pdf.cell(0, 10, "1. Memory System Health",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("helvetica", "", 10)

    conn = _db()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM memory_items WHERE is_deleted = 0"
        ).fetchone()[0]

        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        added = conn.execute(
            "SELECT COUNT(*) FROM memory_items WHERE is_deleted = 0 AND created_at > ?",
            (week_ago,),
        ).fetchone()[0]
        updated = conn.execute(
            "SELECT COUNT(*) FROM memory_items WHERE is_deleted = 0 AND updated_at > ?",
            (week_ago,),
        ).fetchone()[0]

        imp = conn.execute(
            "SELECT AVG(importance), MIN(importance), MAX(importance) "
            "FROM memory_items WHERE is_deleted = 0"
        ).fetchone()
        avg_imp = imp[0] or 0.0
        min_imp = imp[1] or 0.0
        max_imp = imp[2] or 0.0

        low_imp = conn.execute(
            "SELECT COUNT(*) FROM memory_items WHERE is_deleted = 0 AND importance < 0.3"
        ).fetchone()[0]

        with_embed = conn.execute(
            "SELECT COUNT(DISTINCT me.memory_id) FROM memory_embeddings me "
            "JOIN memory_items mi ON me.memory_id = mi.id WHERE mi.is_deleted = 0"
        ).fetchone()[0]
        without_embed = total - with_embed
    finally:
        conn.close()

    lines = [
        f"Total memory items (active): {total}",
        f"Added this week: {added}  |  Updated this week: {updated}",
        f"Importance -- avg: {avg_imp:.3f}, min: {min_imp:.3f}, max: {max_imp:.3f}",
        f"Items with importance < 0.3 (decay warning): {low_imp}",
        f"Embedding coverage: {with_embed} with embeddings, {without_embed} without",
    ]
    for line in lines:
        pdf.mc(6, sanitize(line, 500))
    pdf.ln(4)

    summary["memory_health"] = "\n".join(lines)


# -- Section 2: Project Decisions ---------------------------------------------
def section_decisions(pdf, summary):
    pdf.set_font("helvetica", "B", 14)
    pdf.cell(0, 10, "2. Project Decisions (Last 7 Days)",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("helvetica", "", 10)

    conn = _db()
    try:
        total_all = conn.execute("SELECT COUNT(*) FROM project_decisions").fetchone()[0]
        rows = conn.execute(
            "SELECT timestamp, project, decision, rationale FROM project_decisions "
            "WHERE timestamp > datetime('now', '-7 days') "
            "ORDER BY timestamp DESC"
        ).fetchall()
    finally:
        conn.close()

    pdf.mc(6, sanitize(
        f"Total decisions (all time): {total_all}  |  This week: {len(rows)}", 500
    ))
    pdf.ln(2)

    if not rows:
        pdf.mc(6, "No decisions recorded this week.")
    else:
        for row in rows:
            pdf.set_font("helvetica", "B", 9)
            pdf.mc(5, sanitize(f"[{row['timestamp']}] {row['project']}", 300))
            pdf.set_font("helvetica", "", 9)
            pdf.mc(5, sanitize(f"Decision: {row['decision']}", 500))
            if row["rationale"]:
                pdf.mc(5, sanitize(f"Rationale: {row['rationale']}", 500))
            pdf.ln(2)

    decision_lines = [f"Total: {total_all}, this week: {len(rows)}"]
    for row in rows[:10]:
        decision_lines.append(
            f"  - [{row['timestamp']}] {row['project']}: {row['decision']}"
        )
    summary["decisions"] = "\n".join(decision_lines)


# -- Section 3: Activity Timeline (legacy) ------------------------------------
def section_activity(pdf, summary):
    pdf.set_font("helvetica", "B", 14)
    pdf.cell(0, 10, "3. Activity Timeline (Legacy Logs)",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("helvetica", "", 10)

    conn = _db()
    try:
        rows = conn.execute(
            "SELECT timestamp, query, response FROM activity_logs "
            "WHERE timestamp > datetime('now', '-7 days') "
            "ORDER BY timestamp DESC LIMIT 50"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        pdf.mc(6, "No activity log entries this week.")
    else:
        pdf.mc(6, sanitize(f"{len(rows)} entries in the last 7 days.", 200))
        pdf.ln(2)
        for row in rows:
            pdf.set_font("helvetica", "B", 9)
            pdf.mc(5, sanitize(f"[{row['timestamp']}] {row['query']}", 300))
            pdf.set_font("helvetica", "", 9)
            pdf.mc(5, sanitize(f"{row['response']}", 300))
            pdf.ln(2)

    summary["activity"] = f"{len(rows)} activity log entries this week."


# -- Section 4: ChromaDB Sync Status ------------------------------------------
def section_chroma_sync(pdf, summary):
    pdf.set_font("helvetica", "B", 14)
    pdf.cell(0, 10, "4. ChromaDB Sync Status",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("helvetica", "", 10)

    conn = _db()
    try:
        pending = conn.execute("SELECT COUNT(*) FROM chroma_sync_queue").fetchone()[0]
        failed = conn.execute(
            "SELECT COUNT(*) FROM chroma_sync_queue WHERE attempts > 0"
        ).fetchone()[0]
    finally:
        conn.close()

    if pending == 0:
        line = "All synced -- no pending items in queue."
    else:
        line = f"Pending sync items: {pending}  |  Failed attempts (retryable): {failed}"

    pdf.mc(6, sanitize(line, 500))
    pdf.ln(4)
    summary["chroma_sync"] = line


# -- Section 5: Git Activity --------------------------------------------------
def section_git(pdf, summary):
    pdf.set_font("helvetica", "B", 14)
    pdf.cell(0, 10, "5. Git Activity (m3-memory)",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("helvetica", "", 9)

    try:
        cmd = [
            "git", "-C", REPO_PATH, "log", "--since=1 week ago",
            "--pretty=format:%h - %s (%an)",
        ]
        output = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode("utf-8")
    except subprocess.CalledProcessError:
        output = "No recent git activity."

    if not output.strip():
        output = "No commits in the last 7 days."

    pdf.mc(5, sanitize(output, 3000))
    pdf.ln(4)
    summary["git"] = output[:500]


# -- Section 6: Tool Inventory Refresh ---------------------------------------
def section_tool_inventory(pdf, summary):
    """Refresh docs/tools/ and report which tools drifted.

    Drift = the live file's sha1 has changed since the last inventory write.
    We snapshot the `sha1:` frontmatter values BEFORE running the generator,
    then diff after.
    """
    pdf.set_font("helvetica", "B", 14)
    pdf.cell(0, 10, "6. Tool Inventory",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("helvetica", "", 9)

    inv_dir = os.path.join(BASE_DIR, "docs", "tools")
    gen_script = os.path.join(BASE_DIR, "bin", "gen_tool_inventory.py")

    def _snapshot_sha1s():
        out = {}
        if not os.path.isdir(inv_dir):
            return out
        for fn in os.listdir(inv_dir):
            if not fn.endswith(".md") or fn == "INDEX.md":
                continue
            path = os.path.join(inv_dir, fn)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        if line.startswith("sha1:"):
                            out[fn] = line.split(":", 1)[1].strip()
                            break
            except OSError:
                continue
        return out

    before = _snapshot_sha1s()
    try:
        subprocess.check_call(
            [sys.executable, gen_script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=120,
        )
        ran = True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        ran = False
        err = str(e)
    after = _snapshot_sha1s()

    # Refresh the mermaid call graph from the (possibly updated) inventory.
    graph_script = os.path.join(BASE_DIR, "scripts", "inventory_graph.py")
    graph_ok = False
    graph_err = ""
    if os.path.isfile(graph_script):
        try:
            subprocess.check_call(
                [sys.executable, graph_script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=30,
            )
            graph_ok = True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            graph_err = str(e)

    drifted = sorted(f for f in after if before.get(f) != after.get(f))
    new = sorted(f for f in after if f not in before)
    removed = sorted(f for f in before if f not in after)

    if not ran:
        line = f"Generator FAILED: {err}"
    elif not drifted and not new and not removed:
        line = f"{len(after)} tool entries — no drift since last run."
    else:
        parts = [f"{len(after)} tool entries."]
        if new:
            parts.append(f"NEW: {', '.join(n.replace('.md','') for n in new[:10])}")
        if drifted:
            parts.append(
                f"DRIFTED (source changed, re-enrichment recommended): "
                f"{', '.join(d.replace('.md','') for d in drifted[:15])}"
            )
        if removed:
            parts.append(f"REMOVED: {', '.join(r.replace('.md','') for r in removed[:10])}")
        line = " | ".join(parts)

    if graph_ok:
        line += " | Call graph: refreshed (CALL_GRAPH.md)."
    elif os.path.isfile(graph_script):
        line += f" | Call graph FAILED: {graph_err}"

    pdf.mc(5, sanitize(line, 2000))
    pdf.ln(4)
    summary["tool_inventory"] = line


# -- Memory write -------------------------------------------------------------
def write_summary_to_memory(summary_text, week_label):
    """Import memory_bridge functions and write a weekly summary memory item."""
    bin_dir = os.path.dirname(os.path.abspath(__file__))
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)

    from memory_bridge import chroma_sync, memory_write

    result = asyncio.run(memory_write(
        type="document",
        title=f"Weekly Audit Summary -- {week_label}",
        content=summary_text,
        metadata=f'{{"source_type": "weekly_audit", "week": "{week_label}"}}',
        importance=0.7,
        agent_id="weekly_auditor",
        source="system",
        embed=True,
    ))
    print(f"Memory write: {result}")

    sync_result = asyncio.run(chroma_sync())
    print(f"ChromaDB sync: {sync_result}")


# -- Main ---------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="M3 Max Weekly Audit Report")
    parser.add_argument(
        "--no-memory", action="store_true",
        help="Skip writing summary to memory system and ChromaDB",
    )
    add_database_arg(parser)
    args = parser.parse_args()

    if args.database:
        os.environ["M3_DATABASE"] = args.database

    week_label = _week_label()
    pdf = AI_Report()
    pdf.add_page()

    summary = {}

    section_memory_health(pdf, summary)
    section_decisions(pdf, summary)
    section_activity(pdf, summary)
    section_chroma_sync(pdf, summary)
    section_git(pdf, summary)
    section_tool_inventory(pdf, summary)

    output_path = os.path.join(
        REPORTS_DIR, f"Audit_{datetime.now().strftime('%Y_W%V')}.pdf"
    )
    os.makedirs(REPORTS_DIR, exist_ok=True)
    pdf.output(output_path)
    print(f"Weekly Audit saved to: {output_path}")

    if not args.no_memory:
        summary_text = (
            f"Weekly Audit Summary -- {week_label}\n\n"
            f"MEMORY HEALTH:\n{summary.get('memory_health', 'N/A')}\n\n"
            f"DECISIONS:\n{summary.get('decisions', 'N/A')}\n\n"
            f"ACTIVITY:\n{summary.get('activity', 'N/A')}\n\n"
            f"CHROMA SYNC:\n{summary.get('chroma_sync', 'N/A')}\n\n"
            f"GIT:\n{summary.get('git', 'N/A')}\n\n"
            f"TOOL INVENTORY:\n{summary.get('tool_inventory', 'N/A')}"
        )
        write_summary_to_memory(summary_text, week_label)
    else:
        print("--no-memory: skipping memory write and ChromaDB sync.")


if __name__ == "__main__":
    main()
