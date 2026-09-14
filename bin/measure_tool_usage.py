#!/usr/bin/env python3
"""Freeze the m3 tool-usage baseline from agent transcripts.

P0 of the slim-tools work. One script, run once, prints the whole baseline --
no manual collation, no second pass, no hand-edited numbers. Re-running it on
the same transcripts reproduces the same output.

REPRODUCIBLE, NOT FROZEN: the counts grow while a session is live, because the
running agent appends to the transcript this script reads -- measuring it
changes it. Two runs minutes apart legitimately differ (observed: delegate
26 -> 27 across one edit). That is the observer effect, not nondeterminism.
Pin a comparison with --since, or diff against a saved --json artifact.

WHY A SCRIPT AND NOT A COUNT IN A DOC: the first two attempts at this number
were both wrong, in ways a frozen figure would have hidden.

  * A getsource()-based sweep silently skipped every tool in the catalog
    (LazyImpl wrappers have no source) and reported a confident zero.
  * A transcript count of 391 calls missed every tool invoked THROUGH m3_call
    and every tool invoked from the CLI, undercounting the delegated surface.

THREE SURFACES, COUNTED SEPARATELY. They are different questions and a single
total hides the one that matters:

  direct   -- mcp__m3_memory__<tool> as its own tool_use block. The startup
              surface: these are the tools whose schemas are loaded eagerly.
  delegate -- a tool named in an m3_call payload ({"tool": "x"} or a batch).
              Reached WITHOUT a loaded schema, so usage here is evidence the
              proxy works, not evidence the tool needs promoting.
  cli      -- `m3 <domain> <tool>` in a Bash command string. Invisible to any
              MCP-level count; a tool used only here looks unused.

A tool with zero DIRECT calls but heavy DELEGATE use is not unused -- it is
being reached the cheap way, which is the outcome the slim plan wants. Demoting
it on a merged total would be backwards. That distinction is the point of this
script.

Usage:
    python bin/measure_tool_usage.py                 # human-readable
    python bin/measure_tool_usage.py --json          # machine-readable
    python bin/measure_tool_usage.py --since 2026-08-01
    python bin/measure_tool_usage.py --transcripts DIR
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

MCP_PREFIXES = ("mcp__m3_memory__", "mcp__plugin_m3_memory__")

# `m3 <domain> <tool>` / `m3 memory memory_search ...`. Both the domain and the
# bare form are accepted because the CLI takes either; the captured group is
# the last token, which is the tool.
_CLI_RE = re.compile(
    r"\bm3\s+(?:[a-z_]+\s+)?([a-z][a-z0-9_]{2,})\b(?!\s*=)",
    re.IGNORECASE,
)
# Subcommands of `m3` that are NOT catalog tools -- counting them would inflate
# the CLI surface with things that have no ToolSpec at all.
_CLI_NON_TOOLS = {
    "install", "setup", "start", "stop", "status", "doctor", "upgrade",
    "uninstall", "version", "help", "embedder", "chatlog", "memory", "admin",
    "agent", "tasks", "files", "conversations", "entity", "diagnostics",
    "config", "serve", "run", "init", "migrate", "backup", "restore",
}


def _iter_tool_uses(path: Path):
    """Yield (tool_name, input_dict) for every tool_use block in a transcript."""
    try:
        fh = path.open(encoding="utf-8", errors="ignore")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue  # a partial write at the tail is normal, not an error
            content = (rec.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            ts = rec.get("timestamp") or ""
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    yield block.get("name") or "?", block.get("input") or {}, ts


def _delegated_tools(payload: dict) -> list[str]:
    """Tool names an m3_call payload delegates to.

    Handles both shapes the proxy accepts: a single {"tool": ...} and a
    {"batch": [{"tool": ...}, ...]}. A batch of 40 is 40 calls, not one --
    counting the m3_call wrapper alone is exactly the undercount P0 exists to
    correct.
    """
    out: list[str] = []
    tool = payload.get("tool")
    if isinstance(tool, str) and tool:
        out.append(tool)
    batch = payload.get("batch")
    if isinstance(batch, list):
        for item in batch:
            if isinstance(item, dict):
                t = item.get("tool")
                if isinstance(t, str) and t:
                    out.append(t)
    return out


def _cli_tools(command: str) -> list[str]:
    """m3 catalog tools invoked from a shell command string."""
    if "m3 " not in command:
        return []
    found = []
    for m in _CLI_RE.finditer(command):
        name = m.group(1).lower()
        if name in _CLI_NON_TOOLS:
            continue
        # Catalog tools are domain_verb; a bare word with no underscore is
        # almost always a subcommand or a flag value, not a tool.
        if "_" not in name:
            continue
        found.append(name)
    return found


def collect(transcript_dir: Path, since: str | None):
    direct: Counter[str] = Counter()
    delegate: Counter[str] = Counter()
    cli: Counter[str] = Counter()
    m3_call_wrappers = 0
    files = 0
    other_tools: Counter[str] = Counter()

    for path in sorted(transcript_dir.rglob("*.jsonl")):
        files += 1
        for name, payload, ts in _iter_tool_uses(path):
            if since and ts and ts[:10] < since:
                continue
            if name.startswith(MCP_PREFIXES):
                short = name.split("__")[-1]
                if short == "m3_call":
                    m3_call_wrappers += 1
                    for d in _delegated_tools(payload):
                        delegate[d] += 1
                else:
                    direct[short] += 1
            elif name in ("Bash", "PowerShell"):
                cmd = payload.get("command") or ""
                if isinstance(cmd, str):
                    for t in _cli_tools(cmd):
                        cli[t] += 1
            else:
                other_tools[name] += 1

    return {
        "transcript_dir": str(transcript_dir),
        "transcripts_scanned": files,
        "since": since,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "direct": dict(direct),
        "delegate": dict(delegate),
        "cli": dict(cli),
        "m3_call_wrappers": m3_call_wrappers,
        "totals": {
            "direct": sum(direct.values()),
            "delegate": sum(delegate.values()),
            "cli": sum(cli.values()),
            "m3_call_wrappers": m3_call_wrappers,
        },
        "non_m3_tool_calls": sum(other_tools.values()),
    }


def _catalog_tools() -> set[str]:
    """Every tool name in the catalog, so we can name the ZEROS.

    A usage table lists what was called. The demotion question needs the
    complement -- what was never called -- which requires the catalog.
    """
    bin_dir = Path(__file__).resolve().parent
    if str(bin_dir) not in sys.path:
        sys.path.insert(0, str(bin_dir))
    try:
        import mcp_tool_catalog as cat  # type: ignore
        names: set[str] = set()
        for attr in ("TOOLS", "PROTOCOL_TOOLS", "DEBUG_TOOLS"):
            val = getattr(cat, attr, None)
            if isinstance(val, dict):
                names.update(val.keys())
            elif isinstance(val, (list, tuple, set)):
                for item in val:
                    n = getattr(item, "name", None) or (
                        item.get("name") if isinstance(item, dict) else None)
                    if n:
                        names.add(n)
        return names
    except Exception as e:  # noqa: BLE001
        print(f"[warn] catalog unavailable ({type(e).__name__}: {e}); "
              f"zero-call list omitted", file=sys.stderr)
        return set()


def render(data: dict) -> None:
    t = data["totals"]
    print("=" * 72)
    print("m3 TOOL-USAGE BASELINE (P0)")
    print("=" * 72)
    print(f"transcripts scanned : {data['transcripts_scanned']}")
    print(f"since               : {data['since'] or '(all)'}")
    print(f"generated           : {data['generated_at']}")
    print()
    print("BY SURFACE (these are different questions -- do not sum them):")
    print(f"  direct   (loaded schema, startup surface) : {t['direct']:6d}")
    print(f"  delegate (via m3_call, no schema loaded)  : {t['delegate']:6d}"
          f"   in {t['m3_call_wrappers']} m3_call wrapper(s)")
    print(f"  cli      (m3 ... in a shell command)      : {t['cli']:6d}")
    print()

    for label, key in (("DIRECT", "direct"), ("DELEGATE", "delegate"), ("CLI", "cli")):
        rows = sorted(data[key].items(), key=lambda kv: -kv[1])
        print(f"-- {label} ({len(rows)} distinct) " + "-" * (50 - len(label)))
        for name, n in rows[:25]:
            print(f"   {n:5d}  {name}")
        if len(rows) > 25:
            print(f"   ... {len(rows) - 25} more")
        print()

    catalog = _catalog_tools()
    if catalog:
        used = set(data["direct"]) | set(data["delegate"]) | set(data["cli"])
        zeros = sorted(catalog - used)
        print(f"-- ZERO CALLS ON EVERY SURFACE ({len(zeros)} of {len(catalog)}) " + "-" * 14)
        print("   Candidates ONLY. Zero calls is a SHAPE signal as much as a need")
        print("   signal: a tool whose return shape forces a Bash fallback gets")
        print("   avoided, so demoting on this list before the shape is fixed")
        print("   would bury a capability for being unusable. Re-measure after")
        print("   as_records lands (P1.5) before acting on any of these.")
        for i in range(0, len(zeros), 3):
            print("   " + "  ".join(f"{z:<34}" for z in zeros[i:i + 3]).rstrip())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    default_dir = Path(os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--transcripts", type=Path, default=default_dir,
                    help=f"transcript root (default: {default_dir})")
    ap.add_argument("--since", help="ignore calls before this YYYY-MM-DD")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args()

    if not args.transcripts.is_dir():
        print(f"error: no transcript directory at {args.transcripts}",
              file=sys.stderr)
        return 2

    data = collect(args.transcripts, args.since)
    if args.json:
        json.dump(data, sys.stdout, indent=2, sort_keys=True)
        print()
    else:
        render(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
